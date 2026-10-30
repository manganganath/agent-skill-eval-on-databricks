"""Pull requests opened from the Skill Hub: the release steps of one skill's pull request (GET /api/release-status), and a
reviewer's merge (POST /api/pulls/{number}/merge). The rules are in backend.release, the GitHub calls in backend.github."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from databricks.sdk import WorkspaceClient
from fastapi import APIRouter, Depends, HTTPException, Path, Query

from backend import authz, candidates, github, queries, release, skill_ref
from backend.api.skills import read_dept
from backend.authz import Reason
from backend.data import UserSql
from backend.deps import get_app_client, get_cfg, get_sql, get_user_client, problem
from backend.schemas import (
    MergeOut,
    ReleaseGate,
    ReleaseLive,
    ReleaseMerged,
    ReleaseOpened,
    ReleaseRelease,
    ReleaseStatusOut,
)
from skillhub.config import HubConfig

router = APIRouter(tags=["pulls"])


def _pull(gh: github.GitHubClient, number: int) -> github.Pull:
    try:
        return gh.pull(number)
    except github.GitHubNotFound:
        raise problem(404, Reason.PULL_NOT_FOUND) from None


def _when(value: object) -> datetime | None:
    """A journal or GitHub timestamp as an aware datetime, or None."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _switched_after(rows: list[dict], merged_at: datetime | None) -> bool:
    if merged_at is None:
        return False
    return any(
        r.get("action") == "switched" and (ts := _when(r.get("ts"))) is not None and ts > merged_at
        for r in rows
    )


@router.get("/release-status", response_model=ReleaseStatusOut)
def release_status(
    pr: Annotated[int, Query(ge=1, le=999_999_999)],
    dept: Annotated[str, Query(max_length=64)],
    skill: Annotated[str, Query(max_length=64)],
    user_w: Annotated[WorkspaceClient, Depends(get_user_client)],
    app_w: Annotated[WorkspaceClient, Depends(get_app_client)],
    cfg: Annotated[HubConfig, Depends(get_cfg)],
    sql: Annotated[UserSql, Depends(get_sql)],
) -> ReleaseStatusOut:
    """The steps of a pull request's path to a live version, for one skill of it: opened, the eval gate, merged, the
    release (waiting for approval in GitHub, or done), and the skill's live version. Authors and reviewers, with the
    visibility of the department's candidates. 404 `pull_not_found`; 503 `github_not_configured`."""
    if dept not in cfg.departments or not candidates.valid_skill_name(skill):
        raise problem(404, Reason.CANDIDATE_NOT_FOUND)
    me = authz.fetch_me(user_w)
    candidates.require_visible(app_w, cfg, me, [dept])
    gh = github.connect(app_w)
    pull = _pull(gh, pr)
    checks = gh.check_states(pull.head_sha)
    merged_sha = pull.merge_sha if pull.merged else None
    run = gh.publish_run(merged_sha, release.PUBLISH_WORKFLOW) if merged_sha else None
    rows, access = read_dept(sql, queries.journal_sql(cfg, dept, skill), dept)
    merged_at = _when(pull.merged_at) if pull.merged else None
    return ReleaseStatusOut(
        pr_number=pr,
        dept=dept,
        skill=skill,
        opened=ReleaseOpened(state="closed" if pull.state == "closed" else "open", url=pull.url),
        gate=ReleaseGate(state=release.gate_state(checks), url=f"{pull.url}/checks"),
        merged=ReleaseMerged(merged=pull.merged, sha=merged_sha, at=merged_at),
        release=ReleaseRelease(state=release.release_state(run), url=run.url if run else None),
        live=ReleaseLive(
            version=skill_ref.live_version(rows) if access == "ok" else None,
            switched_after_merge=access == "ok" and _switched_after(rows, merged_at),
            eval_access=access,
        ),
    )


@router.post("/pulls/{number}/merge", response_model=MergeOut)
def merge_pull(
    user_w: Annotated[WorkspaceClient, Depends(get_user_client)],
    app_w: Annotated[WorkspaceClient, Depends(get_app_client)],
    cfg: Annotated[HubConfig, Depends(get_cfg)],
    number: Annotated[int, Path(ge=1, le=999_999_999)],
) -> MergeOut:
    """Merge a pull request with a merge commit. Reviewers only: 403 `not_reviewer`. 409 `not_mergeable` lists in
    `unmet` the rules that do not hold: the pull request is open, it touches only skills/, it was opened from the Skill
    Hub or names only skills the reviewer may see, every required check passed, and its mergeable state is clean.
    The production release is never approved here: it waits for the reviewers of the production environment in GitHub."""
    me = authz.fetch_me(user_w)
    if not authz.has_reviewer_role(me, cfg):
        raise problem(403, Reason.NOT_REVIEWER)
    gh = github.connect(app_w, cached=False)  # the rules are checked against GitHub as it is now
    pull = _pull(gh, number)
    try:
        paths = gh.pull_paths(number)
    except github.GitHubRejected:
        paths = []  # too many files to be a skill pull request: the rules below refuse it
    checks = gh.check_states(pull.head_sha)
    unmet = release.unmet_merge_rules(
        pull, paths, checks, cfg, visible=set(cfg.departments)
    )  # reviewers see all
    if unmet:
        raise HTTPException(status_code=409, detail={"reason": Reason.NOT_MERGEABLE.value, "unmet": unmet})
    try:
        sha = gh.merge(number, pull.head_sha)
    except github.GitHubRejected:  # GitHub refused the merge: the branch moved, or branch protection says no
        raise HTTPException(
            status_code=409, detail={"reason": Reason.NOT_MERGEABLE.value, "unmet": ["github"]}
        ) from None
    return MergeOut(pr_number=number, merged=True, sha=sha)
