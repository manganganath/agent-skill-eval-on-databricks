"""The model catalog table, `<catalog>.eval.model_profiles`: its DDL, its reads, and the writes the Skill Hub makes.

One row per eval target. A row is disabled, never deleted, so old runs can still name their profile, and Delta
history keeps every change. Bootstrap creates the table. The eval and probe Jobs (skillhub-ci-eval) only read it;
the Skill Hub app, after its reviewer check, is the only writer. No statement here deletes a row.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from skillhub.config import HubConfig
from skillhub.model_catalog.entry import CatalogEntry, Target
from skillhub.naming import eval_schema
from skillhub.sql import SqlExecutor

log = logging.getLogger(__name__)

TABLE = "model_profiles"
COLUMNS: list[tuple[str, str]] = [
    ("name", "STRING"),
    ("kind", "STRING"),
    ("route", "STRING"),
    ("endpoint", "STRING"),
    ("model_id", "STRING"),
    ("region", "STRING"),
    ("model_class", "STRING"),
    ("harness", "STRING"),
    ("agent", "STRING"),
    ("price_in", "DOUBLE"),
    ("price_out", "DOUBLE"),
    ("enabled", "BOOLEAN"),
    ("tested_at", "TIMESTAMP"),
    ("test_result", "STRING"),
    ("created_by", "STRING"),
    ("created_at", "TIMESTAMP"),
    ("updated_by", "STRING"),
    ("updated_at", "TIMESTAMP"),
]
NAMES = [n for n, _ in COLUMNS]
# What a reviewer may change on an existing row. name, kind, route and harness are fixed when the row is added.
EDITABLE = ("endpoint", "model_id", "region", "model_class", "agent", "price_in", "price_out", "enabled")


def table_name(cfg: HubConfig) -> str:
    return f"{eval_schema(cfg)}.{TABLE}"


def ddl(cfg: HubConfig) -> str:
    return f"CREATE TABLE IF NOT EXISTS {table_name(cfg)} ({', '.join(f'{n} {t}' for n, t in COLUMNS)})"


def ensure_table(cfg: HubConfig, sql: SqlExecutor) -> None:
    """Create the table. Bootstrap runs this after the `eval` schema exists; it creates no schema."""
    sql.execute(ddl(cfg))


def current_version(sql: SqlExecutor, cfg: HubConfig) -> int | None:
    """The table's latest Delta version, or None when it cannot be read (the run then records no version)."""
    try:
        rows = sql.execute(f"DESCRIBE HISTORY {table_name(cfg)} LIMIT 1")
    except Exception as exc:  # the read below still decides whether the catalog can be used
        log.warning("model catalog history unavailable: %s", type(exc).__name__)
        return None
    raw = rows[0].get("version") if rows else None
    return int(raw) if raw is not None else None


def read_entries(sql: SqlExecutor, cfg: HubConfig) -> tuple[list[CatalogEntry], int | None]:
    """Every row, enabled or not, and the Delta version they were read at.

    The rows are read at that version, so a run records exactly the catalog it used. A name is taken once, from its
    oldest row: two reviewers adding one name at the same moment cannot make two profiles. A row that does not
    parse is skipped with a warning."""
    version = current_version(sql, cfg)
    at = f" VERSION AS OF {version}" if version is not None else ""
    rows = sql.execute(f"SELECT {', '.join(NAMES)} FROM {table_name(cfg)}{at} ORDER BY name, created_at")
    entries: list[CatalogEntry] = []
    seen: set[str] = set()
    for row in rows:
        try:
            entry = CatalogEntry.model_validate(row)
        except ValidationError:
            log.warning("skipped a model catalog row that does not parse: %r", row.get("name"))
            continue
        if entry.name not in seen:
            seen.add(entry.name)
            entries.append(entry)
    return entries, version


def _value(column: str, value: object, params: dict[str, Any]) -> str:
    """The SQL for one value: a NULL literal for None, else a typed parameter."""
    if value is None:
        return "NULL"
    params[column] = value
    return f":{column}"


def insert_entry(sql: SqlExecutor, cfg: HubConfig, entry: CatalogEntry, actor: str, now: datetime) -> bool:
    """Add a row unless one with its name exists. False when the name is taken."""
    t = table_name(cfg)
    params: dict[str, Any] = {"name": entry.name}
    values = {
        **{
            c: getattr(entry, c)
            for c in NAMES
            if c not in ("created_by", "created_at", "updated_by", "updated_at")
        },
        "tested_at": None,
        "test_result": None,
        "created_by": actor,
        "created_at": now,
        "updated_by": actor,
        "updated_at": now,
    }
    select = ", ".join(_value(c, values[c], params) for c in NAMES)
    rows = sql.execute(
        f"INSERT INTO {t} ({', '.join(NAMES)}) SELECT {select} FROM (SELECT 1 AS one) AS one "
        f"WHERE NOT EXISTS (SELECT 1 FROM {t} WHERE name = :name)",
        params,
    )
    return bool(rows) and int(rows[0].get("num_affected_rows") or 0) > 0


def update_entry(
    sql: SqlExecutor, cfg: HubConfig, entry: CatalogEntry, actor: str, now: datetime, *, clear_test: bool
) -> None:
    """Write the editable columns of the row. `clear_test` drops its last test, after its target changed."""
    params: dict[str, Any] = {"name": entry.name}
    sets = [f"{c} = {_value(c, getattr(entry, c), params)}" for c in EDITABLE]
    if clear_test:
        sets += ["tested_at = NULL", "test_result = NULL"]
    sets += [
        f"updated_by = {_value('updated_by', actor, params)}",
        f"updated_at = {_value('updated_at', now, params)}",
    ]
    sql.execute(f"UPDATE {table_name(cfg)} SET {', '.join(sets)} WHERE name = :name", params)


def record_test(
    sql: SqlExecutor, cfg: HubConfig, name: str, target: Target, result: str, tested_at: datetime
) -> None:
    """Store a connection test, only on the row it tested: same name and same target. A result already stored is
    not written again, so polling a finished test changes nothing."""
    params: dict[str, Any] = {"name": name, "result": result, "tested_at": tested_at}
    where = ["name = :name", "COALESCE(test_result, '') <> :result"]
    for column, value in target.params().items():
        params[f"t_{column}"] = value
        where.append(f"COALESCE({column}, '') = :t_{column}")
    sql.execute(
        f"UPDATE {table_name(cfg)} SET test_result = :result, tested_at = :tested_at WHERE {' AND '.join(where)}",
        params,
    )
