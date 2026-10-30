from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import mlflow
from mlflow import MlflowClient

from skillhub.config import HubConfig
from skillhub.evalspec import Rubric, rubric_hash
from skillhub.naming import SKILL_RE, evals_schema, experiment_parts
from skillhub.results.experiments import configure_mlflow, skill_experiments, trusted_run_authors

if TYPE_CHECKING:
    from skillhub.sql import SqlClient

log = logging.getLogger(__name__)
DEFAULT_HARNESS = "reference"  # the built-in agent loop; the only harness so far
PARENT_TAG = "parent_run_id"  # The runner sets it on each child run
POLL_S = 5
STALL_S = 60  # how long a scored run's trace count may stay unchanged before sync writes what it has
CHUNK = 500

RUN_FIELDS: list[tuple[str, str]] = [
    ("run_id", "string"),
    ("parent_run_id", "string"),
    ("run_key", "string"),
    ("experiment_id", "string"),
    ("dept", "string"),
    ("skill", "string"),
    ("skill_fqn", "string"),
    ("skill_version", "int"),
    ("content_hash", "string"),
    ("rubric_hash", "string"),
    ("dataset_digest", "string"),
    ("model_profile", "string"),
    ("harness", "string"),
    ("endpoint", "string"),
    ("model_class", "string"),
    ("mode", "string"),
    ("judges", "string"),
    ("prompt_versions", "string"),
    ("git_sha", "string"),
    ("trigger_source", "string"),
    ("requested_by", "string"),
    ("verdict", "string"),
    ("pass_rate", "double"),
    ("spread", "double"),
    ("error_rate", "double"),
    ("n_cases", "int"),
    ("n_reviewed", "int"),
    ("accuracy", "double"),
    ("precision", "double"),
    ("recall", "double"),
    ("pass_rate_without", "double"),
    ("uplift_task_quality", "double"),
    ("uplift_skill_format", "double"),
    ("planted_bugs_recall", "double"),
    ("started_at", "timestamp"),
    ("ended_at", "timestamp"),
    ("synced_at", "timestamp"),
    ("run_author", "string"),  # see run_author(); the gates read only trusted authors
]
CASE_FIELDS: list[tuple[str, str]] = [
    ("run_id", "string"),
    ("trace_id", "string"),
    ("assessment_name", "string"),
    ("case_id", "int"),
    ("repeat", "int"),
    ("mode", "string"),
    ("user_input", "string"),
    ("expected_skill", "string"),
    ("invoked_skill", "string"),
    ("trigger_outcome", "string"),
    ("value", "string"),
    ("rationale", "string"),
    ("judge_model", "string"),
    ("source_type", "string"),
    ("reviewed", "boolean"),
    ("created_at", "timestamp"),
]
RUBRIC_FIELDS: list[tuple[str, str]] = [
    ("skill", "string"),
    ("rubric_hash", "string"),
    ("rubric_json", "string"),
    ("source_hash", "string"),
    ("reviewed", "boolean"),
    ("ts", "timestamp"),
]
RUN_KEYS = ["run_id"]
CASE_KEYS = ["run_id", "trace_id", "assessment_name", "source_type"]  # insert key
RUBRIC_KEYS = ["rubric_hash"]


class SyncIncomplete(RuntimeError):
    """A run is not finished (no verdict yet) or its inputs are not logged. Nothing is written for it."""


@dataclass
class SyncReport:
    run_key: str
    runs_inserted: int
    cases_inserted: int
    waited_seconds: float


# Where the runner writes each child-run field (eval/runner.py `_run_child`, eval/verdict.py `log_verdict`).
# Sync reads each field only from there: a param cannot change once logged, a tag can, so a value found
# in the other place was planted. run_key is written as both, and the two must agree.
CHILD_TAGS = frozenset({"run_key", PARENT_TAG, "harness", "verdict", "error", "rubric_stale"})
CHILD_PARAMS = frozenset(
    {
        "skill_fqn",
        "skill",
        "dept",
        "skill_version",
        "content_hash",
        "rubric_hash",
        "dataset_digest",
        "model_profile",
        "endpoint",
        "model_class",
        "mode",
        "judges",
        "prompt_versions",
        "git_sha",
        "trigger_source",
        "requested_by",
        "run_key",
        "repeats",
    }
)


def _tag(run, key: str) -> str | None:
    """A field the runner writes as a tag. Read from the tags only."""
    return (run.data.tags or {}).get(key)


def _param(run, key: str) -> str | None:
    """A field the runner writes as a param. Read from the params only."""
    return (run.data.params or {}).get(key)


def tampering(run) -> str | None:
    """Why a child run cannot be trusted although its author is trusted, or None.

    Anyone with edit rights on the experiment can add params and tags to a finished run. A runner tag logged
    as a param (for example `verdict=pass` next to the runner's `verdict=fail` tag), a runner param set as a
    tag, or a run_key tag that differs from the run_key param means someone else wrote to the run."""
    tags = run.data.tags or {}
    params = run.data.params or {}
    reasons = []
    as_params = sorted(set(params) & (CHILD_TAGS - CHILD_PARAMS))
    if as_params:
        reasons.append(f"runner tag(s) logged as params: {', '.join(as_params)}")
    as_tags = sorted(set(tags) & (CHILD_PARAMS - CHILD_TAGS))
    if as_tags:
        reasons.append(f"runner param(s) set as tags: {', '.join(as_tags)}")
    if params.get("run_key") != tags.get("run_key"):
        reasons.append("the run_key tag differs from the run_key param")
    return "; ".join(reasons) or None


def _ts(ms: int | None) -> str | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()


def _home_matches(run, home: tuple[str, str | None] | None) -> bool:
    """Whether the run's dept and skill params match its experiment. The department always comes from the
    experiment, so a run cannot write into another department's tables. The unreleased experiment holds the skills
    of its department that have no experiment of their own: its runs name their skill, which must be a valid name.
    Every experiment of a department has the same ACL (derived from that department's grants), so this lets no
    one write anything they could not already write to the skill's own experiment."""
    if home is None or _param(run, "dept") != home[0]:
        return False
    skill = _param(run, "skill")
    if home[1] is None:
        return isinstance(skill, str) and SKILL_RE.fullmatch(skill) is not None
    return skill == home[1]


def child_runs(cfg: HubConfig, run_key: str, *, trusted: frozenset[str] | None = None) -> list[Any]:
    """Trusted child runs (one per skill x model x mode) for a run_key.

    The search runs department by department over this catalog's skill experiments. The parent run
    also carries run_key, but it has no parent_run_id tag, so it's skipped. These are skipped with a
    WARNING and counted, never raised: an experiment that is not <root>/<dept>/<skill> for a configured
    department, a run whose author (`run_author`) is not in `trusted` (default:
    trusted_run_authors()), a run that someone else wrote to (`tampering`), and a run whose dept or skill differs
    from its experiment's. A run in the department's unreleased experiment names its skill (`_home_matches`).
    """
    if not re.fullmatch(r"[a-z0-9_]+", run_key):
        raise ValueError(f"invalid run_key {run_key!r}")
    trusted = trusted_run_authors() if trusted is None else trusted
    home: dict[str, tuple[str, str | None]] = {}  # experiment_id -> (dept, skill or None when unreleased)
    by_dept: dict[str, list[str]] = {}
    for e in skill_experiments(cfg):
        try:
            dept, skill = experiment_parts(cfg, e.name)
        except ValueError:
            log.warning("skipping experiment %s: not <experiment root>/<dept>/<skill>", e.name)
            continue
        if dept not in cfg.departments:
            log.warning("skipping experiment %s: unknown department %r", e.name, dept)
            continue
        home[e.experiment_id] = (dept, skill)
        by_dept.setdefault(dept, []).append(e.experiment_id)
    client = MlflowClient()
    found: list[Any] = []
    untrusted = mismatched = tampered = 0
    for experiment_ids in by_dept.values():
        token = None
        while True:
            page = client.search_runs(
                experiment_ids=experiment_ids,
                filter_string=f"tags.run_key = '{run_key}'",
                max_results=1000,
                page_token=token,
            )
            for r in page:
                if PARENT_TAG not in (r.data.tags or {}):
                    continue
                if run_author(r) not in trusted:
                    untrusted += 1
                elif (why := tampering(r)) is not None:
                    tampered += 1
                    log.warning("run_key %s: skipped tampered child run %s: %s", run_key, r.info.run_id, why)
                elif not _home_matches(r, home.get(r.info.experiment_id)):
                    mismatched += 1
                else:
                    found.append(r)
            token = page.token
            if not token:
                break
    if untrusted or mismatched or tampered:
        log.warning(
            "run_key %s: skipped %d child runs by untrusted authors (trusted: %s), %d tampered, and %d whose "
            "dept or skill differs from their experiment",
            run_key,
            untrusted,
            sorted(trusted),
            tampered,
            mismatched,
        )
    return found


def _traces(run) -> list:
    return mlflow.search_traces(
        locations=[run.info.experiment_id], run_id=run.info.run_id, return_type="list"
    )


def _expected_cells(run) -> int:
    """Number of (case_id, repeat) cells the run should have. A mid-run-failed child has n_cases 0."""
    params = run.data.params or {}
    metrics = run.data.metrics or {}
    if "n_cases" not in metrics:
        raise SyncIncomplete(f"run {run.info.run_id}: 'n_cases' metric not logged yet")
    n_cases = int(float(metrics["n_cases"]))
    if n_cases == 0:
        return 0
    if "repeats" not in params:
        raise SyncIncomplete(f"run {run.info.run_id}: 'repeats' param not logged yet")
    return n_cases * int(params["repeats"])


def _is_error(trace) -> bool:
    state = getattr(trace.info, "state", None)
    return getattr(state, "value", state) == "ERROR"


def _cell(trace) -> tuple[str, str] | None:
    tags = trace.info.tags or {}
    if "case_id" in tags and "repeat" in tags and "mode" in tags:
        return str(tags["case_id"]), str(tags["repeat"])
    return None


def _ready_cells(traces: list) -> tuple[set, set]:
    """(ready cells, seen cells). MLflow predict retries leave extra ERROR traces for one cell, so
    traces are grouped by their (case_id, repeat) tags. A cell is ready when any of its traces has
    assessments, or when every one of its traces is ERROR (failed cases may never be judged). An
    ERROR retry next to an OK trace that is not scored yet is not ready."""
    by_cell: dict[tuple[str, str], list] = {}
    for t in traces:
        cell = _cell(t)
        if cell is not None:
            by_cell.setdefault(cell, []).append(t)
    ready = {
        cell
        for cell, ts in by_cell.items()
        if any(t.info.assessments for t in ts) or all(_is_error(t) for t in ts)
    }
    return ready, set(by_cell)


def _usable(traces: list) -> tuple[list, int]:
    """Traces that carry case_id, repeat and mode, and how many were dropped for lacking them."""
    keep = [t for t in traces if all(k in (t.info.tags or {}) for k in ("case_id", "repeat", "mode"))]
    return keep, len(traces) - len(keep)


def run_author(run) -> str | None:
    """Who created the run. Databricks returns no `run.info.user_id`, so the MLflow client's `mlflow.user` tag
    is the fallback. A client sets that tag, so it is only as trustworthy as write access to the experiment:
    bootstrap restricts the experiment folder to the ci, ci_eval and app principals."""
    author = getattr(run.info, "user_id", None)
    if author:
        return author
    tags = getattr(run.data, "tags", None) or {}
    return tags.get("mlflow.user") or None


def wait_for_traces(run, timeout_s: int, *, allow_partial: bool = False) -> tuple[list, float]:
    """Poll until every expected (case_id, repeat) cell has a ready trace.

    Rows land about 20 s after the run. Traces can arrive before their assessments, and a run row
    marks the run complete, so both conditions are required (ruling 17). On timeout this raises
    SyncIncomplete, unless allow_partial: then it logs a WARNING naming the missing cells and
    returns the tagged traces that are available.
    """
    expected = _expected_cells(run)
    start = time.monotonic()
    progress, changed_at = None, start
    while True:
        traces = _traces(run)
        ready, seen = _ready_cells(traces)
        if len(ready) >= expected:
            return traces, time.monotonic() - start
        now = time.monotonic()
        if (len(ready), len(seen), len(traces)) != progress:
            progress, changed_at = (len(ready), len(seen), len(traces)), now
        # A run with a verdict has finished evaluating; once nothing changes for STALL_S, the missing cells
        # are not coming (seen live: an OK trace whose assessment was never written).
        stalled = allow_partial and now - changed_at >= STALL_S
        if stalled or now - start >= timeout_s:
            waited = int(now - start)
            waiting = sorted(seen - ready)
            detail = (
                f"{len(ready)} of {expected} cells ready after {waited}s; "
                f"{expected - len(ready)} missing (traces without assessments: {waiting}, "
                f"cells with no trace: {max(expected - len(seen), 0)})"
            )
            if not allow_partial:
                raise SyncIncomplete(f"run {run.info.run_id}: {detail}")
            keep, dropped = _usable(traces)
            log.warning(
                "run %s scored (verdict logged) but %s; %d untagged traces dropped; syncing available "
                "rows, re-run sync to pick up late cases",
                run.info.run_id,
                detail,
                dropped,
            )
            return keep, time.monotonic() - start
        time.sleep(POLL_S)


def _source(a) -> str | None:
    return a.source.source_type if getattr(a, "source", None) else None


def latest_assessments(trace) -> list:
    """Repeated annotation rows collapse to the latest row per (name, source type)."""
    latest: dict[tuple, Any] = {}
    for a in trace.info.assessments or []:
        key = (a.name, _source(a))
        cur = latest.get(key)
        if cur is None or (a.create_time_ms or 0) >= (cur.create_time_ms or 0):
            latest[key] = a
    return list(latest.values())


def _value(a) -> str | None:
    v = a.value
    if _source(a) == "HUMAN" and v in ("pass", "fail"):
        v = v == "pass"  # the Review App stores pass/fail; judges store booleans
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, dict)):
        return json.dumps(v, default=str)  # JSON, so readers can parse it; str() would give a Python repr
    return None if v is None else str(v)


def _prompt(trace) -> str | None:
    try:
        return json.loads(trace.data.request).get("prompt")
    except (TypeError, ValueError, AttributeError):
        return trace.info.request_preview


def _skill_meta(meta: dict, key: str) -> str | None:
    """A skill name from assessment metadata. MLflow keeps metadata as strings, so a missing skill reads "None"."""
    v = meta.get(key)
    return None if v in (None, "", "None") else str(v)


def _invoked(trace, assessments) -> str | None:
    trig = next((a for a in assessments if a.name == "skill_trigger"), None)
    meta = (trig.metadata or {}) if trig else {}
    if _skill_meta(meta, "invoked_skill"):
        return _skill_meta(meta, "invoked_skill")
    for s in trace.data.spans or []:
        fqn = (s.attributes or {}).get("skill.fqn") if s.span_type == "TOOL" else None
        if fqn:
            return fqn.split(".")[-1]
    return None


def case_rows(run_id: str, traces: list) -> list[dict]:
    rows: list[dict] = []
    for t in traces:
        tags = t.info.tags or {}
        missing = [k for k in ("case_id", "repeat", "mode") if k not in tags]
        if missing:
            raise SyncIncomplete(f"trace {t.info.trace_id}: tags not logged yet: {', '.join(missing)}")
        assessments = latest_assessments(t)
        trig = next((a for a in assessments if a.name == "skill_trigger"), None)
        outcome = _value(trig) if trig else None
        expected = _skill_meta(trig.metadata or {}, "expected_skill") if trig else None
        invoked = _invoked(t, assessments)
        for a in assessments:
            rows.append(
                {
                    "run_id": run_id,
                    "trace_id": t.info.trace_id,
                    "assessment_name": a.name,
                    "case_id": int(tags["case_id"]),
                    "repeat": int(tags["repeat"]),
                    "mode": tags["mode"],
                    "user_input": _prompt(t),
                    "expected_skill": expected,
                    "invoked_skill": invoked,
                    "trigger_outcome": outcome,
                    "value": _value(a),
                    "rationale": a.rationale,
                    "judge_model": a.name.split("__", 1)[1] if "__" in a.name else None,
                    "source_type": _source(a),
                    "reviewed": str(tags.get("reviewed", "false")).lower() == "true",
                    "created_at": _ts(a.create_time_ms),
                }
            )
    return rows


def run_row(run, synced_at: str) -> dict:
    m = run.data.metrics or {}

    def fl(k):
        v = m.get(k)
        return None if v is None else float(v)

    def it(v):
        return None if v in (None, "") else int(float(v))

    return {
        "run_id": run.info.run_id,
        "parent_run_id": _tag(run, PARENT_TAG),
        "run_key": _tag(run, "run_key"),  # equal to the param: child_runs skips a run where they differ
        "experiment_id": run.info.experiment_id,
        "dept": _param(run, "dept"),
        "skill": _param(run, "skill"),
        "skill_fqn": _param(run, "skill_fqn"),
        "skill_version": it(_param(run, "skill_version")),
        "content_hash": _param(run, "content_hash"),
        "rubric_hash": _param(run, "rubric_hash"),
        "dataset_digest": _param(run, "dataset_digest"),
        "model_profile": _param(run, "model_profile"),
        "harness": _tag(run, "harness") or DEFAULT_HARNESS,  # A run without the tag is a reference run
        "endpoint": _param(run, "endpoint"),
        "model_class": _param(run, "model_class"),
        "mode": _param(run, "mode"),
        "judges": _param(run, "judges"),
        "prompt_versions": _param(run, "prompt_versions"),
        "git_sha": _param(run, "git_sha"),
        "trigger_source": _param(run, "trigger_source"),
        "requested_by": _param(run, "requested_by"),
        "verdict": _tag(run, "verdict"),
        "pass_rate": fl("pass_rate"),
        "spread": fl("spread"),
        "error_rate": fl("error_rate"),
        "n_cases": it(m.get("n_cases")),
        "n_reviewed": it(m.get("n_reviewed")),
        "accuracy": fl("accuracy"),
        "precision": fl("precision"),
        "recall": fl("recall"),
        "pass_rate_without": fl("pass_rate_without"),
        "uplift_task_quality": fl("uplift_task_quality"),
        "uplift_skill_format": fl("uplift_skill_format"),
        "planted_bugs_recall": fl("planted_bugs_recall"),
        "started_at": _ts(run.info.start_time),
        "ended_at": _ts(run.info.end_time),
        "synced_at": synced_at,
        "run_author": run_author(run),
    }


def insert_missing(
    sql: SqlClient, ev: str, table: str, fields: list[tuple[str, str]], rows: list[dict], keys: list[str]
) -> int:
    """INSERT ... SELECT ... WHERE NOT EXISTS. Rows travel as one JSON parameter per batch."""
    if not rows:
        return 0
    struct = ",".join(f"`{n}`:{t}" for n, t in fields)
    cols = ", ".join(f"r.`{n}`" for n, _ in fields)
    names = ", ".join(f"`{n}`" for n, _ in fields)
    join = " AND ".join(f"x.`{k}` <=> r.`{k}`" for k in keys)  # null-safe
    stmt = (
        f"INSERT INTO {ev}.{table} ({names}) SELECT {cols} "
        f"FROM (SELECT explode(from_json(:rows, 'array<struct<{struct}>>')) AS r) "
        f"WHERE NOT EXISTS (SELECT 1 FROM {ev}.{table} AS x WHERE {join})"
    )
    done = 0
    for i in range(0, len(rows), CHUNK):
        res = sql.execute(stmt, {"rows": json.dumps(rows[i : i + CHUNK])})
        done += int(res[0]["num_affected_rows"])
    return done


def _count_runs(sql: SqlClient, ev: str, run_id: str) -> int:
    res = sql.execute(f"SELECT COUNT(*) AS n FROM {ev}.eval_runs WHERE run_id = :rid", {"rid": run_id})
    return int(res[0]["n"])


def sync_run(
    cfg: HubConfig,
    sql: SqlClient,
    run_key: str,
    *,
    timeout_s: int = 300,
    rebuild: bool = False,
    trusted: frozenset[str] | None = None,
) -> SyncReport:
    """Read a run back from MLflow into <d>_evals. Insert-only and idempotent.

    Cases are written before the run row. For an existing run row only missing case rows are added. A child with a
    verdict tag is authoritative: if cells are still missing after the timeout, the available rows are
    written with a WARNING instead of blocking the run key forever. Only trusted child runs are read (child_runs).
    """
    configure_mlflow(cfg)
    synced_at = datetime.now(UTC).isoformat()
    runs_inserted = cases_inserted = 0
    waited = 0.0
    runs = child_runs(cfg, run_key, trusted=trusted)
    if not runs:
        raise SyncIncomplete(f"no trusted child runs for run_key {run_key}")
    for run in runs:
        dept = _param(run, "dept")  # child_runs checked it against the experiment's department
        if _tag(run, "verdict") is None:  # verdict is logged last: still writing
            raise SyncIncomplete(f"run {run.info.run_id}: verdict not logged yet")
        ev = evals_schema(cfg, dept)
        if not rebuild and _count_runs(sql, ev, run.info.run_id) > 0:
            # Run row stays as it is. Traces that arrived after a partial write still get case rows;
            # the case key dedupes the rest.
            traces, _ = _usable(_traces(run))
            cases_inserted += insert_missing(
                sql, ev, "eval_cases", CASE_FIELDS, case_rows(run.info.run_id, traces), CASE_KEYS
            )
            continue
        traces, secs = wait_for_traces(run, timeout_s, allow_partial=True)
        waited += secs
        cases_inserted += insert_missing(
            sql, ev, "eval_cases", CASE_FIELDS, case_rows(run.info.run_id, traces), CASE_KEYS
        )
        runs_inserted += insert_missing(sql, ev, "eval_runs", RUN_FIELDS, [run_row(run, synced_at)], RUN_KEYS)
    return SyncReport(
        run_key=run_key,
        runs_inserted=runs_inserted,
        cases_inserted=cases_inserted,
        waited_seconds=round(waited, 1),
    )


def record_rubric(cfg: HubConfig, sql: SqlClient, dept: str, rubric: Rubric) -> None:
    """Insert the rubric into <d>_evals.rubrics unless its rubric_hash is already recorded.

    The table is insert-only. Rubrics stay out of UC skill bundles, so the agent under test can't read them.
    """
    row = {
        "skill": rubric.skill_name,
        "rubric_hash": rubric_hash(rubric),
        "rubric_json": json.dumps(rubric.model_dump(mode="json"), sort_keys=True, separators=(",", ":")),
        "source_hash": rubric.source_hash,
        "reviewed": rubric.reviewed,
        "ts": datetime.now(UTC).isoformat(),
    }
    insert_missing(sql, evals_schema(cfg, dept), "rubrics", RUBRIC_FIELDS, [row], RUBRIC_KEYS)
