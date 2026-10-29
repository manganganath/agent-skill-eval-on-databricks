"""Run skills x models x modes and log one verdict per child run.

Order per run (binding): `stage` -> per child run: `mlflow.genai.evaluate` once per repeat -> read the
traces back -> `compute_verdict` -> `log_verdict` -> `record_rubric`; after every child run: `sync_run`.
The run schemas are dropped in `finally`. `sync` copies the verdict tag, so it comes after `log_verdict`.

- One parent run per department, created with `MlflowClient` in the experiment of the department's first skill
  and never made active, so each child run lives in its own skill experiment. Children carry the tags
  `run_key` and `parent_run_id`.
- One child run per skill x model x mode. Repeats are `evaluate` calls inside it. Trigger mode runs the
  `trigger` cases and the effectiveness modes run the `effectiveness` cases; `without_skill` runs first so
  `with_skill` can report uplift.
- No child run is left that sync cannot read. A skill whose setup fails gets no child run; it is recorded in
  `RunSummary.errors`. A child run that fails after it started is closed with verdict `inconclusive` and an
  `error` tag, so sync completes and the gate blocks (fail closed).
- The model sees production names only: the `ToolNameMap` rewrite covers every staged FQN of the scope.
- Up to `defaults.skill_workers` skills run at once, each in its own worker process (`skillhub.eval.worker`).
  In a process, child runs run one at a time, each with one `evaluate` over all its repeats. Results keep plan
  order.
"""

from __future__ import annotations

import copy
import dataclasses
import functools
import hashlib
import importlib
import json
import logging
import math
import os
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import mlflow
import mlflow.openai
from mlflow import MlflowClient
from mlflow.entities import SpanType
from mlflow.pyfunc.context import get_prediction_context

from skillhub import SkillHubError, evalspec
from skillhub.agent.fixtures import FixtureStore
from skillhub.agent.loop import SkillsBridge, run_agent
from skillhub.auth import auth_headers, workspace_client
from skillhub.bundle import SkillBundle, SkillRefError, discover_bundles, find_bundle, select_bundles
from skillhub.config import ConfigError
from skillhub.eval.judges import scorers_for
from skillhub.eval.outcomes import case_outcome
from skillhub.eval.scorers import PLANTED_FIXTURE, load_planted_bugs, planted_bugs_check
from skillhub.eval.verdict import (
    CaseScore,
    Verdict,
    VerdictResult,
    case_score_from_assessments,
    compute_verdict,
    gating_scores,
    log_verdict,
    trigger_case_score,
    trigger_metrics,
    uplift,
)
from skillhub.evalspec import (
    NO_RUBRIC,
    PLANTED_MANIFEST,
    EvalCase,
    EvalFile,
    GateThresholds,
    Rubric,
    dataset_digest,
    fixture_index_files,
    fixture_paths,
    gate_thresholds,
    load_rubric,
    read_evals,
    read_fixture,
    rubric_hash,
)
from skillhub.gateway.models import model_client
from skillhub.gateway.skills_mcp import SkillsMcpClient
from skillhub.gateway.toolnames import ToolNameMap
from skillhub.naming import judge_name, live_schema, make_run_key
from skillhub.prompts import load_packaged, load_prompt
from skillhub.registry.journal import Journal
from skillhub.registry.stage import RunScope, StagedSkill, drop_run_schemas, stage
from skillhub.registry.uc_skills import UCSkills
from skillhub.results.datasets import case_record
from skillhub.sql import SqlClient

log = logging.getLogger(__name__)

Mode = Literal["trigger", "with_skill", "without_skill"]
MAX_TURNS = 8
# The run plan is shared with the gates, which require a row for every planned gating cell.
EFFECTIVENESS_ORDER = evalspec.EFFECTIVENESS_ORDER
CASE_KIND = evalspec.CASE_KIND
REQUEST_MODES = evalspec.REQUEST_MODES
TRIGGER_SOURCES = ("pr", "merge", "nightly", "hub", "hub-try", "manual")
EVAL_PROMPT_KEYS = ("agent_system", "judge_criterion", "judge_skill_use")  # the prompts an eval run uses
TRACE_TIMEOUT_S = 300
TRACE_POLL_S = 5
TRACE_STALL_S = 60  # how long the trace count may stay unchanged after the evaluation returns
ERROR_MAX = 500  # characters kept of an error message on a run tag or in the summary
SKIP_TRACE_VALIDATION = "MLFLOW_GENAI_EVAL_SKIP_TRACE_VALIDATION"
MAX_WORKERS = "MLFLOW_GENAI_EVAL_MAX_WORKERS"  # the predict pool of one evaluate
MAX_SCORER_WORKERS = "MLFLOW_GENAI_EVAL_MAX_SCORER_WORKERS"
# Judges at once per case. It multiplies with the predict workers, so it stays small.
SCORER_WORKERS = 2
TRIGGER = "skill_trigger"
PLANTED = "planted_bugs_found"
_YES = frozenset({"yes", "true", "pass"})
_NO = frozenset({"no", "false", "fail"})
_NO_SKILL = frozenset({"", "None", "null"})
_NOT_RECORDED = object()  # the trigger assessment carries no `invoked_skill`
WORKER_STOP_S = 20  # how long a stopped worker gets to close its open child runs before it is killed
WORKER_TAIL_LINES = 20  # the stderr lines of a failed worker that its error keeps
# A child process cannot use a Job's runtime auth, so a worker gets a host and token; these would conflict.
_PAT_CONFLICTS = (
    "DATABRICKS_AUTH_TYPE",
    "DATABRICKS_CONFIG_PROFILE",
    "DATABRICKS_CLIENT_ID",
    "DATABRICKS_CLIENT_SECRET",
)


class EvalRunError(SkillHubError, ValueError):
    """The request names something that cannot be evaluated, or a skill's eval setup is broken."""


@dataclass
class RunRequest:
    run_key: str
    skills: list[str]  # `dept/name` or a bare `name` that exists in one department only
    models: list[str]  # empty: each skill's evals.json `models`, else defaults.models
    modes: list[Literal["trigger", "effectiveness"]]  # "effectiveness" expands to with_skill + without_skill
    source: str  # "git:<sha>" | "draft:<id>" | "live"
    trigger_source: Literal["pr", "merge", "nightly", "hub", "hub-try", "manual"]
    requested_by: str | None = None
    judges: list[str] | None = None  # default: cfg.judges_pr for pr, else cfg.judges
    versions: dict[str, int] | None = None  # Evaluate these registered versions instead of candidates
    skills_root: str | None = None  # Candidate skills folder; None means $SKILLHUB_SKILLS
    fixtures_root: str | None = None  # Fixtures folder; None means $SKILLHUB_FIXTURES


@dataclass
class ChildRunSummary:
    run_id: str
    dept: str
    skill: str
    skill_version: int | None
    content_hash: str
    model_profile: str
    mode: Mode
    verdict: Verdict
    metrics: dict[str, float]
    harness: str = "reference"  # The built-in agent loop; coding-agent harnesses are not supported yet


@dataclass
class RunError:
    """A skill or child run that could not be evaluated. Any error makes `skillhub eval` exit non-zero."""

    dept: str
    skill: str
    message: str
    model_profile: str | None = None
    mode: str | None = None
    run_id: str | None = None  # set when the child run exists (its verdict is `inconclusive`)


@dataclass
class RunSummary:
    run_key: str
    parent_run_ids: dict[str, str]  # one parent per department
    children: list[ChildRunSummary] = field(default_factory=list)
    errors: list[RunError] = field(default_factory=list)


# --- results, imported when first used -----------------------------------------------------------------

_RESULTS = (
    ("experiments", "configure_mlflow"),
    ("experiments", "ensure_experiment"),
    ("datasets", "sync_dataset"),
    ("sync", "sync_run"),
    ("sync", "record_rubric"),
)


def _results(module: str, name: str) -> Callable[..., Any]:
    try:
        fn = getattr(importlib.import_module(f"skillhub.results.{module}"), name)
    except (ImportError, AttributeError) as e:
        raise EvalRunError(
            f"skillhub.results.{module}.{name} is needed to run evals but cannot be imported: {e}"
        ) from e
    return fn


def _require_results() -> None:
    """Import the results functions up front, so a broken results package fails before anything is staged."""
    for module, name in _RESULTS:
        _results(module, name)


def configure_mlflow(cfg) -> None:
    _results("experiments", "configure_mlflow")(cfg)


def ensure_experiment(cfg, dept: str, skill: str) -> str:
    return _results("experiments", "ensure_experiment")(cfg, dept, skill)


def sync_dataset(cfg, dept: str, skill: str, ef: EvalFile, experiment_id: str) -> tuple[object, str]:
    return _results("datasets", "sync_dataset")(cfg, dept, skill, ef, experiment_id)


def sync_run(cfg, sql, run_key: str, *, timeout_s: int = 300, rebuild: bool = False):
    return _results("sync", "sync_run")(cfg, sql, run_key, timeout_s=timeout_s, rebuild=rebuild)


def record_rubric(cfg, sql, dept: str, rubric: Rubric) -> None:
    _results("sync", "record_rubric")(cfg, sql, dept, rubric)


# --- concurrency, tracing and trace read-back ---------------------------------------------------------------

_SEMAPHORES: dict[str, threading.BoundedSemaphore] = {}
_SEM_LOCK = threading.Lock()


def _endpoint_semaphore(endpoint: str, limit: int) -> threading.BoundedSemaphore:
    """One bound per serving endpoint for the whole process.

    The model client retries 429 and 5xx with backoff."""
    with _SEM_LOCK:
        return _SEMAPHORES.setdefault(endpoint, threading.BoundedSemaphore(limit))


def _tag_current_trace(tags: dict[str, str]) -> None:
    mlflow.update_current_trace(tags=tags)


def read_child_traces(
    exp_id: str,
    run_id: str,
    expected: int,
    *,
    complete: Callable[[list], bool] | None = None,
    timeout_s: int = TRACE_TIMEOUT_S,
) -> list:
    """The child run's traces.

    Traces and their assessments land about 20 s after the run, so this polls until
    `complete(traces)` holds (default: at least `expected` traces). After the timeout it returns what is
    there.
    """
    start = time.monotonic()
    progress, changed_at = None, start
    while True:
        mlflow.flush_trace_async_logging()
        # Full traces, as sync reads them: assessments come with the loaded trace.
        traces = mlflow.search_traces(locations=[exp_id], run_id=run_id, return_type="list")
        if complete(traces) if complete is not None else len(traces) >= expected:
            return traces
        now = time.monotonic()
        state = (len(traces), sum(1 for t in traces if t.info.assessments))
        if state != progress:
            progress, changed_at = state, now
        # The evaluation has returned, so traces that stop changing for TRACE_STALL_S are all there is
        # (seen live: a case whose trace never arrived).
        if now - start >= timeout_s or now - changed_at >= TRACE_STALL_S:
            log.warning(
                "run %s: traces still incomplete after %ss (%d of %d)",
                run_id,
                int(now - start),
                len(traces),
                expected,
            )
            return traces
        time.sleep(TRACE_POLL_S)


@contextmanager
def _evaluating(concurrency: int) -> Iterator[None]:
    """Settings for `mlflow.genai.evaluate`, restored afterwards.

    - `predict` is already traced, so MLflow's trace-validation call (one extra prediction) is skipped.
    - One evaluate predicts up to `concurrency` (`concurrency_per_endpoint`) records at once, and each case
      runs at most min(`concurrency`, SCORER_WORKERS) judges at a time, unless the environment sets
      MLFLOW_GENAI_EVAL_MAX_WORKERS or MLFLOW_GENAI_EVAL_MAX_SCORER_WORKERS. MLflow's scorer rate limit is in
      calls per second, not a concurrency, so it keeps its adaptive default (it backs off on 429).
    - MLflow prints a banner per `evaluate`; it goes to stderr so stdout stays the RunSummary JSON.
    """
    env = {SKIP_TRACE_VALIDATION: "true"}
    if MAX_WORKERS not in os.environ:
        env[MAX_WORKERS] = str(concurrency)
    if MAX_SCORER_WORKERS not in os.environ:
        env[MAX_SCORER_WORKERS] = str(min(concurrency, SCORER_WORKERS))
    prev = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        with redirect_stdout(sys.stderr):
            yield
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class _CachedSkills:
    """The skills bridge for one child run. `list_tools()` is asked once and shared by every case.

    A failure is re-raised with production names, because the staged schema appears in the server URL."""

    def __init__(self, bridge, names: ToolNameMap):
        self._bridge = bridge
        self._names = names
        self._tools: list | None = None
        self._lock = threading.Lock()

    def list_tools(self) -> list:
        with self._lock:
            if self._tools is None:
                try:
                    self._tools = self._bridge.list_tools()
                except Exception as e:
                    raise RuntimeError(
                        self._names.to_production(f"list_tools failed: {type(e).__name__}: {e}")
                    ) from None
            return self._tools

    def call_tool(self, name: str, arguments: dict) -> str:
        return self._bridge.call_tool(name, arguments)

    def close(self) -> None:
        self._bridge.close()


# --- request resolution -------------------------------------------------------------------------------------


def _select(bundles: list[SkillBundle], refs: list[str]) -> list[SkillBundle]:
    try:
        chosen = select_bundles(bundles, refs)  # the same resolver as the CLI
    except SkillRefError as e:
        raise EvalRunError(str(e)) from None
    if not chosen:
        raise EvalRunError("no skills selected")
    return chosen


def _pinned(selected: list[SkillBundle], versions: dict[str, int] | None) -> dict[tuple[str, str], int]:
    out: dict[tuple[str, str], int] = {}
    for ref, n in (versions or {}).items():
        try:
            b = find_bundle(selected, ref)
        except SkillRefError as e:
            raise EvalRunError(
                f"versions name {ref!r}, which is not one of the selected skills ({e})"
            ) from None
        if isinstance(n, bool) or not isinstance(n, int) or n < 1:
            raise EvalRunError(f"version for {ref!r} must be a positive whole number, got {n!r}")
        out[(b.dept, b.name)] = n
    return out


def _judges(cfg, req: RunRequest) -> list[str]:
    # A profile listed twice would cast two votes with one judge.
    return list(dict.fromkeys(req.judges or (cfg.judges_pr if req.trigger_source == "pr" else cfg.judges)))


def _check_request(cfg, req: RunRequest) -> None:
    bad = [m for m in [*req.models, *(req.judges or [])] if m not in cfg.models]
    if bad:
        raise EvalRunError(f"unknown model profile(s): {', '.join(bad)}")
    if not req.modes or any(m not in REQUEST_MODES for m in req.modes):
        raise EvalRunError(f"modes must be trigger and/or effectiveness, got {req.modes}")
    if not (req.source == "live" or req.source.startswith(("git:", "draft:"))):
        raise EvalRunError(f"source must be git:<sha>, draft:<id> or live, got {req.source!r}")
    if req.trigger_source not in TRIGGER_SOURCES:
        raise EvalRunError(f"trigger_source must be one of {', '.join(TRIGGER_SOURCES)}")


@dataclass
class _SkillPlan:
    bundle: SkillBundle
    ef: EvalFile
    models: list[str]  # the profiles this skill runs on that a model client can serve
    evals_bytes: bytes = b""  # the evals.json bytes `ef` was parsed from
    dataset_digest: str = ""  # evals.json and the fixtures its cases use, as the agent reads them


def _plan(
    cfg, req: RunRequest, selected: list[SkillBundle]
) -> tuple[list[_SkillPlan], dict[str, Any], list[RunError]]:
    """Read each skill's evals.json, resolve its models and build their clients before anything is staged.

    An explicit request list wins; otherwise the skill's own `models`, else defaults.models. An
    unknown profile in evals.json is a request error. A skill whose evals.json cannot be read, or a profile
    whose route no model client serves yet (for example `ug-anthropic`, which is not supported yet), is recorded for that skill."""
    plans: list[_SkillPlan] = []
    clients: dict[str, Any] = {}
    unservable: dict[str, str] = {}
    errors: list[RunError] = []
    for b in selected:
        try:
            ef, data = _load_evals(b)
        except EvalRunError as e:
            errors.append(RunError(b.dept, b.name, str(e)[:ERROR_MAX]))
            continue
        models = evalspec.resolve_models(ef, req.models, cfg.defaults.models)
        unknown = [m for m in models if m not in cfg.models]
        if unknown:
            raise EvalRunError(
                f"{b.dept}/{b.name}: evals.json names unknown model profile(s): {', '.join(unknown)}"
            )
        for m in models:
            if m in clients or m in unservable:
                continue
            try:
                clients[m] = model_client(cfg, m, auth_headers)
            except (NotImplementedError, ConfigError) as e:
                unservable[m] = f"model profile {m!r} cannot be served: {e}"
        errors += [
            RunError(b.dept, b.name, unservable[m], model_profile=m) for m in models if m in unservable
        ]
        servable = [m for m in models if m in clients]
        if servable:
            plans.append(_SkillPlan(bundle=b, ef=ef, models=servable, evals_bytes=data))
    return plans, clients, errors


def _snapshot_fixtures(source: Path, plans: list[_SkillPlan], dest: Path) -> FixtureStore:
    """Copy the fixtures the planned cases use into `dest` and set each plan's dataset digest from the same
    bytes. The agent and the scorers read only the copy, so a fixture changed in the candidate folder
    during the eval cannot reach them, and the digest names exactly what they read. Each file is read once."""
    index = fixture_index_files(source)
    files: dict[str, bytes | None] = {}
    for plan in plans:
        paths = fixture_paths(plan.ef, index)
        for rel in paths:
            if rel not in files:
                files[rel] = read_fixture(source, rel)
        plan.dataset_digest = dataset_digest(plan.evals_bytes, {rel: files[rel] for rel in paths})
    if PLANTED_MANIFEST not in files:  # the audit-xls scorer loads it even without a planted case
        files[PLANTED_MANIFEST] = read_fixture(source, PLANTED_MANIFEST)
    root = dest.resolve()
    for rel, data in files.items():
        if data is None:
            continue
        target = (root / rel).resolve()
        if root not in target.parents:  # read_fixture already refused such a path
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return FixtureStore(root)


def _modes_for(modes: list[str]) -> list[str]:
    return evalspec.expand_modes(modes)


def _git_sha(source: str) -> str:
    return source.split(":", 1)[1] if source.startswith("git:") else ""


def _load_prompts(cfg) -> tuple[str, dict[str, int]]:
    """The agent system prompt (registry, packaged fallback) and the version of each prompt the run uses.

    Judges are built from the packaged text, so a judge prompt records its registry version only when the
    registry holds that same text; otherwise 0 (not registered)."""
    refs = {k: load_prompt(cfg, k) for k in EVAL_PROMPT_KEYS}
    versions = {
        k: r.version if k == "agent_system" or r.template == load_packaged(k) else 0 for k, r in refs.items()
    }
    return refs["agent_system"].template, versions


# --- the run ------------------------------------------------------------------------------------------------


@dataclass
class _ModeData:
    data: object  # what `evaluate` gets: the dataset itself, or records of this case kind rebuilt from evals.json
    case_ids: list[int]  # the cases that are evaluated
    n_records: int
    missing_ids: list[int] = field(default_factory=list)  # cases of this kind with no dataset row: errors
    records: list[dict] = field(default_factory=list)  # the records rebuilt from evals.json, one per case


@dataclass
class _SkillCtx:
    cfg: Any
    req: RunRequest
    judges: list[str]
    scope: RunScope
    skill: StagedSkill
    ef: EvalFile
    cases: dict[int, EvalCase]
    rubric: Rubric | None
    thresholds: GateThresholds  # every verdict input: defaults, tightened per skill only
    data: dict[str, _ModeData]  # by case kind
    scorers: dict[str, list]  # by mode, built before any child run starts
    digest: str
    names: ToolNameMap
    fixtures: FixtureStore
    system_prompt: str
    prompt_versions: dict[str, int]
    exp_id: str
    parent_id: str
    version: int | None
    sql: Any


@dataclass
class _SkillRun:
    """One skill's `_run_skill` call, made in this process or in a worker process."""

    plan: _SkillPlan
    scope: RunScope
    skill: StagedSkill
    exp_id: str
    parent_id: str
    version: int | None
    pinned: bool = False  # `version` comes from the request, not from the journal


def run_eval(cfg, req: RunRequest) -> RunSummary:
    req = dataclasses.replace(req, run_key=make_run_key(req.run_key))
    _check_request(cfg, req)
    judges = _judges(cfg, req)
    selected = _select(discover_bundles(Path(req.skills_root) if req.skills_root else None), req.skills)
    pinned = _pinned(selected, req.versions)  # everything up to staging is checked before anything is written
    plans, clients, errors = _plan(cfg, req, selected)
    _require_results()
    depts = sorted({b.dept for b in selected})

    mlflow.openai.autolog()  # LLM spans in every trace
    w = workspace_client()
    sql = SqlClient(w, cfg.workspace.sql_warehouse_id)
    journal = Journal(cfg, sql)
    # A pinned skill is staged from its registered version, not from the candidate bundle.
    candidates = [] if req.source == "live" else [b for b in selected if (b.dept, b.name) not in pinned]
    snapshot = Path(tempfile.mkdtemp(prefix="skillhub-fixtures-"))
    parents: dict[str, str] = {}
    children: list[ChildRunSummary] = []
    ok = False
    try:
        fixtures = _snapshot_fixtures(FixtureStore(req.fixtures_root).root, plans, snapshot)
        scopes = stage(
            cfg,
            UCSkills(w),
            journal,
            req.run_key,
            candidates,
            scope_depts=depts,
            versions={f"{d}/{name}": n for (d, name), n in pinned.items()} or None,  # dept/name
        )
        by_dept = {s.dept: s for s in scopes}
        prompts: tuple[str, dict[str, int]] | None = None
        planned: list[_SkillRun | RunError] = []  # in plan order; the skills run after the checks
        for dept in depts:
            scope = by_dept[dept]
            staged = {s.name: s for s in scope.skills if s.dept == dept}
            for plan in (p for p in plans if p.bundle.dept == dept):
                name = plan.bundle.name
                skill = staged.get(name)
                if skill is None:
                    if req.source == "live":  # never switched live: nothing to evaluate
                        log.warning("%s/%s has no live version; skipped", dept, name)
                    else:
                        planned.append(RunError(dept, name, "the candidate was not staged in the run schema"))
                    continue
                exp_id = ensure_experiment(cfg, dept, name)
                if prompts is None:  # after ensure_experiment, which points MLflow at the workspace
                    prompts = _load_prompts(cfg)
                if dept not in parents:
                    parents[dept] = _open_parent(req, scope, exp_id, prompts[1])
                version = pinned.get((dept, name))
                if version is not None and skill.origin != "version":  # Never label other content as vN
                    planned.append(
                        RunError(
                            dept,
                            name,
                            f"version {version} was pinned, but the run schema holds the {skill.origin} copy; "
                            f"pin a version published in {dept}",
                        )
                    )
                    continue
                pin = version is not None
                if version is None:
                    version = journal.version_by_hash(dept, name, skill.content_hash)
                planned.append(_SkillRun(plan, scope, skill, exp_id, parents[dept], version, pin))
        runs = [p for p in planned if isinstance(p, _SkillRun)]
        results = iter(
            _run_skills(
                cfg,
                req,
                runs,
                clients=clients,
                judges=judges,
                prompts=prompts,
                fixtures=fixtures,
                sql=sql,
                w=w,
            )
        )
        for item in planned:  # results are kept in plan order, whichever worker finishes first
            if isinstance(item, RunError):
                errors.append(item)
                continue
            ran, failed = next(results)
            children += ran
            errors += failed
        if children:
            sync_run(cfg, sql, req.run_key)
        elif not errors:
            raise EvalRunError("nothing was evaluated: no selected skill has cases for the requested modes")
        ok = True
    finally:
        _close_parents(parents, ok)
        shutil.rmtree(snapshot, ignore_errors=True)
        try:
            drop_run_schemas(cfg, w, req.run_key)
        except Exception:  # the cleanup Job drops leftover run schemas
            log.warning("could not drop the run schemas of %s", req.run_key, exc_info=True)
    for e in errors:
        log.error("%s/%s %s %s: %s", e.dept, e.skill, e.model_profile or "", e.mode or "", e.message)
    return RunSummary(run_key=req.run_key, parent_run_ids=parents, children=children, errors=errors)


# --- skills at once, one worker process each ------------------------------------------------------------------


def _run_skills(
    cfg, req: RunRequest, runs: list[_SkillRun], *, clients, judges, prompts, fixtures, sql, w
) -> list[tuple[list[ChildRunSummary], list[RunError]]]:
    """(children, errors) per skill run, in order.

    With `defaults.skill_workers` > 1, up to that many skills run at once, each in its own process. MLflow keeps
    one trace destination per process (set_experiment resets it) and links a trace to the latest active run of
    any thread, so two skill experiments must never share a process."""
    if cfg.defaults.skill_workers == 1 or len(runs) <= 1:
        return [
            _run_skill(
                cfg,
                req,
                r.plan,
                r.scope,
                r.skill,
                clients=clients,
                judges=judges,
                exp_id=r.exp_id,
                parent_id=r.parent_id,
                prompts=prompts,
                version=r.version,
                fixtures=fixtures,
                sql=sql,
                index=i,
            )
            for i, r in enumerate(runs)
        ]
    return _run_in_workers(cfg, req, runs, prompts=prompts, fixtures=fixtures, w=w)


def _wait_all(futures) -> None:
    pending = set(futures)
    while pending:  # with a timeout, so the waiting main thread still handles a signal
        _, pending = wait(pending, timeout=1)


def _raise_on_sigterm(signum, frame) -> None:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)  # one is enough: a second must not cut the cleanup short
    raise SystemExit(128 + signum)


@contextmanager
def _sigterm_raises() -> Iterator[None]:
    """A SIGTERM (a Job cancel) raises SystemExit inside, so the cleanup in `finally` blocks runs.

    Only the main thread can set a signal handler; elsewhere this does nothing."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    prev = signal.signal(signal.SIGTERM, _raise_on_sigterm)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, prev)


class _Workers:
    """The worker processes started so far, so that an interrupt can stop them all."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._procs: list[subprocess.Popen] = []
        self._stopping = False

    def start(self, cmd: list[str], env: dict[str, str]) -> subprocess.Popen | None:
        with self._lock:
            if self._stopping:
                return None
            proc = subprocess.Popen(
                cmd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
            )
            self._procs.append(proc)
            return proc

    def stop(self, grace_s: float) -> None:
        """SIGTERM each running worker (it closes its open child runs), then kill those still running."""
        with self._lock:
            self._stopping = True
            procs = [p for p in self._procs if p.poll() is None]
        for p in procs:
            p.terminate()
        deadline = time.monotonic() + grace_s
        for p in procs:
            try:
                p.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                p.kill()


_STDERR_LOCK = threading.Lock()


def _launch_worker(
    cmd: list[str], env: dict[str, str], label: str, procs: _Workers
) -> tuple[int | None, str]:
    """Run one worker to its end: (exit code, the last lines of its stderr).

    Its stderr streams to this process's stderr (the Job log), each line prefixed with `[dept/skill]`. Its
    stdout is not read."""
    proc = procs.start(cmd, env)
    if proc is None:
        return None, "not started: the run was interrupted"
    tail: deque[str] = deque(maxlen=WORKER_TAIL_LINES)
    for line in proc.stderr:
        line = line.rstrip("\n")
        tail.append(line)
        with _STDERR_LOCK:
            sys.stderr.write(f"[{label}] {line}\n")
            sys.stderr.flush()
    return proc.wait(), "\n".join(tail)


def _worker_command(spec: Path, out: Path) -> list[str]:
    return [sys.executable, "-m", "skillhub.eval.worker", str(spec), str(out)]


def _worker_env(w) -> dict[str, str]:
    """The worker's environment: this process's, plus a host and token when this process uses a Job's runtime
    auth, which a child process cannot use. The token is passed only here, never logged."""
    env = dict(os.environ)
    config = w.config
    if config.auth_type != "runtime":
        return env  # a CLI profile or M2M variables work the same in the worker
    scheme, _, token = config.authenticate().get("Authorization", "").partition(" ")
    if scheme != "Bearer" or not token:
        raise EvalRunError("the runtime auth gave no bearer token to pass to the evaluation workers")
    for k in _PAT_CONFLICTS:
        env.pop(k, None)
    env["DATABRICKS_HOST"] = config.host
    env["DATABRICKS_TOKEN"] = token
    return env


def _worker_spec(
    cfg, req: RunRequest, run: _SkillRun, *, prompts, fixtures: FixtureStore, index: int
) -> dict:
    """Everything skillhub.eval.worker needs to rebuild this skill's `_run_skill` call."""
    key = f"{run.scope.dept}/{run.skill.name}"
    narrowed = dataclasses.replace(req, skills=[key], versions={key: run.version} if run.pinned else None)
    return {
        # As loaded, so the worker needs no --config path and resolves no ${VARS} again.
        "config": cfg.model_dump(mode="json", by_alias=True),
        "request": dataclasses.asdict(narrowed),
        "dept": run.scope.dept,
        "skill": run.skill.name,
        "scope": dataclasses.asdict(run.scope),
        # The worker reads evals.json again and refuses other bytes. The digest is of the fixture snapshot.
        "models": run.plan.models,
        "evals_sha256": hashlib.sha256(run.plan.evals_bytes).hexdigest(),
        "dataset_digest": run.plan.dataset_digest,
        "exp_id": run.exp_id,
        "parent_id": run.parent_id,
        "prompts": list(prompts),
        "version": run.version,
        "fixtures": str(fixtures.root),
        "index": index,
    }


def _read_worker_result(path: Path) -> tuple[list[ChildRunSummary], list[RunError]]:
    got = json.loads(path.read_text(encoding="utf-8"))
    return [ChildRunSummary(**c) for c in got["children"]], [RunError(**e) for e in got["errors"]]


def _run_in_worker(
    cfg, req: RunRequest, run: _SkillRun, workdir: Path, index: int, *, prompts, fixtures, w, procs: _Workers
) -> tuple[list[ChildRunSummary], list[RunError]]:
    """One skill in a worker process. A worker that fails or leaves no result is one error of the skill."""
    label = f"{run.scope.dept}/{run.skill.name}"
    spec, out = workdir / f"{index}.spec.json", workdir / f"{index}.out.json"
    try:
        body = _worker_spec(cfg, req, run, prompts=prompts, fixtures=fixtures, index=index)
        spec.write_text(json.dumps(body), "utf-8")
        code, tail = _launch_worker(_worker_command(spec, out), _worker_env(w), label, procs)
    except Exception as e:
        code, tail = None, _describe(e)
    if code == 0:
        try:
            return _read_worker_result(out)
        except (OSError, ValueError, TypeError, KeyError):
            pass  # no valid result
    prefix = "evaluation worker failed: "
    text = _run_names(cfg, run.scope).to_production(tail or f"exit code {code}")
    return [], [RunError(run.scope.dept, run.skill.name, prefix + text[-(ERROR_MAX - len(prefix)) :])]


def _run_in_workers(
    cfg, req: RunRequest, runs: list[_SkillRun], *, prompts, fixtures, w
) -> list[tuple[list[ChildRunSummary], list[RunError]]]:
    workdir = Path(tempfile.mkdtemp(prefix="skillhub-workers-"))
    procs = _Workers()
    pool = ThreadPoolExecutor(
        max_workers=min(cfg.defaults.skill_workers, len(runs)), thread_name_prefix="skill-worker"
    )
    try:
        with _sigterm_raises():
            futures = [
                pool.submit(
                    _run_in_worker,
                    cfg,
                    req,
                    r,
                    workdir,
                    i,
                    prompts=prompts,
                    fixtures=fixtures,
                    w=w,
                    procs=procs,
                )
                for i, r in enumerate(runs)
            ]
            _wait_all(futures)
    except BaseException:  # an interrupt: stop the workers, then the caller's cleanup runs
        procs.stop(WORKER_STOP_S)
        raise
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        shutil.rmtree(workdir, ignore_errors=True)
    return [f.result() for f in futures]


def _open_parent(req: RunRequest, scope: RunScope, exp_id: str, prompt_versions: dict[str, int]) -> str:
    """The department's parent run.

    It records every in-scope skill's staged FQN, content hash and etag, because the skills server reports
    no version."""
    client = MlflowClient()
    tags = {
        "run_key": req.run_key,
        "dept": scope.dept,
        "kind": "parent",
        "source": req.source,
        "git_sha": _git_sha(req.source),
        "trigger_source": req.trigger_source,
        "requested_by": req.requested_by or "",
        "prompt_versions": json.dumps(prompt_versions, sort_keys=True),
    }
    run = client.create_run(
        experiment_id=exp_id,
        run_name=f"{req.run_key}:{scope.dept}",
        tags={k: v for k, v in tags.items() if v},
    )
    scope_record = {"schema": scope.schema_fqn, "skills": [dataclasses.asdict(s) for s in scope.skills]}
    client.log_dict(run.info.run_id, scope_record, "run_scope.json")
    return run.info.run_id


def _close_parents(parents: dict[str, str], ok: bool) -> None:
    client = MlflowClient()
    for run_id in parents.values():
        try:
            client.set_terminated(run_id, "FINISHED" if ok else "FAILED")
        except Exception:
            log.warning("could not end parent run %s", run_id, exc_info=True)


def _load_evals(bundle: SkillBundle) -> tuple[EvalFile, bytes]:
    """evals.json, read once: the cases and the bytes the dataset digest covers."""
    path = bundle.root / "evals" / "evals.json"
    try:
        ef, data = read_evals(path)
    except (OSError, ValueError) as e:
        raise EvalRunError(f"{bundle.dept}/{bundle.name}: cannot read {path}: {e}") from e
    return ef, data  # EvalFile refuses repeated case ids at load, so cases can be keyed by id


def _load_rubric(bundle: SkillBundle, *, required: bool) -> Rubric | None:
    """The rubric scores the effectiveness modes. A trigger-only run records it when there is one."""
    path = bundle.root / "evals" / "rubric.json"
    try:
        return load_rubric(path)
    except FileNotFoundError as e:
        if not required:
            return None
        raise EvalRunError(
            f"{bundle.dept}/{bundle.name}: {path} is missing; run `skillhub generate --rubric-only`"
        ) from e
    except (OSError, ValueError) as e:
        raise EvalRunError(f"{bundle.dept}/{bundle.name}: cannot read {path}: {e}") from e


def _describe(e: Exception) -> str:
    return str(e) if isinstance(e, EvalRunError) else f"{type(e).__name__}: {e}"


def _run_names(cfg, scope: RunScope) -> ToolNameMap:
    """Every staged FQN in the scope reads as its production FQN, and the bare run schema as the department's
    schema."""
    names = ToolNameMap()
    rewrite = {scope.schema_fqn: live_schema(cfg, scope.dept)}
    rewrite.update({s.staged_fqn: s.production_fqn for s in scope.skills})
    names.set_fqn_rewrite(rewrite)
    return names


def _run_skill(
    cfg,
    req: RunRequest,
    plan: _SkillPlan,
    scope: RunScope,
    skill: StagedSkill,
    *,
    clients: dict[str, Any],
    judges: list[str],
    exp_id: str,
    parent_id: str,
    prompts: tuple[str, dict[str, int]],
    version: int | None,
    fixtures: FixtureStore,
    sql,
    index: int = 0,
) -> tuple[list[ChildRunSummary], list[RunError]]:
    """Every child run of one skill: per model, its modes in order (trigger, without_skill, with_skill).

    The models run one after another, starting at model `index % len(models)`: `index` is the skill's place in
    the run, so skills that start together begin on different endpoints. Children come back in plan order.

    The setup every child needs (rubric, dataset rows, scorers) is done first. If it fails, no child run is
    created and the skill is recorded as an error."""
    ef = plan.ef
    names = _run_names(cfg, scope)
    modes = evalspec.run_modes(ef, req.modes)
    skipped = [m for m in _modes_for(req.modes) if m not in modes]
    if skipped:
        log.warning("%s/%s has no cases for %s; skipped", scope.dept, skill.name, ", ".join(skipped))
    if not modes:
        return [], []
    try:
        ctx = _prepare(
            cfg,
            req,
            plan,
            scope,
            skill,
            modes,
            names=names,
            judges=judges,
            exp_id=exp_id,
            parent_id=parent_id,
            prompts=prompts,
            version=version,
            fixtures=fixtures,
            sql=sql,
        )
    except Exception as e:
        message = names.to_production(f"setup failed: {_describe(e)}")[:ERROR_MAX]
        return [], [RunError(scope.dept, skill.name, message)]
    first = index % len(plan.models)
    by_model: dict[str, tuple[list[ChildRunSummary], list[RunError]]] = {}
    # Once per skill: set_experiment resets the process's trace destination (traces follow the skill
    # experiment's trace location), and _evaluating changes os.environ and sys.stdout for every child.
    mlflow.set_experiment(experiment_id=ctx.exp_id)
    with _evaluating(cfg.defaults.concurrency_per_endpoint):
        for profile in plan.models[first:] + plan.models[:first]:
            by_model[profile] = _run_model(ctx, clients[profile], profile, modes)
    children: list[ChildRunSummary] = []
    errors: list[RunError] = []
    for profile in plan.models:
        children += by_model[profile][0]
        errors += by_model[profile][1]
    return children, errors


def _run_model(ctx: _SkillCtx, client, profile: str, modes: list[str]) -> tuple[list, list]:
    children: list[ChildRunSummary] = []
    errors: list[RunError] = []
    without: list[CaseScore] | None = None  # with_skill reads this model's without_skill scores
    for mode in modes:
        child, scores, failed = _run_child(ctx, client, profile, mode, without)
        if mode == "without_skill":
            without = scores
        if child is not None:
            children.append(child)
        errors += failed
    return children, errors


def _prepare(
    cfg, req, plan, scope, skill, modes, *, names, judges, exp_id, parent_id, prompts, version, fixtures, sql
) -> _SkillCtx:
    label = f"{scope.dept}/{skill.name}"
    ef = plan.ef
    rubric = _load_rubric(plan.bundle, required=any(m != "trigger" for m in modes))
    if rubric is not None and rubric.skill_name != skill.name:  # It would be recorded as this skill's
        raise EvalRunError(f"{label}: rubric.json is for skill {rubric.skill_name!r}, not {skill.name!r}")
    if rubric is not None and rubric.source_hash != skill.content_hash:
        log.warning("%s: rubric.json was generated from other content than the skill under test", label)
    ds, _ = sync_dataset(cfg, scope.dept, skill.name, ef, exp_id)  # the gates bind plan.dataset_digest
    cases = {c.id: c for c in ef.evals}
    data = {
        k: _mode_data(ds, cases, k, skill.name, label) for k in dict.fromkeys(CASE_KIND[m] for m in modes)
    }
    empty = [k for k, md in data.items() if not md.n_records]
    if empty:
        raise EvalRunError(f"{label}: the eval dataset has no current {', '.join(empty)} rows; re-sync it")
    scorers = {m: _scorers(cfg, rubric, judges, m, skill.name, cases, fixtures, label) for m in modes}
    return _SkillCtx(
        cfg=cfg,
        req=req,
        judges=judges,
        scope=scope,
        skill=skill,
        ef=ef,
        cases=cases,
        rubric=rubric,
        thresholds=gate_thresholds(cfg.defaults, ef.criteria, rubric, skill=label),
        data=data,
        scorers=scorers,
        digest=plan.dataset_digest,
        names=names,
        fixtures=fixtures,
        system_prompt=prompts[0],
        prompt_versions=prompts[1],
        exp_id=exp_id,
        parent_id=parent_id,
        version=version,
        sql=sql,
    )


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_dict(value) -> dict | None:
    if isinstance(value, str):  # managed datasets read through Spark hold JSON strings
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def _rows(ds) -> list[dict]:
    if hasattr(ds, "to_df"):
        ds = ds.to_df()
    if hasattr(ds, "to_dict"):
        return ds.to_dict(orient="records")
    return list(ds)


def _canon(value) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _mode_data(ds, cases: dict[int, EvalCase], kind: str, skill: str, label: str = "") -> _ModeData:
    """The records of one case kind (trigger or effectiveness), joined to evals.json on case_id.

    Labels and expectations always come from the evals.json case, never from the dataset row: the shared
    dataset can hold labels from an older or unmerged evals.json, or from a concurrent run. Each kept record is
    rebuilt with `case_record`. A row of another prompt (an edited or removed case) is left out. When every row
    is kept and already equals its rebuilt record, the dataset object itself is evaluated, which keeps MLflow's
    dataset lineage. A case with no row is listed in `missing_ids`; the runner counts it as an error."""
    rows = _rows(ds)
    keep: dict[int, dict] = {}
    stale: list[int] = []
    for row in rows:
        inputs = _as_dict(row.get("inputs")) or {}
        cid = _int(inputs.get("case_id"))
        case = cases.get(cid)
        if case is None or case.kind != kind or case.prompt != inputs.get("prompt") or cid in keep:
            continue
        current = case_record(case, skill)
        if any(_canon(_as_dict(row.get(k)) or {}) != _canon(current[k]) for k in ("expectations", "tags")):
            stale.append(case.id)
        keep[case.id] = current
    missing = sorted(c.id for c in cases.values() if c.kind == kind and c.id not in keep)
    if stale:
        log.warning(
            "%s: dataset rows of %s case(s) %s differ from evals.json; the evals.json labels are used",
            label,
            kind,
            sorted(stale),
        )
    for cid in missing:
        log.warning("%s: %s case %s has no row in the eval dataset; it counts as an error", label, kind, cid)
    records = [keep[cid] for cid in sorted(keep)]
    whole = len(records) == len(rows) and not stale
    return _ModeData(
        data=ds if whole else records,
        case_ids=sorted(keep),
        n_records=len(records),
        missing_ids=missing,
        records=records,
    )


def _repeated(md: _ModeData, repeats: int) -> object:
    """What the child run's one `evaluate` gets: each record `repeats` times.

    Two evaluate calls at once in one process deadlocked inside MLflow (seen live), and one after another is
    slow, so the repeats share one evaluate. With more than one repeat the records are plain dicts, which gives
    up the dataset lineage that a single repeat over the dataset object keeps."""
    if repeats == 1:
        return md.data
    return [copy.deepcopy(r) for _ in range(repeats) for r in md.records]


def _predict_fn(ctx, client, mode: str, sem, skills):
    """`predict` for one evaluate over every repeat. Each call of a case takes that case's next repeat number,
    so each case gets 0..repeats-1 once; `repeat` is not an input, because the judges and sync read the inputs.
    MLflow re-runs a rate-limited record under the same prediction context, so a retry keeps its number."""
    trigger = mode == "trigger"
    lock = threading.Lock()
    handed: dict[int, int] = {}  # case_id -> repeats handed out
    by_record: dict[str, int] = {}  # evaluate's request ID of a record -> its repeat

    def next_repeat(case_id: int) -> int:
        pc = get_prediction_context()
        record = pc.request_id if pc is not None else None
        with lock:  # evaluate calls predict from its worker threads
            if record is not None and record in by_record:
                return by_record[record]
            repeat = handed.get(case_id, 0)
            handed[case_id] = repeat + 1
            if record is not None:
                by_record[record] = repeat
            return repeat

    @mlflow.trace(name="eval_case", span_type=SpanType.CHAIN)
    def predict(prompt: str, case_id: int) -> str:
        case = ctx.cases[int(case_id)]  # the runner resolves each case from evals.json by case_id
        repeat = next_repeat(case.id)
        # Tag first, before any model or tool call that can fail, so an errored trace still has them.
        _tag_current_trace(
            {
                "case_id": str(case.id),
                "repeat": str(repeat),
                "mode": mode,
                "reviewed": str(case.reviewed).lower(),
            }
        )
        with sem:
            res = run_agent(
                case,
                model=client,
                skills=skills,
                names=ctx.names,
                fixtures=ctx.fixtures,
                trigger=trigger,
                system_prompt=ctx.system_prompt,
                max_turns=MAX_TURNS,
            )
        if res.error:  # e.g. max_turns: a model fail (the judges score the empty answer), shown on the trace
            _tag_current_trace({"agent_error": res.error})
        return res.final_text

    return predict


def _scorers(cfg, rubric, judges, mode, skill_name, cases, fixtures, label) -> list:
    """Built before any child run starts, so a bad planted-bug manifest is a clear setup error."""
    try:
        return scorers_for(
            cfg, rubric, judges, mode, skill_name=skill_name, cases=cases, fixtures_root=fixtures.root
        )
    except Exception as e:
        raise EvalRunError(f"{label} {mode}: scorers could not be built: {type(e).__name__}: {e}") from e


def _open_skills(ctx: _SkillCtx) -> _CachedSkills:
    try:
        bridge = SkillsBridge(SkillsMcpClient(ctx.cfg.workspace.host, [ctx.scope.schema_fqn], auth_headers))
    except Exception as e:  # the server URL holds the staged schema
        raise RuntimeError(ctx.names.to_production(f"skills server: {type(e).__name__}: {e}")) from None
    return _CachedSkills(bridge, ctx.names)


def _fail_child(run_id: str, message: str) -> VerdictResult:
    """Close a child run that failed after it started so that sync can read it and the gate blocks: verdict
    `inconclusive`, no cases, and an `error` tag (fail closed).

    The verdict is set here, not computed: with `max_error_rate >= 1.0`, an empty run would compute as
    `advisory`, which never gates."""
    metrics = {"pass_rate": 0.0, "spread": 0.0, "error_rate": 1.0, "n_cases": 0.0, "n_reviewed": 0.0}
    result = VerdictResult("inconclusive", 0.0, 0.0, 1.0, 0, 0, metrics)
    client = MlflowClient()
    client.set_tag(run_id, "error", message)
    log_verdict(run_id, result, client=client)
    return result


def _run_child(ctx: _SkillCtx, client, profile: str, mode: str, without_scores: list[CaseScore] | None):
    """One child run. Returns (summary or None, case scores or None, errors)."""
    cfg, req = ctx.cfg, ctx.req
    prof = cfg.models[profile]
    endpoint = prof.endpoint or prof.model or profile
    repeats = cfg.defaults.repeats
    md = ctx.data[CASE_KIND[mode]]
    scorers = ctx.scorers[mode]
    planted_on = any(getattr(s, "name", None) == PLANTED for s in scorers)

    def error(message: str, run_id: str | None = None) -> RunError:
        text = ctx.names.to_production(message)[:ERROR_MAX]
        return RunError(ctx.scope.dept, ctx.skill.name, text, model_profile=profile, mode=mode, run_id=run_id)

    skills = None
    if mode != "without_skill":
        try:
            skills = _open_skills(ctx)  # after set_experiment (in _run_skill), so it cannot leak the bridge
        except Exception as e:  # before the child run exists: nothing is left for sync to read
            return None, None, [error(str(e))]
    params = {
        "skill_fqn": ctx.skill.production_fqn,
        "skill": ctx.skill.name,
        "dept": ctx.scope.dept,
        "skill_version": str(ctx.version or 0),
        "content_hash": ctx.skill.content_hash,
        "rubric_hash": rubric_hash(ctx.rubric) if ctx.rubric is not None else NO_RUBRIC,
        "dataset_digest": ctx.digest,
        "model_profile": profile,
        "endpoint": endpoint,
        "model_class": prof.class_,
        "mode": mode,
        "judges": ",".join(ctx.judges),
        "prompt_versions": json.dumps(ctx.prompt_versions, sort_keys=True),
        "git_sha": _git_sha(req.source),
        "trigger_source": req.trigger_source,
        "requested_by": req.requested_by or "",
        "run_key": req.run_key,
        "repeats": str(repeats),
    }
    try:
        run_id = mlflow.start_run(
            experiment_id=ctx.exp_id, run_name=f"{ctx.skill.name}|{profile}|{mode}"
        ).info.run_id
    except BaseException:
        if skills is not None:
            skills.close()
        raise
    status, errors, scores = "FINISHED", [], None
    try:
        mlflow.log_params(params)
        mlflow.set_tags(
            {
                "run_key": req.run_key,
                "parent_run_id": ctx.parent_id,
                "harness": "reference",
            }  # sync reads these
        )
        if mode != "trigger" and ctx.rubric is not None and ctx.rubric.source_hash != ctx.skill.content_hash:
            mlflow.set_tag("rubric_stale", "true")  # The rubric was generated from other skill content
        sem = _endpoint_semaphore(endpoint, cfg.defaults.concurrency_per_endpoint)
        mlflow.genai.evaluate(  # one evaluate over every repeat; never two at once in a process
            data=_repeated(md, repeats),
            predict_fn=_predict_fn(ctx, client, mode, sem, skills),
            scorers=scorers,
        )
        traces = read_child_traces(
            ctx.exp_id,
            run_id,
            md.n_records * repeats,
            complete=lambda ts: _complete(ctx, ts, mode, md.case_ids, repeats, planted_on),
        )
        scored = _score(ctx, traces, mode, md.case_ids, repeats, planted_on, md.missing_ids)
        t = ctx.thresholds
        result = compute_verdict(
            scored.scores,
            fail_rate=t.fail_rate,
            warn_rate=t.warn_rate,
            max_error_rate=t.max_error_rate,
            min_reviewed_cases=t.min_reviewed_cases,
            judges_responded_ratio=scored.responded_ratio,
        )
        extra: dict[str, float] = {}
        if mode == "trigger":
            extra.update(trigger_metrics(scored.pairs, ctx.skill.name))
        if mode == "with_skill" and without_scores is not None:
            extra.update(uplift(scored.scores, without_scores))
        if scored.planted:
            extra["planted_bugs_recall"] = statistics.fmean(scored.planted)
        log_verdict(run_id, result, extra_metrics=extra, client=MlflowClient())  # verdict tag last
        scores = scored.scores
    except Exception as e:
        status = "FAILED"
        errors.append(error(f"{type(e).__name__}: {e}", run_id))
        result, extra = _fail_child(run_id, errors[-1].message), {}
    except BaseException:  # A Job cancel or interrupt still leaves a verdict, so sync can finish the run
        status = "FAILED"
        try:
            _fail_child(run_id, "interrupted before the verdict was logged")
        except Exception:
            log.warning("could not close interrupted child run %s", run_id, exc_info=True)
        raise
    finally:
        if skills is not None:
            try:
                skills.close()
            except Exception:
                log.warning("could not close the skills bridge of run %s", run_id, exc_info=True)
        mlflow.end_run(status)
    if ctx.rubric is not None:  # Once per child run, after the verdict
        try:
            record_rubric(cfg, ctx.sql, ctx.scope.dept, ctx.rubric)
        except Exception as e:
            errors.append(error(f"record_rubric: {type(e).__name__}: {e}", run_id))
    child = ChildRunSummary(
        run_id=run_id,
        dept=ctx.scope.dept,
        skill=ctx.skill.name,
        skill_version=ctx.version,
        content_hash=ctx.skill.content_hash,
        model_profile=profile,
        mode=mode,  # type: ignore[arg-type]
        verdict=result.verdict,
        metrics={**result.metrics, **extra},
    )
    return child, scores, errors


# --- reading assessments back -------------------------------------------------------------------------------


@dataclass
class _Scored:
    scores: list[CaseScore]
    pairs: list[tuple[str | None, str | None]]  # (expected, invoked) for trigger metrics
    planted: list[float]  # planted-bug recall per workbook case-repeat
    responded_ratio: float


def _is_error(trace) -> bool:
    state = getattr(trace.info, "state", None)
    return getattr(state, "value", state) == "ERROR"


def _failed(a) -> bool:
    return getattr(a, "error", None) is not None


def _latest(trace) -> dict[str, Any]:
    """The newest assessment per name."""
    out: dict[str, Any] = {}
    for a in trace.info.assessments or []:
        cur = out.get(a.name)
        if cur is None or (getattr(a, "create_time_ms", 0) or 0) >= (getattr(cur, "create_time_ms", 0) or 0):
            out[a.name] = a
    return out


def _cells(traces: list, cases: dict[int, EvalCase]) -> dict[tuple[int, int], Any]:
    """One trace per (case_id, repeat).

    MLflow re-runs `predict` on a rate-limit error, and each attempt leaves its own tagged trace; only the
    last attempt is scored. So a trace that did not fail wins, then a scored one, then the newest."""
    best: dict[tuple[int, int], Any] = {}

    def rank(t) -> tuple:
        return (not _is_error(t), bool(t.info.assessments), getattr(t.info, "timestamp_ms", 0) or 0)

    for t in traces:
        tags = t.info.tags or {}
        key = (_int(tags.get("case_id")), _int(tags.get("repeat")))
        if key[0] not in cases or key[1] is None:
            continue
        if key not in best or rank(t) > rank(best[key]):
            best[key] = t
    return best


def _vote(a) -> bool | None:
    """A judge's pass/fail. None when the judge failed or gave no usable answer.

    Built-in guideline judges answer "yes"/"no"; rubric judges answer booleans."""
    if a is None or _failed(a):
        return None
    v = a.value
    if isinstance(v, bool):
        return v
    text = str(v).strip().lower() if v is not None else ""
    return True if text in _YES else False if text in _NO else None


def _recall(a) -> float | None:
    if a is None or _failed(a) or a.value is None:
        return None
    try:
        value = float(a.value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _invoked(a) -> Any:
    """The first invoked skill name recorded by the trigger scorer, None for no skill, or _NOT_RECORDED.

    Assessment metadata holds strings on Databricks, where a missing skill can read as "None"."""
    metadata = getattr(a, "metadata", None) or {}
    if "invoked_skill" not in metadata:
        return _NOT_RECORDED
    name = metadata["invoked_skill"]
    return None if name is None or str(name) in _NO_SKILL else str(name)


def _trigger_outcome(a, expected: str | None) -> tuple[str | None, Any]:
    """(outcome, invoked) for one trigger cell. The outcome compares the evals.json label with the invoked skill,
    so a stale label in the assessment or the dataset row never decides it. No usable assessment: (None, _)."""
    if a is None or _failed(a) or a.value is None:
        return None, _NOT_RECORDED
    invoked = _invoked(a)
    if invoked is _NOT_RECORDED:
        return None, invoked
    return case_outcome(expected, invoked).value, invoked


def _judge_names(ctx: _SkillCtx, case: EvalCase) -> list[str]:
    names = [judge_name(f"rubric_{c.id}", p) for c in ctx.rubric.criteria for p in ctx.judges]
    if case.assertions:
        names += [judge_name("assertions", p) for p in ctx.judges]
    return names


def _planted_recall(ctx: _SkillCtx, trace) -> float | None:
    """The planted-bug recall of one trace, computed from its final answer: the scorer's own code.

    None when the trace holds no spans to read, or the case is misconfigured (an unknown bug_scope id)."""
    if getattr(trace, "data", None) is None:
        return None
    fb = planted_bugs_check(trace, ctx.cases, _planted_bugs(str(ctx.fixtures.root)))
    return None if fb is None or fb.value is None else float(fb.value)


@functools.lru_cache(maxsize=8)
def _planted_bugs(root: str) -> list[dict]:
    return load_planted_bugs(Path(root))


def _planted_case(case: EvalCase, planted_on: bool) -> bool:
    return planted_on and PLANTED_FIXTURE in case.fixtures  # Workbook cases only


def _ready(ctx: _SkillCtx, trace, mode: str, planted_on: bool) -> bool:
    """A trace is final when every assessment the verdict reads has landed.

    A failed trace is final once it is scored (the final attempt always carries the dataset expectations); a
    failed attempt that MLflow retried has no assessments."""
    got = {a.name for a in trace.info.assessments or []}
    if _is_error(trace):
        return bool(got)
    case = ctx.cases[_int(trace.info.tags["case_id"])]
    if mode == "trigger":
        needed = {TRIGGER}
    else:
        needed = set(_judge_names(ctx, case))  # the planted-bug recall is computed here, from the trace
    return needed <= got


def _complete(
    ctx: _SkillCtx, traces: list, mode: str, case_ids: list[int], repeats: int, planted_on: bool
) -> bool:
    """Every expected (case, repeat) has a final trace. A retry's extra trace never fills another cell."""
    cells = _cells(traces, ctx.cases)
    return all(
        (cid, rep) in cells and _ready(ctx, cells[(cid, rep)], mode, planted_on)
        for cid in case_ids
        for rep in range(repeats)
    )


def _score(
    ctx: _SkillCtx,
    traces: list,
    mode: str,
    case_ids: list[int],
    repeats: int,
    planted_on: bool,
    missing_ids: list[int] = (),
) -> _Scored:
    """One CaseScore per (case, repeat) of the mode, including the cases in `missing_ids` (no dataset row).

    A lost trace, a failed one, or a case that could not be evaluated is an error, never a fail."""
    cells = _cells(traces, ctx.cases)
    items: list[tuple[CaseScore, tuple | None, float | None]] = []
    responded = total = 0
    for cid in sorted({*case_ids, *missing_ids}):
        case = ctx.cases[cid]
        for rep in range(repeats):
            t = None if cid in missing_ids else cells.get((cid, rep))
            if t is None or _is_error(t):
                items.append((trigger_case_score(cid, rep, case.reviewed, None), None, None))
                continue
            got = _latest(t)
            if mode == "trigger":
                expected = case.resolved_expected(ctx.skill.name)  # the evals.json label
                outcome, invoked = _trigger_outcome(got.get(TRIGGER), expected)
                score = trigger_case_score(cid, rep, case.reviewed, outcome)
                items.append((score, None if score.error else (expected, invoked), None))
                continue
            names = _judge_names(ctx, case)
            total += len(names)
            responded += sum(1 for n in names if _vote(got.get(n)) is not None)
            votes = {
                c.id: [_vote(got.get(judge_name(f"rubric_{c.id}", p))) for p in ctx.judges]
                for c in ctx.rubric.criteria
            }
            assertion_votes = (
                [_vote(got.get(judge_name("assertions", p))) for p in ctx.judges] if case.assertions else None
            )
            planted = None
            if _planted_case(case, planted_on):
                planted = _recall(got.get(PLANTED))
                if planted is None:  # MLflow sometimes does not log it; the scorer is code, so recompute it
                    planted = _planted_recall(ctx, t)
            score = case_score_from_assessments(
                case_id=cid,
                repeat=rep,
                reviewed=case.reviewed,
                criteria=ctx.rubric.criteria,
                votes_by_criterion=votes,
                assertion_votes=assertion_votes,
                pass_score=ctx.thresholds.pass_score,
                error=_planted_case(case, planted_on) and planted is None,  # a failed planted-bug score
            )
            items.append((score, None, planted))
    scores = [s for s, _, _ in items]
    counted = {id(s) for s in gating_scores(scores)}  # the verdict's population: reviewed, not errored
    return _Scored(
        scores=scores,
        pairs=[p for s, p, _ in items if p is not None and id(s) in counted],
        planted=[v for s, _, v in items if v is not None and id(s) in counted],
        responded_ratio=responded / total if total else 1.0,
    )
