from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from skillhub.config import HubConfig
from skillhub.naming import evals_schema
from skillhub.sql import SqlExecutor

Action = Literal[
    "published", "publish_refused", "switch_started", "switched", "switch_failed", "retired", "drift"
]

_COLS = (
    "dept",
    "skill",
    "action",
    "version",
    "from_version",
    "to_version",
    "versioned_fqn",
    "content_hash",
    "git_sha",
    "eval_run_id",
    "actor",
    "job_run_id",
    "ts",
)
_INT_COLS = ("version", "from_version", "to_version")
_SELECT = ", ".join(_COLS)


@dataclass
class JournalRow:
    dept: str
    skill: str
    action: Action
    version: int | None
    from_version: int | None
    to_version: int | None
    versioned_fqn: str | None
    content_hash: str | None
    git_sha: str | None
    eval_run_id: str | None
    actor: str
    job_run_id: str | None
    ts: datetime


def new_row(
    dept: str,
    skill: str,
    action: Action,
    *,
    actor: str,
    job_run_id: str | None = None,
    version: int | None = None,
    from_version: int | None = None,
    to_version: int | None = None,
    versioned_fqn: str | None = None,
    content_hash: str | None = None,
    git_sha: str | None = None,
    eval_run_id: str | None = None,
) -> JournalRow:
    return JournalRow(
        dept=dept,
        skill=skill,
        action=action,
        version=version,
        from_version=from_version,
        to_version=to_version,
        versioned_fqn=versioned_fqn,
        content_hash=content_hash,
        git_sha=git_sha,
        eval_run_id=eval_run_id,
        actor=actor,
        job_run_id=job_run_id,
        ts=datetime.now(UTC),
    )


def _to_row(d: dict) -> JournalRow:
    ts = d["ts"]
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    return JournalRow(**{**d, "ts": ts})


class Journal:
    """skill_versions per department. Only INSERT and SELECT: nothing updates or deletes a row."""

    TABLE = "skill_versions"

    def __init__(self, cfg: HubConfig, sql: SqlExecutor) -> None:
        self._cfg = cfg
        self._sql = sql

    def _t(self, dept: str) -> str:
        return f"{evals_schema(self._cfg, dept)}.{self.TABLE}"

    def append(self, row: JournalRow) -> None:
        marks = ", ".join(f"CAST(:{c} AS INT)" if c in _INT_COLS else f":{c}" for c in _COLS)
        params = {c: getattr(row, c) for c in _COLS}
        self._sql.execute(f"INSERT INTO {self._t(row.dept)} ({', '.join(_COLS)}) VALUES ({marks})", params)

    def history(self, dept: str, skill: str) -> list[JournalRow]:
        rows = self._sql.execute(
            f"SELECT {_SELECT} FROM {self._t(dept)} WHERE dept = :dept AND skill = :skill ORDER BY ts",
            {"dept": dept, "skill": skill},
        )
        return [_to_row(r) for r in rows]

    def latest_version(self, dept: str, skill: str) -> int:
        rows = self._sql.execute(
            f"SELECT MAX(version) AS v FROM {self._t(dept)} "
            "WHERE dept = :dept AND skill = :skill AND action = 'published'",
            {"dept": dept, "skill": skill},
        )
        return int(rows[0]["v"] or 0)

    def version_by_hash(self, dept: str, skill: str, content_hash: str) -> int | None:
        rows = self._sql.execute(
            f"SELECT version FROM {self._t(dept)} WHERE dept = :dept AND skill = :skill "
            "AND action = 'published' AND content_hash = :h ORDER BY ts DESC LIMIT 1",
            {"dept": dept, "skill": skill, "h": content_hash},
        )
        return rows[0]["version"] if rows else None

    def live_state(self, dept: str, skill: str) -> JournalRow | None:
        """The last switched or retired row. A published row does not make a skill live."""
        rows = self._sql.execute(
            f"SELECT {_SELECT} FROM {self._t(dept)} WHERE dept = :dept AND skill = :skill "
            "AND action IN ('switched', 'retired') ORDER BY ts DESC LIMIT 1",
            {"dept": dept, "skill": skill},
        )
        return _to_row(rows[0]) if rows else None

    def skills(self, dept: str) -> list[str]:
        rows = self._sql.execute(
            f"SELECT DISTINCT skill FROM {self._t(dept)} WHERE dept = :dept ORDER BY skill",
            {"dept": dept},
        )
        return [r["skill"] for r in rows]
