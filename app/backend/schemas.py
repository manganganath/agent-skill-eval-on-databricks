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
