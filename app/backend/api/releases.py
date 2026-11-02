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
from backend.schemas import PendingPull, PendingRelease, PendingSkill
from skillhub.config import HubConfig

log = logging.getLogger("skillhub.app.releases")
router = APIRouter(tags=["releases"])


@dataclass(frozen=True)
class _Read:
    """What GitHub says about one waiting run: its merged pull request, the skills that changes, the run's own security
    scan and when it began to wait. `ok` is False when GitHub failed for the pull request."""

    pull: github.Pull | None
    skills: list[tuple[str, str]]
    scan: str
    waiting_since: datetime | None
    ok: bool = True


def _pull_and_skills(
    gh: github.GitHubClient, run: github.WaitingRun, cfg: HubConfig
) -> tuple[github.Pull | None, list[tuple[str, str]], bool]:
    """The pull request that was merged into the run's commit and the skills it changes. A failure of GitHub marks this
    run only; the others still list."""
    try:
        number = gh.merged_pull_number(run.sha)
        if number is None:
            return None, [], True
        pull = gh.pull(number)
        try:
            paths = gh.pull_paths(number)
        except github.GitHubRejected:
            paths = []  # more files than the Hub reads; no skill pull request is that large
    except github.GitHubError as exc:
        log.warning("could not read the pull request of run %s: %s", run.number, type(exc).__name__)
        return None, [], False
    named = {release.skill_of(path, cfg.departments) for path in paths}
    return pull, sorted(n for n in named if n is not None), True


def _read(gh: github.GitHubClient, run: github.WaitingRun, cfg: HubConfig) -> _Read:
    pull, skills, ok = _pull_and_skills(gh, run, cfg)
    try:
        jobs = gh.run_jobs(run.id)
    except github.GitHubError as exc:  # the scan is unknown and the wait is the run's last change
        log.warning("could not read the jobs of run %s: %s", run.number, type(exc).__name__)
        jobs = []
    return _Read(pull, skills, release.scan_state(jobs), release.waiting_since(jobs) or run.updated_at, ok)


def _evals(rows: list[dict], skill: str, sha: str, pr_key: str | None) -> tuple[str | None, str | None]:
    """(run key, source) of the eval the release gates on for a skill: the newest merge eval of the commit when it has
    rows, else the pull request's eval that the release reuses."""
    mine = {str(r.get("run_key")) for r in rows if r.get("skill") == skill}
    merge = release.merge_eval_key(mine, sha)
    if merge is not None:
        return merge, "merge"
    return (pr_key, "pull_request") if pr_key in mine else (None, None)


def _waiting_order(item: tuple[github.WaitingRun, _Read]) -> tuple[bool, datetime, int]:
    """Longest waiting first; a run without a time last."""
    run, read = item
    return (read.waiting_since is None, read.waiting_since or datetime.min.replace(tzinfo=UTC), run.number)


@router.get("/releases/pending", response_model=list[PendingRelease])
def pending_releases(
    user_w: Annotated[WorkspaceClient, Depends(get_user_client)],
    app_w: Annotated[WorkspaceClient, Depends(get_app_client)],
    cfg: Annotated[HubConfig, Depends(get_cfg)],
    sql: Annotated[UserSql, Depends(get_sql)],
) -> list[PendingRelease]:
    """Every skills-publish run that waits for the production approval, longest waiting first: its merged pull
    request, the skills it changes, each skill's verdicts from the eval the release gates on (the merge eval, or the
    pull request's eval the release reuses), the run's own security scan result, and the run to open in GitHub.
    Reviewers only: 403 `not_reviewer`, before anything is read. 503 `github_not_configured`.
    Read only: nothing is approved or rejected here, and nothing is written to GitHub. The merge commit of a run has
    no pull request when it was pushed straight to main: that run is listed with what is known. When GitHub fails for
    one run, that run is listed with `details` `unavailable`."""
    authz.require_reviewer(user_w, cfg)
    gh = github.connect(app_w)
    runs = gh.waiting_runs(release.PUBLISH_WORKFLOW, release.MAX_WAITING)
    if not runs:
        return []
    try:
        keys = candidates.list_keys(app_w, cfg)
    except DatabricksError as exc:  # the candidate pages are lost, not the list
        log.warning("could not list the candidate uploads: %s", type(exc).__name__)
        keys = []
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        reads = list(pool.map(lambda run: _read(gh, run, cfg), runs))
        ordered = sorted(zip(runs, reads, strict=True), key=_waiting_order)
        uploads = [
            release.eval_key(keys, r.pull.head_sha) if r.pull is not None else None for _, r in ordered
        ]
        # One read per department for all the runs: the merge evals of each commit and the pull requests' own evals.
        wanted: dict[str, list[str]] = {}
        for (run, read), upload in zip(ordered, uploads, strict=True):
            for dept, _ in read.skills:
                wanted.setdefault(dept, []).extend(
                    [release.merge_prefix(run.sha), *([upload.run_key] if upload else [])]
                )
        fetches = {
            dept: pool.submit(
                read_dept,
                sql,
                queries.candidate_runs_sql(cfg, dept, ks),
                dept,
                queries.candidate_runs_sql(cfg, dept, ks, legacy=True),
            )
            for dept, ks in sorted(wanted.items())
        }
        rows = {dept: fetch.result() for dept, fetch in fetches.items()}
    out: list[PendingRelease] = []
    for (run, read), upload in zip(ordered, uploads, strict=True):
        pr_key = upload.run_key if upload is not None else None
        skills = []
        for dept, skill in read.skills:
            dept_rows, access = rows[dept]
            key, source = _evals(dept_rows, skill, run.sha, pr_key)
            mine = [r for r in dept_rows if r.get("skill") == skill and r.get("run_key") == key]
            verdicts = candidates.verdicts(candidates.latest_rows(mine, key or ""), cfg.defaults.gating_modes)
            skills.append(
                PendingSkill(
                    dept=dept, skill=skill, verdicts=verdicts, eval_access=access, eval_source=source
                )
            )
        pull = read.pull
        out.append(
            PendingRelease(
                run_url=run.url,
                run_number=run.number,
                sha=run.sha,
                started_at=run.created_at,
                waiting_since=read.waiting_since,
                pull=None
                if pull is None
                else PendingPull(
                    number=pull.number,
                    title=pull.title,
                    author=pull.author,
                    merged_by=pull.merged_by,
                    url=pull.url,
                ),
                details="ok" if read.ok else "unavailable",
                scan=read.scan,
                run_key=pr_key,
                skills=skills,
            )
        )
    return out
