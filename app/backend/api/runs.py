from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import Annotated, Literal, TypeVar

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import (
    DatabricksError,
    InvalidParameterValue,
    NotFound,
    PermissionDenied,
    ResourceConflict,
)
from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend import authz, candidates, jobs, queries, skill_ref
from backend.authz import Forbidden, Reason
from backend.data import SqlDenied, UserSql
from backend.deps import get_app_client, get_cfg, get_sql, get_user_client, problem
from skillhub.config import HubConfig
from skillhub.naming import make_run_key, version_fqn

T = TypeVar("T")  # a TypeVar, not PEP 695 syntax: the Databricks Apps runtime is Python 3.11

log = logging.getLogger("skillhub.app.runs")
router = APIRouter(tags=["runs"])
# Journal actions that start or end a switch. The others (published, publish_refused, drift) do not.
SWITCH_ROWS = frozenset({"switch_started", "switch_failed", "switched", "retired"})


class CandidateRunIn(BaseModel):
    """Skills of one candidate upload: the Job takes one skills folder per run."""

    model_config = ConfigDict(extra="ignore")
    run_key: str = Field(min_length=1, max_length=64)  # the candidate folder, for example pr5_c7ecd3e_a1
    skills: list[str] = Field(min_length=1, max_length=25)  # dept/name in that folder


class EvalRunIn(BaseModel):
    model_config = ConfigDict(extra="ignore")  # requested_by and other extras are never read from the body
    skills: list[str] = Field(default_factory=list, max_length=25)  # live skill FQNs
    models: list[str] = Field(min_length=1, max_length=8)
    modes: list[Literal["trigger", "effectiveness"]] = Field(min_length=1, max_length=2)
    versions: dict[str, int] = Field(default_factory=dict, max_length=20)  # skill FQN -> published version
    candidate: CandidateRunIn | None = None

    @model_validator(mode="after")
    def _names_something_to_run(self) -> EvalRunIn:
        if not self.skills and self.candidate is None:
            raise ValueError("name live skills or a candidate")
        return self


class EvalRunOut(BaseModel):
    run_key: str
    job_run_id: int


class EvalRunStatus(BaseModel):
    state: str
    result_state: str | None
    run_key: str


class SwitchIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    to_version: int = Field(ge=1)


class SwitchOut(BaseModel):
    job_run_id: int


def _jobs_call(fn: Callable[[], T]) -> T:
    """Run one Jobs call and map SDK failures to reason codes. SDK error text is logged by type only."""
    try:
        return fn()
    except (jobs.JobNotFound, NotFound):
        raise problem(404, Reason.JOB_NOT_FOUND) from None
    except InvalidParameterValue:  # a bad parameter, or a configured Job ID that no longer exists
        log.warning("jobs call refused: InvalidParameterValue")
        raise problem(502, Reason.JOB_FAILED) from None
    except ResourceConflict:
        raise problem(409, Reason.JOB_CONFLICT) from None
    except PermissionDenied:
        raise problem(403, Reason.JOB_FORBIDDEN) from None
    except DatabricksError as exc:
        log.warning("jobs call failed: %s", type(exc).__name__)
        raise problem(502, Reason.JOB_FAILED) from None


def _start(app_w: WorkspaceClient, job_name: str, params: dict[str, str]) -> int:
    return _jobs_call(lambda: jobs.start_job(app_w, job_name, params))


def _journal(sql: UserSql, cfg: HubConfig, dept: str, name: str) -> list[dict]:
    """The skill's journal rows, oldest first, read as the user."""
    try:
        return [r for r in sql.query(*queries.journal_sql(cfg, dept)) if r["skill"] == name]
    except SqlDenied as exc:
        if exc.not_found:  # no skill_versions table yet: no version is published
            return []
        raise Forbidden(Reason.EVALS_NOT_READABLE) from None


def _published(history: list[dict]) -> set[int]:
    return {r["version"] for r in history if r["action"] == "published" and r["version"]}


def _pinned_versions(
    user_w: WorkspaceClient,
    app_w: WorkspaceClient,
    sql: UserSql,
    cfg: HubConfig,
    versions: dict[str, int],
    requested: set[str],
) -> dict[str, int]:
    """Each pinned skill must be requested, and each pinned version published and readable by the user.

    The version lives in `<dept>_versions`, which a live-skill reader may not be able to read, so the user's
    READ on the version itself is checked. Returns {"dept/name": version}.
    """
    pins: dict[str, int] = {}
    for fqn, version in versions.items():
        if fqn not in requested:
            raise problem(422, Reason.VERSION_NOT_REQUESTED)
        dept, name = skill_ref.parse_fqn(cfg, fqn)
        if version not in _published(_journal(sql, cfg, dept, name)):
            raise problem(404, Reason.VERSION_NOT_FOUND)
        authz.authorize_read(user_w, app_w, version_fqn(cfg, dept, name, version))
        pins[f"{dept}/{name}"] = version
    return pins


@router.post("/eval-runs", response_model=EvalRunOut)
def start_eval(
    body: EvalRunIn,
    user_w: Annotated[WorkspaceClient, Depends(get_user_client)],
    app_w: Annotated[WorkspaceClient, Depends(get_app_client)],
    cfg: Annotated[HubConfig, Depends(get_cfg)],
    sql: Annotated[UserSql, Depends(get_sql)],
) -> EvalRunOut:
    models = list(dict.fromkeys(body.models))
    modes = list(dict.fromkeys(body.modes))
    if any(m not in cfg.models for m in models):
        raise problem(422, Reason.UNKNOWN_MODEL)
    if body.candidate is not None:
        if body.skills or body.versions:  # one skills folder per run: live skills and pins run on their own
            raise problem(422, Reason.MIXED_SOURCES)
        params = _candidate_params(user_w, app_w, cfg, body.candidate, models, modes)
        return EvalRunOut(run_key=params["run_key"], job_run_id=_start(app_w, "skill-eval", params))
    refs: set[str] = set()
    for fqn in dict.fromkeys(body.skills):
        dept, name = skill_ref.parse_fqn(cfg, fqn)
        authz.authorize_read(user_w, app_w, fqn)  # 403 skill_not_readable when refused
        refs.add(f"{dept}/{name}")  # the Job always gets dept/name
    pins = _pinned_versions(user_w, app_w, sql, cfg, body.versions, set(body.skills))
    requester = authz.fetch_me(user_w).user
    run_key = make_run_key(f"hub-{uuid.uuid4().hex[:8]}")
    # Every parameter is sent; "" means unset.
    params = {
        "run_key": run_key,
        "skills": ",".join(sorted(refs)),
        "models": ",".join(models),
        "modes": ",".join(modes),
        "versions": ",".join(f"{n}={v}" for n, v in sorted(pins.items())),  # dept/name=N pairs
        "source": "live",
        "trigger_source": "hub",
        "requested_by": requester,
        "skills_root": "",
        "fixtures_root": "",
    }
    return EvalRunOut(run_key=run_key, job_run_id=_start(app_w, "skill-eval", params))


def _candidate_params(
    user_w: WorkspaceClient,
    app_w: WorkspaceClient,
    cfg: HubConfig,
    ref: CandidateRunIn,
    models: list[str],
    modes: list[str],
) -> dict[str, str]:
    """Job parameters for an eval of candidate skills: a pull request upload, or a draft. Authors and reviewers only,
    under the candidate visibility rule (backend.candidates); a candidate the user may not see answers 404, as a
    missing one does. Without evals/rubric.json only trigger mode can run, unless the skill's effectiveness cases are
    all assertion cases, which are then judged on their assertions only (evalspec.rubric_optional)."""
    me = authz.fetch_me(user_w)
    key = candidates.parse_key(ref.run_key)
    refs: list[tuple[str, str]] = []
    for item in dict.fromkeys(ref.skills):
        dept, sep, name = item.partition("/")
        refs.append((dept, name) if sep else ("", name))
    if not candidates.may_see(me, cfg):
        raise problem(403, Reason.NOT_AUTHOR)
    if key is None or any(d not in cfg.departments or not candidates.valid_skill_name(n) for d, n in refs):
        raise problem(404, Reason.CANDIDATE_NOT_FOUND)
    candidates.authorize(app_w, cfg, me, key, refs)
    for dept, name in refs:  # read as the app principal only now
        md = f"{candidates.skill_dir(cfg, key, dept, name)}/SKILL.md"
        if not candidates.workspace_call(lambda md=md: candidates.exists(app_w, md)):
            raise problem(404, Reason.CANDIDATE_NOT_FOUND)
    if "effectiveness" in modes:
        for dept, name in refs:  # effectiveness cases need a rubric; say so before the Job starts
            if not candidates.workspace_call(
                lambda dept=dept, name=name: candidates.rubric_ok(app_w, cfg, key, dept, name)
            ):
                raise problem(422, Reason.RUBRIC_REQUIRED)
    fixtures = candidates.workspace_call(lambda: candidates.fixtures_root(app_w, cfg, key))
    # Every parameter is sent; "" means unset. The key is fresh, so the PR gate's rows are never mixed with these.
    return {
        "run_key": candidates.hub_run_key(key.run_key),
        "skills": ",".join(f"{d}/{n}" for d, n in sorted(refs)),
        "models": ",".join(models),
        "modes": ",".join(modes),
        "versions": "",
        "source": candidates.source(key),
        "trigger_source": "manual",
        "requested_by": me.user,
        "skills_root": f"{candidates.folder(cfg, key)}/skills",
        "fixtures_root": fixtures,
    }


@router.get("/eval-runs/{job_run_id}", response_model=EvalRunStatus)
def eval_status(
    job_run_id: Annotated[int, Path(gt=0)],
    user_w: Annotated[WorkspaceClient, Depends(get_user_client)],
    app_w: Annotated[WorkspaceClient, Depends(get_app_client)],
    cfg: Annotated[HubConfig, Depends(get_cfg)],
) -> EvalRunStatus:
    me = authz.fetch_me(user_w)
    try:
        run = app_w.jobs.get_run(run_id=job_run_id)
    except (NotFound, InvalidParameterValue, PermissionDenied):
        raise problem(404, Reason.RUN_NOT_FOUND) from None
    except DatabricksError as exc:
        log.warning("get_run failed: %s", type(exc).__name__)
        raise problem(502, Reason.JOB_FAILED) from None
    # Only runs of the Hub's own Jobs are visible; no other run id is a window onto the workspace.
    own: set[int] = set()
    for job_name in jobs.JOB_NAMES:  # each id on its own: one missing Job must not hide the other's runs
        try:
            own.add(jobs.job_id(app_w, job_name))
        except jobs.JobNotFound:
            continue
        except DatabricksError as exc:
            log.warning("job lookup failed: %s", type(exc).__name__)
            raise problem(502, Reason.JOB_FAILED) from None
    if getattr(run, "job_id", None) not in own:
        raise problem(404, Reason.RUN_NOT_FOUND)
    params = {p.name: p.value for p in (run.job_parameters or [])}
    owner = params.get("requested_by") or params.get("actor")
    if owner != me.user and not authz.has_reviewer_role(me, cfg):
        raise problem(
            404, Reason.RUN_NOT_FOUND
        )  # same answer as "no such run": other users' runs do not leak
    state = run.state
    life = state.life_cycle_state.value if state and state.life_cycle_state else "UNKNOWN"
    result = state.result_state.value if state and state.result_state else None
    return EvalRunStatus(state=life, result_state=result, run_key=params.get("run_key", ""))


@router.post("/skills/{fqn}/switch", response_model=SwitchOut)
def switch(
    fqn: str,
    body: SwitchIn,
    user_w: Annotated[WorkspaceClient, Depends(get_user_client)],
    app_w: Annotated[WorkspaceClient, Depends(get_app_client)],
    cfg: Annotated[HubConfig, Depends(get_cfg)],
    sql: Annotated[UserSql, Depends(get_sql)],
) -> SwitchOut:
    dept, name = skill_ref.parse_fqn(cfg, fqn)
    actor = authz.require_reviewer(user_w, cfg)
    authz.authorize_read(user_w, app_w, fqn)
    history = _journal(sql, cfg, dept, name)
    if body.to_version not in _published(history):
        raise problem(404, Reason.VERSION_NOT_FOUND)
    # Only a run for this skill blocks. A switch of another skill queues behind it (max_concurrent_runs 1).
    active = _jobs_call(lambda: jobs.active_runs(app_w, "skill-switch"))
    if any(p.get("dept") == dept and p.get("skill") == name for p in active):
        raise problem(409, Reason.SWITCH_IN_PROGRESS)
    # The last row that starts or ends a switch decides it, as in reconcile: published, publish_refused and
    # drift rows can follow an unfinished switch and must not hide it.
    tail = next((r for r in reversed(history) if r["action"] in SWITCH_ROWS), None)
    if tail is not None and tail["action"] in ("switch_started", "switch_failed"):
        # Recovery: re-run the interrupted switch or go back. Nothing else until it is settled.
        if body.to_version not in {tail.get("from_version"), tail.get("to_version")}:
            raise problem(409, Reason.FINISH_OR_REVERT_SWITCH)
    elif skill_ref.live_version(history) == body.to_version:
        raise problem(409, Reason.ALREADY_LIVE)
    # Only the serialised skill-switch Job may switch (max_concurrent_runs 1); the app never switches in-process.
    params = {
        "action": "switch",
        "dept": dept,
        "skill": name,
        "version": str(body.to_version),
        "git_sha": "",
        "eval_run_id": "",
        "actor": actor,
    }
    return SwitchOut(job_run_id=_start(app_w, "skill-switch", params))
