"""The releases that wait for approval: GET /api/releases/pending, for reviewers. The approval is made in GitHub, in the
production environment, by its required reviewers, and never here: this module only reads, and links to the run. The
rules are in backend.release, the GitHub calls in backend.github."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import DatabricksError
from fastapi import APIRouter, Depends

from backend import authz, candidates, github, queries, release
from backend.api.skills import MAX_PARALLEL, read_dept
from backend.data import UserSql
from backend.deps import get_app_client, get_cfg, get_sql, get_user_client
from backend.schemas import PendingPull, PendingRelease, PendingScan, PendingSkill
from skillhub.config import HubConfig

log = logging.getLogger("skillhub.app.releases")
router = APIRouter(tags=["releases"])


@dataclass(frozen=True)
class _Merged:
    """What GitHub says about a run's merge commit. `ok` is False when GitHub failed for this run."""

    pull: github.Pull | None
    skills: list[tuple[str, str]]
    ok: bool = True


def _merged(gh: github.GitHubClient, run: github.WaitingRun, cfg: HubConfig) -> _Merged:
    """The pull request that was merged into the run's commit and the skills it changes. A failure of GitHub marks this
    run only; the others still list."""
    try:
        number = gh.merged_pull_number(run.sha)
        if number is None:
            return _Merged(None, [])
        pull = gh.pull(number)
        try:
            paths = gh.pull_paths(number)
        except github.GitHubRejected:
            paths = []  # more files than the Hub reads; no skill pull request is that large
    except github.GitHubError as exc:
        log.warning("could not read the pull request of run %s: %s", run.number, type(exc).__name__)
        return _Merged(None, [], ok=False)
    named = {release.skill_of(path, cfg.departments) for path in paths}
    return _Merged(pull, sorted(n for n in named if n is not None))


def _scan(
    app_w: WorkspaceClient,
    cfg: HubConfig,
    key: candidates.CandidateKey,
    dept: str,
    skill: str,
    pool: ThreadPoolExecutor,
) -> tuple[PendingScan, bool]:
    """The scan of one skill of the pull request's upload, and whether the upload still has the skill."""
    try:
        files = candidates.read_skill_files(app_w, cfg, key, dept, skill, pool)
    except (candidates.CandidateTooLarge, DatabricksError) as exc:
        log.warning("could not read %s/%s of %s to scan it: %s", dept, skill, key.run_key, type(exc).__name__)
        files = {}
    if "SKILL.md" not in files:
        return PendingScan(state="unavailable", counts={}), False
    found = release.scan_counts(cfg, dept, skill, files)
    return PendingScan(state="findings" if any(found.values()) else "clean", counts=found), True


def _waiting_order(run: github.WaitingRun) -> tuple[bool, datetime, int]:
    """Longest waiting first; a run without a time last."""
    return (run.updated_at is None, run.updated_at or datetime.min.replace(tzinfo=UTC), run.number)


@router.get("/releases/pending", response_model=list[PendingRelease])
def pending_releases(
    user_w: Annotated[WorkspaceClient, Depends(get_user_client)],
    app_w: Annotated[WorkspaceClient, Depends(get_app_client)],
    cfg: Annotated[HubConfig, Depends(get_cfg)],
    sql: Annotated[UserSql, Depends(get_sql)],
) -> list[PendingRelease]:
    """Every skills-publish run that waits for the production approval, longest waiting first: its merged pull
    request, the skills it changes, each skill's gate verdicts from its pull request eval and its security scan, and
    the run to open in GitHub. Reviewers only: 403 `not_reviewer`, before anything is read. 503 `github_not_configured`.
    Read only: nothing is approved or rejected here, and nothing is written to GitHub. The merge commit of a run has
    no pull request when it was pushed straight to main: that run is listed with what is known. When GitHub fails for
    one run, that run is listed with `details` `unavailable`."""
    authz.require_reviewer(user_w, cfg)
    gh = github.connect(app_w)
    runs = sorted(gh.waiting_runs(release.PUBLISH_WORKFLOW, release.MAX_WAITING), key=_waiting_order)
    if not runs:
        return []
    try:
        keys = candidates.list_keys(app_w, cfg)
    except DatabricksError as exc:  # the verdicts and scans are lost, not the list
        log.warning("could not list the candidate uploads: %s", type(exc).__name__)
        keys = []
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        merged = list(pool.map(lambda run: _merged(gh, run, cfg), runs))
        uploads = [release.eval_key(keys, m.pull.head_sha) if m.pull is not None else None for m in merged]
        # One read per department for all the uploads: the pull requests' own eval rows.
        wanted: dict[str, list[str]] = {}
        for m, key in zip(merged, uploads, strict=True):
            for dept, _ in m.skills if key is not None else []:
                wanted.setdefault(dept, []).append(key.run_key)
        reads = {
            dept: pool.submit(
                read_dept,
                sql,
                queries.candidate_runs_sql(cfg, dept, ks),
                dept,
                queries.candidate_runs_sql(cfg, dept, ks, legacy=True),
            )
            for dept, ks in sorted(wanted.items())
        }
        rows = {dept: read.result() for dept, read in reads.items()}
        out: list[PendingRelease] = []
        for run, m, key in zip(runs, merged, uploads, strict=True):
            skills = []
            for dept, skill in m.skills if key is not None else []:
                dept_rows, access = rows[dept]
                mine = [r for r in dept_rows if r.get("skill") == skill and r.get("run_key") == key.run_key]
                scan, in_hub = _scan(app_w, cfg, key, dept, skill, pool)
                verdicts = candidates.verdicts(
                    candidates.latest_rows(mine, key.run_key), cfg.defaults.gating_modes
                )
                skills.append(
                    PendingSkill(
                        dept=dept,
                        skill=skill,
                        verdicts=verdicts,
                        eval_access=access,
                        scan=scan,
                        in_hub=in_hub,
                    )
                )
            pull = m.pull
            out.append(
                PendingRelease(
                    run_url=run.url,
                    run_number=run.number,
                    sha=run.sha,
                    started_at=run.created_at,
                    waiting_since=run.updated_at,
                    pull=None
                    if pull is None
                    else PendingPull(
                        number=pull.number,
                        title=pull.title,
                        author=pull.author,
                        merged_by=pull.merged_by,
                        url=pull.url,
                    ),
                    details="ok" if m.ok else "unavailable",
                    run_key=key.run_key if key is not None else None,
                    skills=skills,
                )
            )
    return out
