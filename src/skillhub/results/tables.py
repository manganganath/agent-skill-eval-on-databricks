"""Results tables and views: DDL from tables.sql / views.sql, applied per department."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from skillhub.config import HubConfig
from skillhub.naming import eval_schema, evals_schema

if TYPE_CHECKING:
    from skillhub.sql import SqlClient

_DIR = Path(__file__).parent
TABLES_SQL = (_DIR / "tables.sql").read_text(encoding="utf-8")
VIEWS_SQL = (_DIR / "views.sql").read_text(encoding="utf-8")

# Column order of eval_matrix and matrix_all. experiment_id links the UI to MLflow.
MATRIX_COLUMNS = [
    "run_id",
    "dept",
    "skill",
    "skill_fqn",
    "skill_version",
    "content_hash",
    "model_profile",
    "harness",
    "endpoint",
    "model_class",
    "mode",
    "verdict",
    "pass_rate",
    "spread",
    "error_rate",
    "n_cases",
    "n_reviewed",
    "unreviewed_cases",
    "accuracy",
    "precision",
    "recall",
    "pass_rate_without",
    "uplift_task_quality",
    "uplift_skill_format",
    "planted_bugs_recall",
    "started_at",
    "run_key",
    "trigger_source",
    "experiment_id",
    "profile_kind",
]

# The rows every gate and alert reads: hub.yaml profiles on the built-in agent loop. Rows written before either
# column existed read as such.
RELEASE_GATE = (
    "COALESCE(harness, 'reference') = 'reference' AND COALESCE(profile_kind, 'hub_yaml') = 'hub_yaml'"
)

_TABLE_BLOCK = re.compile(r"CREATE TABLE IF NOT EXISTS \{evals\}\.(\w+) \((.*?)\)\s*TBLPROPERTIES", re.S)


def split_statements(text: str) -> list[str]:
    return [s.strip() for s in text.split(";") if s.strip()]


def table_columns() -> dict[str, list[str]]:
    """Column names of each results table, parsed from tables.sql."""
    return {
        name: [c.split()[0].strip("`") for c in body.split(",") if c.strip()]
        for name, body in _TABLE_BLOCK.findall(TABLES_SQL)
    }


def ensure_tables(cfg: HubConfig, sql: SqlClient) -> None:
    """The results tables in each `<dept>_evals` schema. Bootstrap creates the schemas first; the
    skillhub-bootstrap Job runs this as ci, which has no CREATE SCHEMA, so no schema is created here."""
    for dept in cfg.departments:
        ev = evals_schema(cfg, dept)
        for stmt in split_statements(TABLES_SQL):
            sql.execute(stmt.format(evals=ev))
        _add_run_columns(sql, ev)
        if cfg.retention.history_days:
            _keep_history(sql, ev, cfg.retention.history_days)


def _keep_history(sql: SqlClient, ev: str, days: int) -> None:
    """Set delta.logRetentionDuration on each results table, so DESCRIBE HISTORY lists every write for `days`. A
    table that already has the value is left alone, so a bootstrap that changes nothing writes nothing."""
    from skillhub.sql import SqlError

    want = f"interval {days} days"
    for table in table_columns():
        rows = sql.execute(f"SHOW TBLPROPERTIES {ev}.{table} ('delta.logRetentionDuration')")
        if any(str(r.get("value", "")).strip().lower() == want for r in rows):
            continue
        alter = f"ALTER TABLE {ev}.{table} SET TBLPROPERTIES ('delta.logRetentionDuration' = '{want}')"
        try:
            sql.execute(alter)
        except SqlError as e:
            raise SqlError(
                f"cannot keep the write history of {ev}.{table} ({e}). Its owner can run: {alter}"
            ) from e


# Columns added to eval_runs after the tables were first created: harness, run_author, and the model catalog's.
_ADDED_RUN_COLUMNS = (
    ("harness", "STRING"),
    ("run_author", "STRING"),
    ("profile_kind", "STRING"),
    ("route", "STRING"),
    ("model_id", "STRING"),
    ("region", "STRING"),
    ("catalog_version", "BIGINT"),
)


def _add_run_columns(sql: SqlClient, ev: str) -> None:
    """Older tables get each added column with an additive ALTER (no default: old rows read NULL).

    A NULL run_author is never trusted, so rows synced before run_author existed never pass a gate.
    """
    present = {str(r.get("col_name", "")).lower() for r in sql.execute(f"DESCRIBE TABLE {ev}.eval_runs")}
    for name, kind in _ADDED_RUN_COLUMNS:
        if name not in present:
            sql.execute(f"ALTER TABLE {ev}.eval_runs ADD COLUMNS ({name} {kind})")


_DEPT_LITERAL = re.compile(r"^[a-z0-9_]+$")


# Rows written before the harness column existed read as the built-in agent loop, and rows written before the model
# catalog as hub.yaml profiles.
_COALESCED = {
    "harness": "COALESCE(`harness`, 'reference') AS `harness`",
    "profile_kind": "COALESCE(`profile_kind`, 'hub_yaml') AS `profile_kind`",
}


def _union_view(
    sql: SqlClient,
    schema: str,
    name: str,
    sources: list[tuple[str, str]],
    columns: list[str],
    *,
    add_dept: bool = False,
) -> None:
    """UNION ALL of (dept, table) sources. With add_dept, each branch starts with '<dept>' AS dept."""
    cols = ", ".join(_COALESCED.get(c, f"`{c}`") for c in columns)
    branches = []
    for dept, src in sources:
        if add_dept:
            if not _DEPT_LITERAL.match(dept):
                raise ValueError(f"department name {dept!r} is not safe to use as a SQL literal")
            branches.append(f"SELECT '{dept}' AS dept, {cols} FROM {src}")
        else:
            branches.append(f"SELECT {cols} FROM {src}")
    body = "\nUNION ALL\n".join(branches)
    sql.execute(f"CREATE OR REPLACE VIEW {schema}.{name} AS\n{body}")


def ensure_views(cfg: HubConfig, sql: SqlClient) -> None:
    """Needs ensure_tables first: the views read the tables. Like ensure_tables, it creates no schema."""
    ev_schema = eval_schema(cfg)
    depts = list(cfg.departments)
    for dept in depts:
        ev = evals_schema(cfg, dept)
        for stmt in split_statements(VIEWS_SQL):
            sql.execute(stmt.format(evals=ev))
    cols = table_columns()
    _union_view(
        sql,
        ev_schema,
        "matrix_all",
        [(d, f"{evals_schema(cfg, d)}.eval_matrix") for d in depts],
        MATRIX_COLUMNS,
    )
    _union_view(
        sql,
        ev_schema,
        "runs_all",
        [(d, f"{evals_schema(cfg, d)}.eval_runs") for d in depts],
        cols["eval_runs"],
    )
    # eval_cases has no dept column, so each branch adds the literal. eval_runs already has dept.
    _union_view(
        sql,
        ev_schema,
        "cases_all",
        [(d, f"{evals_schema(cfg, d)}.eval_cases") for d in depts],
        cols["eval_cases"],
        add_dept=True,
    )
    # The version journal of every department, for the switch-failed alert. It already has dept.
    _union_view(
        sql,
        ev_schema,
        "versions_all",
        [(d, f"{evals_schema(cfg, d)}.skill_versions") for d in depts],
        cols["skill_versions"],
    )
    # Cross-department views are reviewer-only. Bootstrap also grants USE SCHEMA and SELECT on
    # schema eval to reviewers only; these per-view grants are redundant but kept.
    for view in ("runs_all", "cases_all", "versions_all"):
        sql.execute(f"GRANT SELECT ON VIEW {ev_schema}.{view} TO `{cfg.roles.reviewers}`")
