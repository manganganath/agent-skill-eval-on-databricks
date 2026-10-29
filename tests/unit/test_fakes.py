import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from tests.fakes import FakeModelClient, FakeSkillsMcp, FakeSql, FakeUCSkills

SKILL_MD = b"---\nname: demo\ndescription: d\n---\nbody\n"


def test_fake_uc_etags_change_on_metadata_writes_only():
    uc = FakeUCSkills()
    uc.ensure_schema("cat.sch")
    created = uc.create("cat.sch", "demo")
    uc.upload_file("cat.sch.demo", "SKILL.md", SKILL_MD)
    assert uc.get("cat.sch.demo").etag == created.etag  # uploads do not change the etag
    fin = uc.finalize("cat.sch.demo")
    assert fin.description == "d" and fin.etag != created.etag
    with pytest.raises(RuntimeError):
        uc.delete("cat.sch.demo", etag=created.etag)  # stale etag
    uc.delete("cat.sch.demo", etag=fin.etag)
    assert uc.get("cat.sch.demo") is None


def test_fake_sql_roundtrip_with_named_params():
    sql = FakeSql()
    sql.execute(
        "CREATE TABLE IF NOT EXISTS cat.sch.t (a STRING, n INT, ts TIMESTAMP) "
        "TBLPROPERTIES ('delta.appendOnly' = 'true')"
    )
    sql.execute(
        "INSERT INTO cat.sch.t (a, n, ts) VALUES (:a, :n, :ts)",
        {"a": "x", "n": 3, "ts": datetime(2026, 10, 28, tzinfo=UTC)},
    )
    rows = sql.execute("SELECT a, n FROM cat.sch.t WHERE a = :a", {"a": "x"})
    assert rows == [{"a": "x", "n": 3}]


def test_fake_sql_records_and_ignores_grants():
    sql = FakeSql()
    sql.execute("GRANT READ VOLUME ON SCHEMA cat.sch TO `g`")
    assert sql.statements[0][0].startswith("GRANT")


def test_fake_model_client_replays_script_and_records_calls():
    mc = FakeModelClient([SimpleNamespace(text="hi")])
    assert mc.chat([{"role": "user", "content": "x"}], [], trigger=True).text == "hi"
    assert mc.calls[0]["trigger"] is True
    with pytest.raises(AssertionError):
        mc.chat([], [])


def test_fake_skills_mcp_call_tool_returns_text():
    async def go():
        async with FakeSkillsMcp({"skill_cat__sch__x": ("d", "BODY")}) as m:
            return await m.call_tool("skill_cat__sch__x", {})

    assert asyncio.run(go()) == "BODY"


def test_fake_sql_calls_alias_and_insert_count():
    sql = FakeSql()
    sql.execute("CREATE TABLE t.s.a (id STRING, v INT)")
    out = sql.execute("INSERT INTO t.s.a (id, v) VALUES (:i, :v)", {"i": "1", "v": 2})
    assert out == [{"num_affected_rows": 1}]
    assert sql.calls is sql.statements and len(sql.calls) == 2


def test_fake_sql_noops_and_unknown_return_empty():
    sql = FakeSql()
    for stmt in (
        "CREATE OR REPLACE VIEW c.s.v AS SELECT 1",
        "ALTER TABLE c.s.a SET TBLPROPERTIES ('x' = 'y')",
        "COMMENT ON TABLE c.s.a IS 'x'",
        "OPTIMIZE c.s.a",
        "DROP TABLE c.s.never_created",  # not a no-op: sqlite rejects it, the fake returns []
    ):
        assert sql.execute(stmt) == []
    assert len(sql.calls) == 5


def test_fake_sql_aliased_insert_select_where_not_exists():
    sql = FakeSql()
    sql.execute("CREATE TABLE c.s.t (run_id STRING, n INT)")
    ins = (
        "INSERT INTO c.s.t (run_id, n) SELECT :r, :n WHERE NOT EXISTS "
        "(SELECT 1 FROM c.s.t AS x WHERE x.run_id = :r)"
    )
    assert sql.execute(ins, {"r": "a", "n": 1}) == [{"num_affected_rows": 1}]
    assert sql.execute(ins, {"r": "a", "n": 2}) == [{"num_affected_rows": 0}]
    assert sql.execute("SELECT run_id, n FROM c.s.t") == [{"run_id": "a", "n": 1}]
    # an alias reference must stay a column, not become a string literal
    assert sql.execute("SELECT t.n FROM c.s.t AS t WHERE t.run_id = :r", {"r": "a"}) == [{"n": 1}]


def test_make_cfg_helper():
    from tests.fakes import make_cfg

    assert make_cfg().catalog == "skill_hub"


def test_fake_model_client_calls_carry_messages_and_tools():
    mc = FakeModelClient([SimpleNamespace(text="a")])
    mc.chat([{"role": "user", "content": "x"}], [{"name": "t"}])
    assert mc.calls[0]["messages"] == [{"role": "user", "content": "x"}]
    assert mc.calls[0]["tools"] == [{"name": "t"}]


def test_fake_sql_delta_ddl_builds_a_usable_table():
    sql = FakeSql()
    sql.execute(
        "CREATE TABLE IF NOT EXISTS c.s.d (a STRING, n INT) USING DELTA "
        "CLUSTER BY (a) TBLPROPERTIES ('delta.appendOnly'='true')"
    )
    sql.execute("CREATE TABLE c.s.p (a STRING) USING DELTA PARTITIONED BY (a)")
    sql.execute("INSERT INTO c.s.d (a, n) VALUES (:a, :n)", {"a": "x", "n": 1})
    assert sql.execute("SELECT a, n FROM c.s.d") == [{"a": "x", "n": 1}]


def test_fake_sql_merge_set_and_unknown_statements_return_empty():
    sql = FakeSql()
    assert sql.execute("MERGE INTO c.s.d t USING c.s.e s ON t.a = s.a WHEN MATCHED THEN DELETE") == []
    assert sql.execute("SET spark.sql.x = 1") == []
    assert sql.execute("DROP TABLE c.s.nonexistent") == []  # sqlite rejects it; the fake returns []
    assert sql.execute("VACUUM c.s.d") == []
    assert len(sql.calls) == 4


def test_fake_sql_select_and_insert_errors_still_raise():
    import sqlite3

    sql = FakeSql()
    with pytest.raises(sqlite3.OperationalError):
        sql.execute("SELECT a FROM c.s.missing")
    with pytest.raises(sqlite3.OperationalError):
        sql.execute("INSERT INTO c.s.missing (a) VALUES (1)")


def test_fake_sql_runs_star_except_and_drops_those_columns():
    """Databricks `SELECT * EXCEPT (cols)`, as the merge gate's reader uses it: sqlite runs `*`, and the fake
    leaves the excepted columns out of every row, so rows have the shape of the real result."""
    sql = FakeSql()
    sql.execute("CREATE TABLE c.s.t (a STRING, b INT)")
    sql.execute("CREATE TABLE c.s.u (a STRING, b INT)")
    sql.execute("INSERT INTO c.s.t (a, b) VALUES ('x', 1)")
    sql.execute("INSERT INTO c.s.u (a, b) VALUES ('y', 2)")
    q = (
        "SELECT * EXCEPT (rn) FROM (SELECT a, b, ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) AS rn FROM c.s.t) "
        "WHERE rn = 1 UNION ALL SELECT * EXCEPT (`rn`) FROM (SELECT a, b, 1 AS rn FROM c.s.u) WHERE rn = 1"
    )
    assert sql.execute(q) == [{"a": "x", "b": 1}, {"a": "y", "b": 2}]
    assert sql.execute("SELECT * EXCEPT (b, rn) FROM (SELECT a, b, 1 AS rn FROM c.s.t)") == [{"a": "x"}]
