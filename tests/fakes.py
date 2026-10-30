"""In-memory stand-ins for the Databricks-facing clients.

Other tests add to this file and never redefine these classes.
"""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import yaml

from skillhub.config import HubConfig, load_config
from skillhub.registry.uc_skills import SkillInfo

_NOOP = re.compile(
    r"^\s*(GRANT|REVOKE|CREATE\s+SCHEMA|CREATE\s+VOLUME|CREATE\s+(OR\s+REPLACE\s+)?VIEW|ALTER\s|COMMENT\s|OPTIMIZE|MERGE\s|SET\s|USE\s)",
    re.I,
)
_TABLE = re.compile(
    r"\b(FROM|INTO|TABLE|JOIN|EXISTS|VIEW|UPDATE)(\s+(?:IF\s+NOT\s+EXISTS\s+)?)([A-Za-z_]\w*(?:\.[A-Za-z_]\w*){1,2})\b",
    re.I,
)
_INSERT = re.compile(r"^\s*INSERT\b", re.I)
_DELTA_CLAUSES = re.compile(
    r"\s+(?:USING\s+DELTA|(?:TBLPROPERTIES|CLUSTER\s+BY|PARTITIONED\s+BY)\s*\([^)]*\))", re.I
)
_READ_OR_INSERT = re.compile(r"^\s*(SELECT|WITH|INSERT)\b", re.I)
# Databricks `SELECT * EXCEPT (a, b)`: sqlite runs `*`, and execute() drops a and b from the rows.
_STAR_EXCEPT = re.compile(r"\*\s+EXCEPT\s*\(([^)]*)\)", re.I)
_DDL_TYPES = re.compile(r"\b(STRING|TIMESTAMP)\b")
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---", re.S)


def make_cfg() -> HubConfig:
    """Load config/hub.yaml with test values for its ${VARS}, without touching os.environ."""
    env = {
        "DATABRICKS_HOST": "https://example.invalid",
        "SQL_WAREHOUSE_ID": "test-warehouse",
        "SKILLHUB_CATALOG": "skill_hub",
    }
    with patch.dict(os.environ, env):
        return load_config(Path(__file__).resolve().parents[1] / "config" / "hub.yaml")


def _etag() -> str:
    return secrets.token_hex(8)


class FakeUCSkills:
    """In-memory UCSkills with the same methods. Uploads are visible at once, as in Unity Catalog.

    Etags change on metadata writes (create, finalize, update_comment) and not on uploads.
    Test controls:
      fail_once(method)                  the next call to `method` raises (a crash)
      tamper_after_finalize(fqn, n)      the next n read-backs right after a finalize return a stale SKILL.md
      external_edit(fqn, rel, data)      a change made outside the pipeline (no journal row, no etag change)
    """

    def __init__(self) -> None:
        self.schemas: set[str] = set()
        self._skills: dict[str, dict[str, Any]] = {}
        self._fail_once: set[str] = set()
        self._tamper: dict[str, int] = {}
        self._just_finalized: set[str] = set()

    def fail_once(self, method: str) -> None:
        self._fail_once.add(method)

    def tamper_after_finalize(self, fqn: str, times: int) -> None:
        self._tamper[fqn] = times

    def external_edit(self, fqn: str, rel: str, data: bytes) -> None:
        self._skills[fqn]["files"][rel] = data

    def _maybe_fail(self, method: str) -> None:
        if method in self._fail_once:
            self._fail_once.discard(method)
            raise RuntimeError(f"injected failure in {method}")

    def _check_etag(self, fqn: str, etag: str | None) -> None:
        if etag is not None and etag != self._skills[fqn]["etag"]:
            raise RuntimeError(f"etag mismatch for {fqn}")

    def _info(self, fqn: str) -> SkillInfo:
        s = self._skills[fqn]
        return SkillInfo(
            fqn=fqn,
            name=fqn.rsplit(".", 1)[1],
            bundle_name=s["bundle_name"],
            description=s["description"],
            etag=s["etag"],
            finalize_time=s["finalize_time"],
            comment=s["comment"],
        )

    def ensure_schema(self, schema_fqn: str) -> None:
        self.schemas.add(schema_fqn)

    def create(self, schema_fqn: str, skill_id: str, comment: str = "") -> SkillInfo:
        self._maybe_fail("create")
        fqn = f"{schema_fqn}.{skill_id}"
        if fqn in self._skills:
            raise RuntimeError(f"{fqn} already exists")
        self._skills[fqn] = {
            "files": {},
            "etag": _etag(),
            "comment": comment,
            "bundle_name": None,
            "description": None,
            "finalize_time": None,
            "tags": {},
        }
        return self._info(fqn)

    def get(self, fqn: str) -> SkillInfo | None:
        return self._info(fqn) if fqn in self._skills else None

    def list(self, schema_fqn: str) -> list[SkillInfo]:
        return [self._info(f) for f in sorted(self._skills) if f.rsplit(".", 1)[0] == schema_fqn]

    def finalize(self, fqn: str) -> SkillInfo:
        self._maybe_fail("finalize")
        s = self._skills[fqn]
        text = s["files"].get("SKILL.md", b"").decode("utf-8")
        m = _FRONTMATTER.match(text)
        meta = yaml.safe_load(m.group(1)) if m else None
        if not isinstance(meta, dict) or not meta.get("name") or not meta.get("description"):
            raise RuntimeError(f"{fqn}: SKILL.md has no valid frontmatter")
        s.update(
            bundle_name=str(meta["name"]),
            description=str(meta["description"]),
            finalize_time=datetime.now(UTC).isoformat(),
            etag=_etag(),
        )
        self._just_finalized.add(fqn)
        return self._info(fqn)

    def update_comment(self, fqn: str, comment: str, etag: str) -> SkillInfo:
        self._check_etag(fqn, etag)
        self._skills[fqn].update(comment=comment, etag=_etag())
        return self._info(fqn)

    def delete(self, fqn: str, etag: str | None = None) -> None:
        self._maybe_fail("delete")
        self._check_etag(fqn, etag)
        del self._skills[fqn]

    def upload_file(self, fqn: str, rel_path: str, data: bytes) -> None:
        self._maybe_fail("upload_file")
        self._skills[fqn]["files"][rel_path] = bytes(data)

    def delete_file(self, fqn: str, rel_path: str) -> None:
        self._skills[fqn]["files"].pop(rel_path, None)

    def read_files(self, fqn: str) -> dict[str, bytes]:
        self._maybe_fail("read_files")
        if fqn not in self._skills:
            return {}
        files = dict(self._skills[fqn]["files"])
        if fqn in self._just_finalized:
            self._just_finalized.discard(fqn)
            if self._tamper.get(fqn, 0) > 0:
                self._tamper[fqn] -= 1
                files["SKILL.md"] = files.get("SKILL.md", b"") + b"\n<!-- stale -->\n"
        return files

    def set_tag(self, fqn: str, key: str, value: str) -> None:
        self._skills[fqn]["tags"][key] = value

    def get_tags(self, fqn: str) -> dict[str, str]:
        return dict(self._skills[fqn]["tags"]) if fqn in self._skills else {}


def _bind(v: object) -> object:
    return v.isoformat() if isinstance(v, datetime) else v


def _translate(stmt: str) -> str:
    s = _DELTA_CLAUSES.sub("", stmt)
    s = s.replace("`", '"')
    s = _TABLE.sub(lambda m: f'{m.group(1)}{m.group(2)}"{m.group(3)}"', s)
    if s.lstrip().upper().startswith("CREATE TABLE"):
        s = _DDL_TYPES.sub("TEXT", s)
    return s


class FakeSql:
    """sqlite3-backed stand-in for SqlClient.execute(statement, params).

    Every call is recorded in `.calls` (`.statements` is an alias). Statements it does not interpret
    return []. INSERT returns [{"num_affected_rows": n}]. Supports CREATE TABLE, INSERT (VALUES and INSERT..SELECT..WHERE NOT EXISTS), SELECT, with :name
    parameters. GRANT, REVOKE, CREATE SCHEMA/VOLUME/VIEW, ALTER, COMMENT, OPTIMIZE and USE are recorded and ignored.
    `SELECT * EXCEPT (cols)` is run as `SELECT *`, and the excepted columns are left out of the rows. Other
    Databricks-only functions (from_json, VARIANT) are not supported.
    """

    def __init__(self) -> None:
        self._db = sqlite3.connect(":memory:")
        self._db.row_factory = sqlite3.Row
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @property
    def statements(self) -> list[tuple[str, dict[str, Any]]]:
        return self.calls

    def execute(self, statement: str, params: Mapping[str, object] | None = None) -> list[dict[str, Any]]:
        raw = dict(params or {})
        self.calls.append((statement, raw))
        if _NOOP.match(statement):
            return []
        if "information_schema." in statement.lower():
            return []  # the fake models no catalog metadata; tests that need rows subclass it
        excepted = {c.strip().strip("`") for m in _STAR_EXCEPT.finditer(statement) for c in m[1].split(",")}
        try:
            cur = self._db.execute(
                _translate(_STAR_EXCEPT.sub("*", statement)), {k: _bind(v) for k, v in raw.items()}
            )
        except sqlite3.OperationalError:
            if _READ_OR_INSERT.match(statement):
                raise  # real query bugs must not be hidden
            return []  # a statement the fake does not interpret
        rows = (
            [{k: v for k, v in dict(r).items() if k not in excepted} for r in cur.fetchall()]
            if cur.description
            else []
        )
        if _INSERT.match(statement):
            rows = [{"num_affected_rows": cur.rowcount}]
        self._db.commit()
        return rows


class FakeModelClient:
    """Returns scripted ChatResult objects in order and records what it was sent."""

    def __init__(self, script: Sequence[Any]) -> None:
        self.profile_name = "fake-model"
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.tools_seen: list[list[dict[str, Any]]] = []

    def chat(self, messages, tools, *, max_tokens=None, trigger=False):
        self.tools_seen.append(
            [
                t
                if isinstance(t, dict)
                else {"name": t.name, "description": t.description, "parameters": t.parameters}
                for t in tools
            ]
        )
        self.calls.append(
            {"messages": list(messages), "tools": list(tools), "max_tokens": max_tokens, "trigger": trigger}
        )
        if not self._script:
            raise AssertionError("FakeModelClient script exhausted")
        return self._script.pop(0)


class FakeSkillsMcp:
    """tools maps name -> (description, text returned by call_tool). Async context manager like SkillsMcpClient."""

    def __init__(self, tools: Mapping[str, tuple[str, str]]) -> None:
        self._tools = dict(tools)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> FakeSkillsMcp:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def list_tools(self):
        from skillhub.gateway.skills_mcp import McpTool

        return [
            McpTool(name=n, description=d, input_schema={"type": "object", "properties": {}})
            for n, (d, _) in self._tools.items()
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if name not in self._tools:
            raise KeyError(name)
        self.calls.append((name, dict(arguments)))
        return self._tools[name][1]
