import os
import uuid

import pytest

from skillhub.auth import workspace_client
from skillhub.config import load_config
from skillhub.naming import eval_schema, evals_schema
from skillhub.results import tables

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("SKILLHUB_IT") != "1", reason="set SKILLHUB_IT=1"),
]


@pytest.fixture
def env():
    from skillhub.sql import SqlClient  # imported late so collection works without it

    cfg = load_config()
    return cfg, SqlClient(workspace_client(), cfg.workspace.sql_warehouse_id)


def test_tables_and_views_apply_twice_and_hold_properties(env):
    cfg, sql = env
    tables.ensure_tables(cfg, sql)
    tables.ensure_views(cfg, sql)
    tables.ensure_tables(cfg, sql)  # idempotent
    tables.ensure_views(cfg, sql)

    dept = next(iter(cfg.departments))
    props = sql.execute(f"SHOW TBLPROPERTIES {evals_schema(cfg, dept)}.eval_runs")
    assert any(p.get("key") == "delta.appendOnly" and p.get("value") == "true" for p in props)

    cols = [r["col_name"] for r in sql.execute(f"DESCRIBE TABLE {eval_schema(cfg)}.matrix_all")]
    assert cols[: len(tables.MATRIX_COLUMNS)] == tables.MATRIX_COLUMNS


def test_append_only_rows_survive_vacuum(env):
    """Spec 6.4: a probe table built from the tables.sql text keeps its rows through VACUUM."""
    cfg, sql = env
    probe = f"{eval_schema(cfg)}.probe_rubrics_{uuid.uuid4().hex[:8]}"
    rubrics_ddl = next(s for s in tables.split_statements(tables.TABLES_SQL) if "{evals}.rubrics" in s)
    try:
        sql.execute(rubrics_ddl.replace("{evals}.rubrics", probe))
        sql.execute(f"INSERT INTO {probe} VALUES ('s', 'h', '{{}}', 'sh', false, current_timestamp())")
        sql.execute(f"VACUUM {probe}")
        assert int(sql.execute(f"SELECT COUNT(*) AS n FROM {probe}")[0]["n"]) == 1
        with pytest.raises(Exception):  # noqa: B017  append-only tables reject DELETE
            sql.execute(f"DELETE FROM {probe}")
    finally:
        sql.execute(f"DROP TABLE IF EXISTS {probe}")
