"""The releases that wait for approval: GET /api/releases/pending, for reviewers. The approval is made in GitHub, in the
production environment, by its required reviewers, and never here: this module only reads, and links to the run. The
rules are in backend.release, the GitHub calls in backend.github."""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, TypeVar

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
_T = TypeVar("_T")


# What the page calls each standing of a note: the release's own (`found`), a guess for a run from before the notes
# (`absent`), or a note that could not be read or trusted (`unknown`).
SKILLS_SOURCE = {release.FOUND: "release", release.ABSENT: "pull_request", release.UNKNOWN: "unknown"}
EVAL_BASIS = {release.FOUND: "release", release.ABSENT: "inferred", release.UNKNOWN: "unknown"}
EVAL_SOURCE = {"merge": "merge", "pr": "pull_request"}

Skill = tuple[str, str, str]  # department, skill, action


@dataclass(frozen=True)
class _Read:
    """What GitHub says about one waiting run: its merged pull request, the skills that changes, the run's own plan and
    eval notes, its security scan and when it began to wait. `ok` is False when GitHub failed for the pull request."""

    pull: github.Pull | None
    pull_skills: list[tuple[str, str]]
    scan: str
    waiting_since: datetime | None
    plan: release.PlanNote | None = None
    plan_standing: str = release.UNKNOWN
    evaluated: release.EvalNote | None = None
    eval_standing: str = release.UNKNOWN
    ok: bool = True

    @property
    def skills(self) -> list[Skill]:
        """The skills to list: the release's plan when the run annotated it, else the pull request's."""
        if self.plan is None:
            return [(dept, skill, "publish") for dept, skill in self.pull_skills]
        return [(d, s, "publish") for d, s in self.plan.publish] + [
            (d, s, "retire") for d, s in self.plan.retire
        ]

    @property
    def earlier_changes(self) -> bool:
        """The plan has a skill that the merged pull request did not change: earlier merges are in the release."""
        if self.plan is None or self.pull is None:
            return False
        return any((d, s) not in self.pull_skills for d, s, _ in self.skills)


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
            paths = gh.pull_paths(number, settled=True)
        except github.GitHubRejected:
            paths = []  # more files than the Hub reads; no skill pull request is that large
    except github.GitHubError as exc:
        log.warning("could not read the pull request of run %s: %s", run.number, type(exc).__name__)
        return None, [], False
    named = {release.skill_of(path, cfg.departments) for path in paths}
    return pull, sorted(n for n in named if n is not None), True


def _notes(
    gh: github.GitHubClient, job: github.Job | None, run: github.WaitingRun
) -> list[github.Annotation] | None:
    """The annotations of a completed job, read once; None when the job is not one, or GitHub could not be read. A job
    that did not finish has none yet, and the caller tells the two apart by `job`."""
    if job is None or job.status != "completed":
        return None
    try:
        return gh.job_annotations(job.id)
    except github.GitHubError as exc:
        log.warning("could not read the annotations of run %s: %s", run.number, type(exc).__name__)
        return None


def _read(gh: github.GitHubClient, run: github.WaitingRun, cfg: HubConfig) -> _Read:
    pull, skills, ok = _pull_and_skills(gh, run, cfg)
    try:
        jobs = gh.run_jobs(run.id)
    except (
        github.GitHubError
    ) as exc:  # nothing of the run is known, and the wait may be the run's last change
        log.warning("could not read the jobs of run %s: %s", run.number, type(exc).__name__)
        return _Read(pull, skills, "unknown", run.updated_at, ok=ok)
    plan_job, eval_job = release.job_named(jobs, release.PLAN_JOB), release.job_named(jobs, release.EVAL_JOB)
    plan_notes = _notes(gh, plan_job, run)
    scan = release.scan_state(release.scan_step(jobs), plan_notes)
    plan, plan_standing = _standing(
        plan_job, plan_notes, release.PLAN_TITLE, lambda m: release.parse_plan(m, cfg.departments)
    )
    # A re-evaluation job that was skipped (the release only retires) or did not succeed has no note.
    evaluated_job = eval_job if eval_job is not None and eval_job.conclusion == "success" else None
    evaluated, eval_standing = _standing(
        evaluated_job,
        _notes(gh, evaluated_job, run),
        release.EVAL_TITLE,
        lambda m: release.parse_eval(m, run.sha),
    )
    return _Read(
        pull, skills, scan, release.waiting_since(jobs) or run.updated_at, plan, plan_standing, evaluated,
        eval_standing, ok,
    )  # fmt: skip


def _standing(
    job: github.Job | None,
    notes: list[github.Annotation] | None,
    title: str,
    parse: Callable[[str], _T | None],
) -> tuple[_T | None, str]:
    """(the note, its standing) of a job. No job, or one still running, has no note yet: absent. A completed job
    whose annotations could not be read is unknown."""
    if job is None or job.status != "completed":
        return None, release.ABSENT
    return release.read_note(notes, title, parse)


def _evals(rows: list[dict], skill: str, sha: str, pr_key: str | None) -> tuple[str | None, str | None]:
    """(run key, source) of the eval the release gates on for a skill when the run did not say: the newest merge eval of
    the commit when it has rows, else the pull request's eval that the release reuses."""
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
    request (the newest one, as context), the skills the release publishes and retires (from the plan the run
    annotated; for a run from before that, the merged pull request's skills), each skill's verdicts from the eval the
    release gates on (the one the run annotated; else the merge eval, or the pull request's eval the release reuses),
    the run's own security scan result, and the run to open in GitHub.
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
            release.eval_key(keys, r.pull.head_sha, r.pull.number) if r.pull is not None else None
            for _, r in ordered
        ]
        # One read per department for all the runs. A release that says which eval it gates on is read at that key;
        # for any other, the merge evals of its commit and the pull request's own eval.
        wanted: dict[str, list[str]] = {}
        for (run, read), upload in zip(ordered, uploads, strict=True):
            if read.evaluated is not None:
                run_keys = [read.evaluated.run_key]
            else:
                run_keys = [release.merge_prefix(run.sha), *([upload.run_key] if upload else [])]
            for dept, _, action in read.skills:
                if action == "publish":
                    wanted.setdefault(dept, []).extend(run_keys)
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
        for dept, skill, action in read.skills:
            if action == "retire":  # nothing is evaluated for a skill that is removed
                skills.append(
                    PendingSkill(
                        dept=dept, skill=skill, action=action, verdicts={}, eval_access="ok", eval_source=None
                    )
                )
                continue
            dept_rows, access = rows[dept]
            if read.evaluated is not None:
                key, source = read.evaluated.run_key, EVAL_SOURCE[read.evaluated.trigger_source]
            else:
                key, source = _evals(dept_rows, skill, run.sha, pr_key)
            mine = [r for r in dept_rows if r.get("skill") == skill and r.get("run_key") == key]
            verdicts = candidates.verdicts(candidates.latest_rows(mine, key or ""), cfg.defaults.gating_modes)
            skills.append(
                PendingSkill(
                    dept=dept,
                    skill=skill,
                    action=action,
                    verdicts=verdicts,
                    eval_access=access,
                    eval_source=source,
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
                skills_source=SKILLS_SOURCE[read.plan_standing],
                eval_basis=EVAL_BASIS[read.eval_standing],
                earlier_changes=read.earlier_changes,
                run_key=pr_key,
                skills=skills,
            )
        )
    return out
