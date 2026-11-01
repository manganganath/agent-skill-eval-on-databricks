import re

import pytest

from skillhub.naming import eval_schema, evals_schema
from skillhub.results import tables
from tests.fakes import FakeSql, make_cfg


def stmts(sql: FakeSql) -> list[str]:
    return [s for s, _ in sql.calls]


def test_ensure_tables_creates_four_tables_per_department():
    cfg, sql = make_cfg(), FakeSql()
    tables.ensure_tables(cfg, sql)
    text = "\n".join(stmts(sql))
    assert len(cfg.departments) > 2
    for dept in cfg.departments:
        schema = evals_schema(cfg, dept)
        for t in ["skill_versions", "eval_runs", "eval_cases", "rubrics"]:
            assert f"CREATE TABLE IF NOT EXISTS {schema}.{t} (" in text


def test_tables_and_views_never_create_schemas():
    """Bootstrap creates every results schema before these steps. The skillhub-bootstrap Job runs them as ci,
    which has no CREATE SCHEMA on the catalog, so they must not issue CREATE SCHEMA, even IF NOT EXISTS."""
    cfg, sql = make_cfg(), FakeSql()
    tables.ensure_tables(cfg, sql)
    tables.ensure_views(cfg, sql)
    assert not [s for s in stmts(sql) if "CREATE SCHEMA" in s.upper()]
    assert any(s.startswith(f"CREATE OR REPLACE VIEW {eval_schema(cfg)}.") for s in stmts(sql))


def test_every_results_table_is_append_only():
    cfg, sql = make_cfg(), FakeSql()
    tables.ensure_tables(cfg, sql)
    creates = [s for s in stmts(sql) if "CREATE TABLE" in s]
    assert len(creates) == 4 * len(cfg.departments)
    assert all("'delta.appendOnly' = 'true'" in s for s in creates)


def test_no_mutating_statements():
    cfg, sql = make_cfg(), FakeSql()
    tables.ensure_tables(cfg, sql)
    tables.ensure_views(cfg, sql)
    text = "\n".join(stmts(sql))
    assert not re.search(r"\b(UPDATE|MERGE|DELETE)\b", text)


def test_matrix_all_unions_every_department():
    cfg, sql = make_cfg(), FakeSql()
    tables.ensure_views(cfg, sql)
    mat = next(s for s in stmts(sql) if f"VIEW {eval_schema(cfg)}.matrix_all" in s)
    assert mat.count("UNION ALL") == len(cfg.departments) - 1
    for dept in cfg.departments:
        assert f"FROM {evals_schema(cfg, dept)}.eval_matrix" in mat
    assert "`experiment_id`" in mat


def test_eval_matrix_picks_latest_run_per_key():
    view = tables.VIEWS_SQL
    assert "PARTITION BY skill, skill_version, model_profile, COALESCE(harness, 'reference'), mode" in view
    assert "ORDER BY started_at DESC" in view
    assert "WHERE r.parent_run_id IS NOT NULL" in view


def test_eval_matrix_select_list_equals_matrix_columns_in_order():
    select = re.search(r"\)\s*SELECT (.*?)\s+FROM ranked", tables.VIEWS_SQL, re.S).group(1)
    select = re.sub(r"\([^)]*\)", "()", select)  # drop COALESCE(...) arguments before splitting
    names = [item.strip().split()[-1].strip("`") for item in select.split(",")]
    assert names == tables.MATRIX_COLUMNS
    assert "experiment_id" in names


def test_table_columns_match_contract_ddl():
    cols = tables.table_columns()
    assert cols["eval_cases"][:4] == ["run_id", "trace_id", "assessment_name", "case_id"]
    assert "precision" in cols["eval_runs"]
    assert len(cols["eval_runs"]) == 43
    assert cols["eval_runs"][-6:] == [
        "run_author",
        "profile_kind",
        "route",
        "model_id",
        "region",
        "catalog_version",
    ]
    assert cols["eval_runs"][cols["eval_runs"].index("model_profile") + 1] == "harness"
    assert len(cols["eval_cases"]) == 16
    assert len(cols["rubrics"]) == 6
    assert set(cols) == {"skill_versions", "eval_runs", "eval_cases", "rubrics"}


def test_ensure_views_is_idempotent_text():
    cfg, sql = make_cfg(), FakeSql()
    tables.ensure_views(cfg, sql)
    first = stmts(sql)
    sql2 = FakeSql()
    tables.ensure_views(cfg, sql2)
    assert first == stmts(sql2)


def test_runs_and_cases_views_are_reviewer_only():
    cfg, sql = make_cfg(), FakeSql()
    tables.ensure_views(cfg, sql)
    text = "\n".join(stmts(sql))
    for view in ["runs_all", "cases_all", "versions_all"]:
        assert f"GRANT SELECT ON VIEW {eval_schema(cfg)}.{view} TO `{cfg.roles.reviewers}`" in text
    grants = [s for s in stmts(sql) if s.startswith("GRANT")]
    assert len(grants) == 3 and all(f"`{cfg.roles.reviewers}`" in g for g in grants)
    assert "GRANT SELECT ON VIEW skill_hub.eval.matrix_all" not in text


def test_cases_all_adds_dept_literal_per_branch_and_runs_all_does_not():
    cfg, sql = make_cfg(), FakeSql()
    tables.ensure_views(cfg, sql)
    cases = next(s for s in stmts(sql) if f"VIEW {eval_schema(cfg)}.cases_all" in s)
    runs = next(s for s in stmts(sql) if f"VIEW {eval_schema(cfg)}.runs_all" in s)
    for dept in cfg.departments:
        assert f"SELECT '{dept}' AS dept, `run_id`" in cases
    assert "AS dept" not in runs
    assert "`dept`" in runs
    assert "AS dept" not in next(s for s in stmts(sql) if "VIEW skill_hub.eval.matrix_all" in s)


def test_unsafe_department_name_is_rejected_for_cases_all():
    sql = FakeSql()
    with pytest.raises(ValueError):
        tables._union_view(sql, "s", "v", [("x'; DROP", "t")], ["a"], add_dept=True)


class DescribeSql(FakeSql):
    """FakeSql that answers DESCRIBE TABLE ... eval_runs with a scripted column list."""

    def __init__(self, columns: list[str]) -> None:
        super().__init__()
        self.columns = columns

    def execute(self, statement, params=None):
        if statement.lstrip().upper().startswith("DESCRIBE"):
            self.calls.append((statement, dict(params or {})))
            return [{"col_name": c, "data_type": "string", "comment": None} for c in self.columns]
        return super().execute(statement, params)


def alters(sql: FakeSql) -> list[str]:
    """The column additions: the history retention's property changes are tested on their own."""
    return [s for s in stmts(sql) if s.lstrip().upper().startswith("ALTER") and "ADD COLUMNS" in s]


ADDED = ["harness", "run_author", "profile_kind", "route", "model_id", "region", "catalog_version"]


def _add(cfg, dept, name):
    kind = "BIGINT" if name == "catalog_version" else "STRING"
    return f"ALTER TABLE {evals_schema(cfg, dept)}.eval_runs ADD COLUMNS ({name} {kind})"


def test_ensure_tables_adds_every_later_column_once_when_missing():
    cfg = make_cfg()
    sql = DescribeSql(["run_id", "model_profile", "endpoint"])
    tables.ensure_tables(cfg, sql)
    assert sorted(alters(sql)) == sorted(_add(cfg, d, n) for d in cfg.departments for n in ADDED)


def test_ensure_tables_adds_only_the_missing_model_catalog_columns():
    cfg = make_cfg()
    sql = DescribeSql(["run_id", "model_profile", "harness", "endpoint", "run_author"])
    tables.ensure_tables(cfg, sql)
    assert alters(sql) == [_add(cfg, d, n) for d in cfg.departments for n in ADDED[2:]]


def test_ensure_tables_does_not_alter_when_every_column_exists():
    cfg = make_cfg()
    sql = DescribeSql(["run_id", "model_profile", "endpoint", *ADDED])
    tables.ensure_tables(cfg, sql)
    assert alters(sql) == []


def test_matrix_rows_and_runs_all_read_old_rows_as_hub_yaml_profiles():
    cfg, sql = make_cfg(), FakeSql()
    tables.ensure_views(cfg, sql)
    runs = next(s for s in stmts(sql) if f"VIEW {eval_schema(cfg)}.runs_all" in s)
    assert runs.count("COALESCE(`profile_kind`, 'hub_yaml') AS `profile_kind`") == len(cfg.departments)
    assert "COALESCE(profile_kind, 'hub_yaml') AS profile_kind" in tables.VIEWS_SQL
    assert tables.MATRIX_COLUMNS[-1] == "profile_kind"


def test_eval_matrix_view_coalesces_harness_to_reference():
    assert "COALESCE(harness, 'reference') AS harness" in tables.VIEWS_SQL


def test_runs_all_coalesces_harness_and_matrix_all_selects_it():
    cfg, sql = make_cfg(), FakeSql()
    tables.ensure_views(cfg, sql)
    runs = next(s for s in stmts(sql) if f"VIEW {eval_schema(cfg)}.runs_all" in s)
    mat = next(s for s in stmts(sql) if f"VIEW {eval_schema(cfg)}.matrix_all" in s)
    assert runs.count("COALESCE(`harness`, 'reference') AS `harness`") == len(cfg.departments)
    assert "`harness`" in mat
    assert tables.MATRIX_COLUMNS[tables.MATRIX_COLUMNS.index("model_profile") + 1] == "harness"


def test_every_latest_row_window_partitions_by_harness():
    """Fails if any ROW_NUMBER window over model_profile does not also partition by harness."""
    from skillhub.ci import pr_comment
    from skillhub.results import alerts

    cfg = make_cfg()
    texts = [
        tables.VIEWS_SQL,
        pr_comment._matrix_sql(cfg),
        alerts.queries(cfg)["verdict-regression"][0],
    ]
    for text in texts:
        windows = re.findall(r"PARTITION BY ([^\n]*?)\s+ORDER BY", text.replace("\n", " "))
        windows = [w for w in windows if "model_profile" in w]
        assert windows
        for w in windows:
            assert re.search(r"model_profile, (COALESCE\(harness, 'reference'\)|harness), mode", w), w


def test_versions_all_unions_every_department_journal():
    """The switch-failed alert reads one view over every department's skill_versions."""
    cfg, sql = make_cfg(), FakeSql()
    tables.ensure_views(cfg, sql)
    view = next(s for s in stmts(sql) if f"VIEW {eval_schema(cfg)}.versions_all" in s)
    assert view.count("UNION ALL") == len(cfg.departments) - 1
    for dept in cfg.departments:
        assert f"FROM {evals_schema(cfg, dept)}.skill_versions" in view
    assert "`action`" in view and "`ts`" in view and "`dept`" in view


def _with_history(days):
    cfg = make_cfg()
    cfg.retention.history_days = days
    return cfg


def test_without_a_history_setting_no_table_property_is_changed():
    cfg, sql = _with_history(None), FakeSql()
    tables.ensure_tables(cfg, sql)
    assert not [s for s in stmts(sql) if "logRetentionDuration" in s]


def test_the_history_setting_is_set_on_every_results_table_of_every_department():
    cfg, sql = _with_history(2555), FakeSql()
    tables.ensure_tables(cfg, sql)
    alters = [s for s in stmts(sql) if s.startswith("ALTER TABLE") and "logRetentionDuration" in s]
    assert len(alters) == 4 * len(cfg.departments)
    assert f"ALTER TABLE {evals_schema(cfg, 'common')}.skill_versions SET TBLPROPERTIES " in alters[0]
    assert all(s.endswith("('delta.logRetentionDuration' = 'interval 2555 days')") for s in alters)


class _HistorySet(FakeSql):
    def execute(self, statement, params=None):
        if statement.startswith("SHOW TBLPROPERTIES"):
            self.calls.append((statement, dict(params or {})))
            return [{"key": "delta.logRetentionDuration", "value": "interval 2555 days"}]
        return super().execute(statement, params)


def test_a_table_that_already_keeps_its_history_is_left_alone():
    sql = _HistorySet()
    tables.ensure_tables(_with_history(2555), sql)
    assert not [s for s in stmts(sql) if s.startswith("ALTER TABLE") and "logRetentionDuration" in s]


def test_a_refused_change_names_the_table_and_the_statement_its_owner_can_run():
    from skillhub.sql import SqlError

    class Refusing(FakeSql):
        def execute(self, statement, params=None):
            if "logRetentionDuration' =" in statement:
                raise SqlError("PERMISSION_DENIED: not the owner")
            return super().execute(statement, params)

    with pytest.raises(
        SqlError, match=r"cannot keep the write history of .*\.skill_versions .*Its owner can run: ALTER"
    ):
        tables.ensure_tables(_with_history(400), Refusing())
