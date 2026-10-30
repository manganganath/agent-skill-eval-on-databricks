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
    """The changed skills of each pull request's newest upload, and the drafts, that the user may see. A pull request
    skill whose content is already the live version's is left out: its release lists the skill. The work is
    bounded: two folder listings, one marker per pull request run, one draft.json per draft, one Job-runs listing,
    one SQL read per department and one SKILL.md per pull request candidate, side by side on a small pool."""
    me = authz.fetch_me(user_w)
    if not candidates.may_see(me, cfg):
        return []  # nothing is read as the app principal for this user
    memo = authz.RequestMemo()
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        # Folder names only; no content is opened before the departments are settled.
        listing = pool.submit(candidates.list_keys, app_w, cfg)
        draft_listing = pool.submit(candidates.list_draft_keys, app_w, cfg)
        depts = candidates.visible_departments(app_w, cfg, me, pool, memo)
        keys = candidates.workspace_call(listing.result) if depts else []
        draft_keys = candidates.workspace_call(draft_listing.result)
        stamps = {k.run_key: pool.submit(candidates.uploaded_at, app_w, cfg, k) for k in keys}
        metas = [(k, pool.submit(candidates.read_meta, app_w, cfg, k, cfg.departments)) for k in draft_keys]
        newest = candidates.newest_per_pr(keys, {k: s.result() for k, s in stamps.items()})
        # A draft is seen in a visible department, and always by its uploader.
        drafts: list[tuple[candidates.CandidateKey, candidates.DraftMeta]] = []
        for key, read in metas:
            try:
                meta = read.result()
            except DatabricksError as exc:  # this draft only
                log.warning("skipped draft %s: %s", key.run_key, type(exc).__name__)
                continue
            if meta is not None and (meta.dept in depts or candidates.is_uploader(meta, me)):
                drafts.append((key, meta))
        if not newest and not drafts:
            return []
        pr_keys = [k.run_key for k, _ in newest]
        started = pool.submit(candidates.tested_skills, app_w, pr_keys)
        # Per department: the pull request uploads when the user may see the department, and its drafts shown here.
        dept_keys = {d: list(pr_keys) for d in depts} if pr_keys else {}
        for key, meta in drafts:
            dept_keys.setdefault(meta.dept, []).append(key.run_key)
        reads = {
            d: pool.submit(read_dept, sql, queries.candidate_runs_sql(cfg, d, ks), d)
            for d, ks in sorted(dept_keys.items())
        }
        # The live content of each visible department: a pull request skill that is already released is left out.
        journals = (
            {d: pool.submit(read_dept, sql, queries.journal_sql(cfg, d), d) for d in sorted(depts)}
            if pr_keys
            else {}
        )
        tested = started.result()
        rows: dict[str, tuple[list[dict], EvalAccess]] = {d: r.result() for d, r in reads.items()}
        live = {d: candidates.live_hashes(j.result()[0]) for d, j in journals.items()}
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
        skill_mds = [pool.submit(candidates.read_skill_md, app_w, cfg, k, d, n) for k, _, d, n in plan]

        def summary(key, dept, name, **fields) -> CandidateSummary:
            dept_rows, access = rows[dept]
            mine = [r for r in dept_rows if r.get("skill") == name]
            latest = candidates.latest_rows(mine, key.run_key)
            return CandidateSummary(
                kind=key.kind,
                run_key=key.run_key,
                pr_number=key.pr_number,
                git_sha=candidates.commit(key, mine, tested.get(key.run_key)),
                dept=dept,
                name=name,
                content_hash=candidates.content_hash(latest),
                verdicts=candidates.verdicts(latest, cfg.defaults.gating_modes),
                eval_access=access,
                **fields,
            )

        out: list[CandidateSummary] = []
        for (key, stamp, dept, name), skill_md in zip(plan, skill_mds, strict=True):
            try:
                data = skill_md.result()
            except DatabricksError as exc:  # this candidate only; the others still list
                log.warning("skipped candidate %s/%s of %s: %s", dept, name, key.run_key, type(exc).__name__)
                continue
            if data is None:
                continue  # not in the upload: deleted in the pull request, or the folder went away
            item = summary(key, dept, name, uploaded_at=stamp, description=candidates.description(data))
            if item.content_hash is not None and item.content_hash == live.get(dept, {}).get(name):
                continue  # released: the live version has this content, and the Catalog lists it
            out.append(item)
        # A submitted draft is listed as its pull request, so it is left out once that pull request is listed.
        listed = {item.pr_number for item in out}
        for key, meta in drafts:
            if meta.submitted_pr in listed:
                continue
            out.append(
                summary(
                    key,
                    meta.dept,
                    meta.skill,
                    uploaded_at=meta.uploaded_at,
                    description=meta.description,
                    uploaded_by=meta.uploaded_by,
                    has_rubric=meta.has_rubric,
                    submitted_pr=meta.submitted_pr,
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
    # Before any read as the app, except a draft's draft.json, which names its uploader.
    meta = candidates.authorize(app_w, cfg, authz.fetch_me(user_w), key, [(dept, skill)])
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        rows_read = pool.submit(
            read_dept, sql, queries.candidate_runs_sql(cfg, dept, [key.run_key], skill), dept
        )
        stamp_read = pool.submit(candidates.uploaded_at, app_w, cfg, key) if meta is None else None
        files = candidates.workspace_call(
            lambda: candidates.read_skill_files(app_w, cfg, key, dept, skill, pool)
        )
        if "SKILL.md" not in files:
            raise problem(404, Reason.CANDIDATE_NOT_FOUND)
        rows, access = rows_read.result()
        stamp = meta.uploaded_at if meta is not None else stamp_read.result()
    owned = candidates.owned_rows(rows, key.run_key)
    latest = candidates.latest_rows(owned, key.run_key)
    return CandidateDetail(
        dept=dept,
        name=skill,
        description=candidates.description(files["SKILL.md"]),
        content_hash=content_hash(files),  # evals/ is not part of the content, as in the eval
        verdicts=candidates.verdicts(latest, cfg.defaults.gating_modes),
        files=decode_text(files),
        evals=_parse(EvalFile, files.get("evals/evals.json"), "evals.json"),
        rubric=_parse(Rubric, files.get("evals/rubric.json"), "rubric.json"),
        eval_access=access,
        results=[CandidateResult.model_validate(r) for r in owned],
        candidate=CandidateBlock(
            kind=key.kind,
            pr_number=key.pr_number,
            run_key=key.run_key,
            git_sha=candidates.commit(key, owned),
            uploaded_at=stamp,
            uploaded_by=meta.uploaded_by if meta is not None else None,
            submitted_pr=meta.submitted_pr if meta is not None else None,
            submitted_url=meta.submitted_url if meta is not None else None,
        ),
    )
