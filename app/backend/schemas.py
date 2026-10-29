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
    no pull request or commit; `uploaded_by` and `has_rubric` are known for drafts only."""

    kind: Literal["pr", "draft"]
    run_key: str
    pr_number: int | None
    git_sha: str | None
    uploaded_at: datetime | None
    uploaded_by: str | None = None
    has_rubric: bool | None = None
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
