from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from skillhub.evalspec import EvalFile, Rubric

# Whether the user could read the department's eval tables.
EvalAccess = Literal["ok", "denied", "unavailable"]


class MeOut(BaseModel):
    user: str
    groups: list[str]
    is_reviewer: bool
    is_author: bool


class ReadableOut(BaseModel):
    departments: list[str]


class SkillSummary(BaseModel):
    fqn: str
    dept: str
    name: str
    description: str | None
    live_version: int | None
    verdicts: dict[str, str]
    eval_access: EvalAccess


class SkillDetail(BaseModel):
    """One skill. live_version and verdicts are worked out as for its SkillSummary in the catalog."""

    fqn: str
    dept: str
    name: str
    description: str | None
    live_version: int | None
    verdicts: dict[str, str]
    files: dict[str, str]
    evals: EvalFile | None
    rubric: Rubric | None
    eval_access: EvalAccess


class JournalRowOut(BaseModel):
    """One skill_versions row. Mirrors JournalRow in app/frontend/src/api/types.ts."""

    dept: str
    skill: str
    action: str
    version: int | None = None
    from_version: int | None = None
    to_version: int | None = None
    versioned_fqn: str | None = None
    content_hash: str | None = None
    git_sha: str | None = None
    eval_run_id: str | None = None
    actor: str
    job_run_id: str | None = None
    ts: datetime | str


class CandidateSummary(BaseModel):
    """A skill that is not released: under review in a pull request (`kind` pr) or uploaded as a Skill Hub draft
    (`kind` draft). Mirrors CandidateSummary in app/frontend/src/api/types.ts.

    `verdicts` is the newest result of each model across modes, from the candidate's own run keys only. A draft has
    no pull request or commit; `uploaded_by` and `has_rubric` are known for drafts only, and `submitted_pr` is set for
    a draft that was submitted for review."""

    kind: Literal["pr", "draft"]
    run_key: str
    pr_number: int | None
    git_sha: str | None
    uploaded_at: datetime | None
    uploaded_by: str | None = None
    has_rubric: bool | None = None
    submitted_pr: int | None = None
    dept: str
    name: str
    description: str | None
    content_hash: str | None
    verdicts: dict[str, str]
    eval_access: EvalAccess


class CandidateBlock(BaseModel):
    kind: Literal["pr", "draft"]
    pr_number: int | None
    run_key: str
    git_sha: str | None
    uploaded_at: datetime | None
    uploaded_by: str | None = None
    submitted_pr: int | None = None
    submitted_url: str | None = None


class CandidateResult(BaseModel):
    """One eval_runs child row of the candidate: its PR eval or a Hub run started for it."""

    run_key: str
    trigger_source: str | None = None
    skill: str
    model_profile: str
    mode: str
    verdict: str | None = None
    run_id: str | None = None
    experiment_id: str | None = None
    n_cases: int | None = None
    pass_rate: float | None = None
    error_rate: float | None = None
    accuracy: float | None = None
    content_hash: str | None = None
    started_at: datetime | str | None = None


class CandidateDetail(BaseModel):
    """One candidate skill, in the shape of SkillDetail: files, cases and rubric come from the candidate folder."""

    dept: str
    name: str
    description: str | None
    content_hash: str
    verdicts: dict[str, str]
    files: dict[str, str]
    evals: EvalFile | None
    rubric: Rubric | None
    eval_access: EvalAccess
    results: list[CandidateResult]
    candidate: CandidateBlock


class DashboardSummary(BaseModel):
    """The summary tiles. Means are None when no row has the value. Mirrors DashboardSummary in types.ts."""

    skills: int
    verdicts: dict[str, int]
    trigger_accuracy: float | None
    uplift_task_quality: float | None
    error_rate: float | None


class DashboardVerdict(BaseModel):
    dept: str
    model_profile: str
    verdict: str
    n: int


class DashboardUplift(BaseModel):
    dept: str
    skill: str
    skill_version: int
    model_profile: str
    pass_rate_without: float | None
    pass_rate: float | None
    uplift_task_quality: float | None
    uplift_skill_format: float | None


class DashboardRouting(BaseModel):
    """Trigger outcomes of one expected and invoked skill pair, for one model. invoked_skill is None when no skill ran."""

    dept: str
    model_profile: str
    expected_skill: str | None
    invoked_skill: str | None
    trigger_outcome: str | None
    n: int


class DashboardTriggerTrend(BaseModel):
    """Trigger accuracy of one day's runs. `day` is a calendar date, YYYY-MM-DD."""

    dept: str
    skill: str
    skill_version: int
    model_profile: str
    day: str
    accuracy: float | None
    runs: int


class DashboardPassRateTrend(BaseModel):
    dept: str
    skill: str
    skill_version: int
    model_profile: str
    day: str
    pass_rate: float | None
    runs: int


class DashboardHealth(BaseModel):
    """Error and inconclusive rates of one model over its latest gating rows. `cells` is the row count behind them."""

    dept: str
    model_profile: str
    cells: int
    error_rate: float | None
    inconclusive_rate: float | None


class DashboardTokens(BaseModel):
    """One day, endpoint and source. `source` is `eval` (evaluation runs and their judges) or `try` (Try runs)."""

    model_profile: str
    endpoint_name: str
    day: str
    source: Literal["eval", "try"]
    input_tokens: int | None
    output_tokens: int | None


class DashboardOut(BaseModel):
    """GET /api/results/dashboard. `tokens` is None when the system.serving tables cannot be read."""

    summary: DashboardSummary
    verdicts: list[DashboardVerdict]
    uplift: list[DashboardUplift]
    routing: list[DashboardRouting]
    trigger_trend: list[DashboardTriggerTrend]
    pass_rate_trend: list[DashboardPassRateTrend]
    health: list[DashboardHealth]
    tokens: list[DashboardTokens] | None


class SubmitOut(BaseModel):
    """POST /api/drafts/{run_key}/submit. `notes` says what the pull request left out, one plain sentence each."""

    pr_number: int
    url: str
    branch: str
    notes: list[str]


class ReleaseOpened(BaseModel):
    state: Literal["open", "closed"]
    url: str


class ReleaseGate(BaseModel):
    """The eval gate: the skills-pr check of the pull request's head commit. `none` when no check has run, `cancelled`
    when a newer push or a person cancelled it."""

    state: Literal["pending", "passed", "failed", "cancelled", "none"]
    url: str


class ReleaseMerged(BaseModel):
    merged: bool
    sha: str | None
    at: datetime | None


class ReleaseRelease(BaseModel):
    """The skills-publish run of the merge commit. `url` links to the run, which is where a waiting release is approved.
    `cancelled` when a newer run of the release concurrency group replaced it, or a person cancelled it."""

    state: Literal["not_started", "running", "waiting_approval", "succeeded", "failed", "cancelled"]
    url: str | None


class ReleaseLive(BaseModel):
    """The skill's live version from the journal, and whether a switch to it came after the merge. `fqn` is the skill's
    name in Unity Catalog, for its page."""

    fqn: str
    version: int | None
    switched_after_merge: bool
    eval_access: EvalAccess


class ReleaseStatusOut(BaseModel):
    """GET /api/release-status: the steps of one pull request's path to a live version, for one skill."""

    pr_number: int
    dept: str
    skill: str
    opened: ReleaseOpened
    gate: ReleaseGate
    merged: ReleaseMerged
    release: ReleaseRelease
    live: ReleaseLive


class MergeOut(BaseModel):
    pr_number: int
    merged: bool
    sha: str
