import logging
from datetime import datetime

import pytest
from app_fakes import FakeSqlClient
from backend import candidates, deps, queries, skill_ref
from backend.data import SqlDenied, SqlError, SqlMissingColumn, UserSql
from conftest import make_cfg
from databricks.sdk.errors import DatabricksError, NotFound, PermissionDenied

from skillhub.sql import SqlError as SkillhubSqlError


def test_user_input_is_bound_not_interpolated():
    evil = "x'; DROP TABLE skill_hub.common_evals.eval_matrix; --"
    sql, params = queries.matrix_sql(make_cfg(), "common", skill=evil)
    client = FakeSqlClient([[]])
    UserSql(client).query(sql, params)
    statement, bound = client.calls[0]
    assert evil not in statement
    assert bound == {"skill": evil}


def test_rows_pass_through_from_sql_client():
    rows = UserSql(FakeSqlClient([[{"skill_version": 3, "pass_rate": 0.75}]])).query("SELECT 1")
    assert rows == [{"skill_version": 3, "pass_rate": 0.75}]


def test_permission_denied_maps_to_sql_denied():
    with pytest.raises(SqlDenied):
        UserSql(FakeSqlClient([PermissionDenied("[INSUFFICIENT_PRIVILEGES] no SELECT")])).query("SELECT 1")


def test_sdk_permission_denied_without_an_object_marker_is_a_fault():
    # No CAN_USE on the warehouse, or a missing scope: not "you can't read this department".
    with pytest.raises(SqlError) as ei:
        UserSql(FakeSqlClient([PermissionDenied("User is not authorized to use this warehouse")])).query(
            "SELECT 1"
        )
    assert not isinstance(ei.value, SqlDenied)


@pytest.mark.parametrize(
    ("marker", "not_found"),
    [
        ("PERMISSION_DENIED", False),
        ("INSUFFICIENT_PRIVILEGES", False),
        ("INSUFFICIENT_PERMISSIONS", False),
        ("USER_DOES_NOT_HAVE_PERMISSION", False),
        ("TABLE_OR_VIEW_NOT_FOUND", True),
        ("SCHEMA_NOT_FOUND", True),
    ],
)
def test_all_markers_map_to_sql_denied(marker, not_found):
    with pytest.raises(SqlDenied) as ei:
        UserSql(FakeSqlClient([SkillhubSqlError(f"statement failed: [{marker}] detail")])).query("SELECT 1")
    assert ei.value.not_found is not_found


def test_sdk_not_found_for_an_object_is_denied_with_not_found():
    with pytest.raises(SqlDenied) as ei:
        UserSql(FakeSqlClient([NotFound("[TABLE_OR_VIEW_NOT_FOUND] x")])).query("SELECT 1")
    assert ei.value.not_found


def test_sdk_not_found_for_the_warehouse_is_a_fault():
    with pytest.raises(SqlError) as ei:
        UserSql(FakeSqlClient([NotFound("SQL warehouse abc does not exist")])).query("SELECT 1")
    assert not isinstance(ei.value, SqlDenied)


def test_sdk_permission_denied_is_not_not_found():
    with pytest.raises(SqlDenied) as ei:
        UserSql(FakeSqlClient([PermissionDenied("PERMISSION_DENIED: no SELECT on table")])).query("SELECT 1")
    assert not ei.value.not_found


def test_other_databricks_error_is_sql_error():
    with pytest.raises(SqlError) as ei:
        UserSql(FakeSqlClient([DatabricksError("boom")])).query("SELECT 1")
    assert not isinstance(ei.value, SqlDenied)


@pytest.mark.parametrize(
    "code", ["UNRESOLVED_COLUMN.WITH_SUGGESTION", "UNRESOLVED_COLUMN.WITHOUT_SUGGESTION"]
)
def test_a_column_the_view_does_not_have_is_sql_missing_column(code):
    message = (
        f"statement failed: [{code}] A column with name `profile_kind` cannot be resolved. SQLSTATE: 42703"
    )
    with pytest.raises(SqlMissingColumn) as ei:
        UserSql(FakeSqlClient([SkillhubSqlError(message)])).query("SELECT 1")
    assert isinstance(ei.value, SqlError) and str(ei.value) == "statement failed"


@pytest.mark.parametrize(
    "exc",
    [
        SkillhubSqlError("statement failed: [INTERNAL_ERROR] warehouse restarting"),
        SkillhubSqlError("statement 01ef did not finish within 60s"),
        DatabricksError("503 Service Unavailable"),
    ],
)
def test_any_other_failure_is_not_a_missing_column(exc):
    with pytest.raises(SqlError) as ei:
        UserSql(FakeSqlClient([exc])).query("SELECT 1")
    assert not isinstance(ei.value, SqlMissingColumn)


def test_denial_is_logged_without_sql_or_params(caplog):
    caplog.set_level(logging.WARNING, logger="skillhub.app.data")
    with pytest.raises(SqlDenied):
        UserSql(
            FakeSqlClient([SkillhubSqlError("statement failed: [INSUFFICIENT_PRIVILEGES] no\nmore")])
        ).query("SELECT secret_sql", {"p": "secret_value"})
    text = caplog.text
    assert "INSUFFICIENT_PRIVILEGES" in text and "SqlError" in text
    assert "secret_sql" not in text and "secret_value" not in text and "more" not in text


@pytest.mark.parametrize("dept", ["x.y; DROP", "a b", "`", "", "nope"])
def test_hostile_or_unknown_dept_is_refused_by_every_builder(dept):
    cfg = make_cfg()
    for build in (
        lambda: queries.journal_sql(cfg, dept),
        lambda: queries.matrix_sql(cfg, dept, None),
        lambda: queries.cases_sql(cfg, dept, "r"),
        lambda: queries.catalog_verdicts_sql(cfg, dept),
        lambda: queries.dataset_sql(cfg, dept, "s"),
        lambda: queries.rubric_sql(cfg, dept, "s"),
    ):
        with pytest.raises(queries.UnknownDepartment):
            build()


@pytest.mark.parametrize(
    "msg",
    [
        "statement failed: [PARSE_SYNTAX_ERROR] syntax error",
        "statement 01ef did not finish within 60s",
        "result was truncated by the server; narrow the query",
    ],
)
def test_other_sql_failure_maps_to_sql_error(msg):
    with pytest.raises(SqlError) as ei:
        UserSql(FakeSqlClient([SkillhubSqlError(msg)])).query("SELECT 1")
    assert not isinstance(ei.value, SqlDenied)


def test_matrix_sql_uses_naming_and_binds_skill():
    sql, params = queries.matrix_sql(make_cfg(), "equity_research", skill="earnings-preview")
    assert "skill_hub.dept_equity_research_evals.eval_matrix" in sql
    assert "earnings-preview" not in sql
    assert params == {"skill": "earnings-preview"}


def test_matrix_sql_selects_harness_after_model_profile():
    sql, _ = queries.matrix_sql(make_cfg(), "common", skill=None)
    assert "model_profile, harness, endpoint" in sql


def test_matrix_sql_without_skill_has_no_params():
    sql, params = queries.matrix_sql(make_cfg(), "common", skill=None)
    assert "skill_hub.common_evals.eval_matrix" in sql and params == {}


def test_cases_sql_binds_run_id():
    sql, params = queries.cases_sql(make_cfg(), "common", "0123abcd")
    assert ":run_id" in sql and "0123abcd" not in sql and params == {"run_id": "0123abcd"}


def test_journal_sql_binds_dept_and_orders_by_time():
    sql, params = queries.journal_sql(make_cfg(), "common")
    assert "skill_hub.common_evals.skill_versions" in sql
    assert "ORDER BY ts" in sql and params == {"dept": "common"}


def test_journal_and_verdict_sql_for_one_skill_bind_the_name():
    for build, want in (
        (queries.journal_sql, {"dept": "common", "skill": "audit-xls"}),
        (queries.catalog_verdicts_sql, {"skill": "audit-xls"}),
    ):
        sql, params = build(make_cfg(), "common", "audit-xls")
        assert "skill = :skill" in sql and "audit-xls" not in sql and params == want


def _row(skill, version, profile, mode, verdict):
    return {
        "skill": skill,
        "skill_version": version,
        "model_profile": profile,
        "mode": mode,
        "verdict": verdict,
    }


def test_catalog_verdict_is_worst_across_modes_per_profile():
    rows = [
        _row("a", 3, "claude-sonnet", "trigger", "pass"),
        _row("a", 3, "claude-sonnet", "with_skill", "warn"),
        _row("a", 3, "gemini-flash", "trigger", "pass"),
        _row("a", 3, "gemini-flash", "with_skill", "inconclusive"),
        _row("a", 3, "gemini-flash", "effectiveness", "fail"),
    ]
    assert skill_ref.live_verdicts(rows, {"a": 3}) == {"a": {"claude-sonnet": "warn", "gemini-flash": "fail"}}


def test_worst_verdict_order_and_unknown_is_worst():
    order = ["pass", "advisory", "warn", "inconclusive", "fail"]
    for i in range(1, len(order)):
        rows = [_row("a", 1, "m", "x", order[i]), _row("a", 1, "m", "y", order[i - 1])]
        assert skill_ref.live_verdicts(rows, {"a": 1}) == {"a": {"m": order[i]}}
    rows = [_row("a", 1, "m", "x", "pass"), _row("a", 1, "m", "y", "weird")]
    assert skill_ref.live_verdicts(rows, {"a": 1}) == {"a": {"m": "weird"}}


def test_worst_verdicts_use_live_version_only_and_skip_baseline_and_null():
    rows = [
        _row("a", 2, "m", "with_skill", "fail"),  # older version
        _row("a", 3, "m", "with_skill", "pass"),
        _row("a", 3, "m", "without_skill", "fail"),  # baseline
        _row("b", 1, "m", "with_skill", "fail"),  # no live version
    ]
    assert skill_ref.live_verdicts(rows, {"a": 3, "b": None}) == {"a": {"m": "pass"}}


def test_catalog_verdict_counts_only_the_gating_modes():
    # gating_modes [trigger]: effectiveness is advisory, so its fail does not reach the card. An unknown mode
    # still counts (fail closed), as in the release gate.
    rows = [
        _row("a", 3, "m", "trigger", "pass"),
        _row("a", 3, "m", "with_skill", "fail"),
        _row("b", 3, "m", "trigger", "pass"),
        _row("b", 3, "m", "mystery", "fail"),
    ]
    got = skill_ref.live_verdicts(rows, {"a": 3, "b": 3}, ["trigger"])
    assert got == {"a": {"m": "pass"}, "b": {"m": "fail"}}
    assert skill_ref.live_verdicts(rows, {"a": 3}, ["trigger", "effectiveness"]) == {"a": {"m": "fail"}}


def test_candidate_verdicts_count_only_the_gating_modes():
    rows = [
        {"model_profile": "m", "mode": "trigger", "verdict": "pass"},
        {"model_profile": "m", "mode": "with_skill", "verdict": "fail"},
        {"model_profile": "m", "mode": "without_skill", "verdict": "fail"},
    ]
    assert candidates.verdicts(rows, ["trigger"]) == {"m": "pass"}
    assert candidates.verdicts(rows) == {"m": "fail"}


def test_null_verdict_counts_as_inconclusive():
    rows = [_row("a", 3, "m", "with_skill", "pass"), _row("a", 3, "m", "trigger", None)]
    assert skill_ref.live_verdicts(rows, {"a": 3}) == {"a": {"m": "inconclusive"}}


def test_catalog_verdicts_sql_has_no_params():
    sql, params = queries.catalog_verdicts_sql(make_cfg(), "common")
    assert "skill_hub.common_evals.eval_matrix" in sql and params == {}


def test_catalog_verdicts_sql_reads_the_content_hash_and_start_time():
    sql, _ = queries.catalog_verdicts_sql(make_cfg(), "common")
    assert "content_hash" in sql and "started_at" in sql


def _live(version, content, started, verdict="pass", mode="trigger", profile="m", skill="a"):
    return {
        "skill": skill,
        "skill_version": version,
        "content_hash": content,
        "model_profile": profile,
        "mode": mode,
        "verdict": verdict,
        "started_at": started,
    }


H1 = "sha256:h1"
JOURNAL_H1 = [{"action": "switched", "to_version": 1, "content_hash": H1}]


def test_a_pre_release_row_counts_for_the_live_version_by_its_content_hash():
    # The merge re-evaluation runs before publishing: version 0 with the hash the release then publishes as v1.
    rows = [_live(0, H1, datetime(2026, 10, 30), "pass")]
    got = skill_ref.live_verdicts(rows, {"a": 1}, live_hashes={"a": skill_ref.live_content_hash(JOURNAL_H1)})
    assert got == {"a": {"m": "pass"}}


def test_a_pre_release_row_of_another_content_hash_does_not_count():
    rows = [_live(0, "sha256:other", datetime(2026, 10, 30), "fail")]
    assert skill_ref.live_verdicts(rows, {"a": 1}, live_hashes={"a": H1}) == {}


def test_a_newer_run_on_the_live_version_replaces_the_pre_release_row_of_the_same_model_and_mode():
    pre = _live(0, H1, datetime(2026, 10, 29), "fail")
    post = _live(1, H1, datetime(2026, 10, 30), "pass")
    for rows in ([pre, post], [post, pre]):  # the order of the rows does not matter
        assert skill_ref.live_verdicts(rows, {"a": 1}, live_hashes={"a": H1}) == {"a": {"m": "pass"}}


def test_a_pre_release_row_still_counts_for_a_mode_with_no_newer_row():
    rows = [
        _live(0, H1, datetime(2026, 10, 29), "pass", mode="trigger"),
        _live(1, H1, datetime(2026, 10, 30), "warn", mode="with_skill"),
    ]
    assert skill_ref.live_verdicts(rows, {"a": 1}, live_hashes={"a": H1}) == {"a": {"m": "warn"}}


def test_a_retired_skill_has_no_live_content_and_no_verdicts():
    journal = [*JOURNAL_H1, {"action": "retired", "to_version": None}]
    assert skill_ref.live_version(journal) is None and skill_ref.live_content_hash(journal) is None
    rows = [_live(0, H1, datetime(2026, 10, 30), "pass")]
    got = skill_ref.live_verdicts(rows, {"a": None}, live_hashes={"a": None})
    assert got == {}


def test_live_content_hash_is_the_latest_switch_and_follows_live_version():
    journal = [
        {"action": "published", "version": 2, "content_hash": "sha256:h2"},  # a publish never makes it live
        {"action": "switched", "to_version": 1, "content_hash": H1},
        {"action": "switched", "to_version": 2, "content_hash": "sha256:h2"},
        {"action": "switched", "to_version": 3, "content_hash": None},  # no hash recorded
    ]
    assert skill_ref.live_content_hash(journal) is None
    assert skill_ref.live_content_hash(journal[:3]) == "sha256:h2"
    assert skill_ref.live_version(journal[:3]) == 2


def test_get_sql_builds_user_sql_on_the_user_client(monkeypatch):
    seen = {}

    class Spy:
        def __init__(self, w, wh, *, timeout_s):
            seen.update(w=w, wh=wh, timeout_s=timeout_s)

    monkeypatch.setattr(deps, "SqlClient", Spy)
    user_w = object()
    out = deps.get_sql(user_w, make_cfg())
    assert isinstance(out, UserSql)
    assert seen == {"w": user_w, "wh": "wh-test", "timeout_s": 60}


def test_dataset_sql_reads_the_ds_table_with_no_values():
    sql, params = queries.dataset_sql(make_cfg(), "common", "audit-xls")
    assert "skill_hub.common_evals.ds_audit_xls" in sql and params == {}


def test_rubric_sql_reads_the_newest_row_for_the_skill():
    sql, params = queries.rubric_sql(make_cfg(), "common", "audit-xls")
    assert "skill_hub.common_evals.rubrics" in sql and "ORDER BY ts DESC LIMIT 1" in sql
    assert "audit-xls" not in sql and params == {"skill": "audit-xls"}
