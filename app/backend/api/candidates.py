"""Candidate skills: under review in a pull request, not released. The visibility rule is in backend.candidates."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Annotated

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import DatabricksError
from fastapi import APIRouter, Depends
from pydantic import ValidationError

from backend import authz, candidates, queries
from backend.api.skills import MAX_PARALLEL, decode_text, read_dept
from backend.authz import Reason
from backend.data import UserSql
from backend.deps import get_app_client, get_cfg, get_sql, get_user_client, problem
from backend.schemas import CandidateBlock, CandidateDetail, CandidateResult, CandidateSummary, EvalAccess
from skillhub.bundle import content_hash
from skillhub.config import HubConfig
from skillhub.evalspec import EvalFile, Rubric

log = logging.getLogger("skillhub.app.candidates")
router = APIRouter(tags=["candidates"])


def _order(c: CandidateSummary) -> tuple[bool, float, str, str]:
    """Newest upload first; then department and name."""
    stamp = c.uploaded_at
    return (stamp is None, -(stamp.timestamp() if stamp else 0.0), c.dept, c.name)


@router.get("/candidates", response_model=list[CandidateSummary])
def list_candidates(
    user_w: Annotated[WorkspaceClient, Depends(get_user_client)],
    app_w: Annotated[WorkspaceClient, Depends(get_app_client)],
    cfg: Annotated[HubConfig, Depends(get_cfg)],
    sql: Annotated[UserSql, Depends(get_sql)],
) -> list[CandidateSummary]:
    """The changed skills of each pull request's newest upload that the user may see. The work is bounded: one
    folder listing, one marker per pull request run, one Job-runs listing, one SQL read per visible department and
    one SKILL.md per candidate, side by side on a small pool. No other skill file is opened."""
    me = authz.fetch_me(user_w)
    if not candidates.may_see(me, cfg):
        return []  # nothing is read as the app principal for this user
    memo = authz.RequestMemo()
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        # Folder names only; no content is opened before the departments are settled.
        listing = pool.submit(candidates.list_keys, app_w, cfg)
        depts = candidates.visible_departments(app_w, cfg, me, pool, memo)
        keys = candidates.workspace_call(listing.result)
        if not keys or not depts:
            return []
        stamps = {k.run_key: pool.submit(candidates.uploaded_at, app_w, cfg, k.run_key) for k in keys}
        newest = candidates.newest_per_pr(keys, {k: s.result() for k, s in stamps.items()})
        chosen = [k.run_key for k, _ in newest]
        started = pool.submit(candidates.tested_skills, app_w, chosen)
        reads = {
            d: pool.submit(read_dept, sql, queries.candidate_runs_sql(cfg, d, chosen), d)
            for d in sorted(depts)
        }
        tested = started.result()
        rows: dict[str, tuple[list[dict], EvalAccess]] = {d: r.result() for d, r in reads.items()}
        # The skills under review of each upload: those its PR eval was started for, and those with eval rows.
        plan: list[tuple[candidates.CandidateKey, datetime | None, str, str]] = []
        for key, stamp in newest:
            refs = set(tested.get(key.run_key, candidates.StartedEval()).skills)
            for d, (dept_rows, _) in rows.items():
                refs |= {
                    (d, str(r.get("skill")))
                    for r in dept_rows
                    if candidates.owns_run_key(key.run_key, str(r.get("run_key") or ""))
                }
            plan += [
                (key, stamp, d, n) for d, n in sorted(refs) if d in depts and candidates.valid_skill_name(n)
            ]
        plan = plan[: candidates.MAX_CANDIDATES]
        skill_mds = [
            pool.submit(candidates.read_skill_md, app_w, cfg, k.run_key, d, n) for k, _, d, n in plan
        ]
        out: list[CandidateSummary] = []
        for (key, stamp, dept, name), skill_md in zip(plan, skill_mds, strict=True):
            try:
                data = skill_md.result()
            except DatabricksError as exc:  # this candidate only; the others still list
                log.warning("skipped candidate %s/%s of %s: %s", dept, name, key.run_key, type(exc).__name__)
                continue
            if data is None:
                continue  # not in the upload: deleted in the pull request, or the folder went away
            dept_rows, access = rows[dept]
            mine = [r for r in dept_rows if r.get("skill") == name]
            latest = candidates.latest_rows(mine, key.run_key)
            out.append(
                CandidateSummary(
                    run_key=key.run_key,
                    pr_number=key.pr_number,
                    git_sha=candidates.commit(key, mine, tested.get(key.run_key)),
                    uploaded_at=stamp,
                    dept=dept,
                    name=name,
                    description=candidates.description(data),
                    content_hash=candidates.content_hash(latest),
                    verdicts=candidates.verdicts(latest),
                    eval_access=access,
                )
            )
    return sorted(out, key=_order)


def _parse(model, data: bytes | None, label: str):
    if data is None:
        return None
    try:
        return model.model_validate_json(data)
    except ValidationError:
        log.warning("the candidate's %s could not be parsed", label)
        return None


@router.get("/candidates/{run_key}/{dept}/{skill}", response_model=CandidateDetail)
def candidate_detail(
    run_key: str,
    dept: str,
    skill: str,
    user_w: Annotated[WorkspaceClient, Depends(get_user_client)],
    app_w: Annotated[WorkspaceClient, Depends(get_app_client)],
    cfg: Annotated[HubConfig, Depends(get_cfg)],
    sql: Annotated[UserSql, Depends(get_sql)],
) -> CandidateDetail:
    key = candidates.parse_key(run_key)
    if key is None or dept not in cfg.departments or not candidates.valid_skill_name(skill):
        raise problem(404, Reason.CANDIDATE_NOT_FOUND)
    candidates.require_visible(app_w, cfg, authz.fetch_me(user_w), [dept])  # before any read as the app
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        rows_read = pool.submit(
            read_dept, sql, queries.candidate_runs_sql(cfg, dept, [key.run_key], skill), dept
        )
        stamp_read = pool.submit(candidates.uploaded_at, app_w, cfg, key.run_key)
        files = candidates.workspace_call(
            lambda: candidates.read_skill_files(app_w, cfg, key.run_key, dept, skill, pool)
        )
        if "SKILL.md" not in files:
            raise problem(404, Reason.CANDIDATE_NOT_FOUND)
        rows, access = rows_read.result()
        stamp = stamp_read.result()
    owned = candidates.owned_rows(rows, key.run_key)
    latest = candidates.latest_rows(owned, key.run_key)
    return CandidateDetail(
        dept=dept,
        name=skill,
        description=candidates.description(files["SKILL.md"]),
        content_hash=content_hash(files),  # evals/ is not part of the content, as in the eval
        verdicts=candidates.verdicts(latest),
        files=decode_text(files),
        evals=_parse(EvalFile, files.get("evals/evals.json"), "evals.json"),
        rubric=_parse(Rubric, files.get("evals/rubric.json"), "rubric.json"),
        eval_access=access,
        results=[CandidateResult.model_validate(r) for r in owned],
        candidate=CandidateBlock(
            pr_number=key.pr_number,
            run_key=key.run_key,
            git_sha=candidates.commit(key, owned),
            uploaded_at=stamp,
        ),
    )
