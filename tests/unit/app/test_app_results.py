from app_fakes import FakeApi, FakeWorkspace
from backend.data import SqlDenied, SqlError
from conftest import make_cfg

ME = "/api/2.0/preview/scim/v2/Me"
RUN = "0123456789abcdef0123456789abcdef"
EQ = "dept_equity_research_evals"


class FakeUserSql:
    """Stands in for UserSql. Records every (statement, params). `rows` maps a schema fragment to rows.
    `denied` lists schema fragments that raise SqlDenied."""

    def __init__(self, rows=None, denied=(), missing=(), broken=()):
        self.rows = rows or {}
        self.denied = set(denied)
        self.missing = set(missing)  # schema fragments whose table does not exist (not_found)
        self.broken = set(broken)
        self.calls = []

    def query(self, statement, params=None):
        self.calls.append((statement, params or {}))
        for schema in self.broken:
            if schema in statement:
                raise SqlError("statement failed")
        for schema in self.missing:
            if schema in statement:
                raise SqlDenied("no table", not_found=True)
        for schema in self.denied:
            if schema in statement:
                raise SqlDenied("no SELECT")
        for schema, rs in self.rows.items():
            if schema in statement:
                return list(rs)
        return []


def reviewer_w():
    return FakeWorkspace(
        api=FakeApi({ME: {"userName": "rita@example.com", "groups": [{"display": "skillhub-reviewers"}]}})
    )


def test_matrix_binds_skill_and_is_not_interpolated(make_client):
    sql = FakeUserSql()
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=sql)
    c.get("/api/results/matrix", params={"skill": "x' OR '1'='1", "dept": "common"})
    statement, params = sql.calls[-1]
    assert "x' OR" not in statement
    assert params == {"skill": "x' OR '1'='1"}


def test_matrix_unknown_dept_is_404(make_client):
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=FakeUserSql())
    r = c.get("/api/results/matrix", params={"dept": "nope"})
    assert r.status_code == 404 and r.json() == {"detail": {"reason": "department_not_found"}}


def test_matrix_all_depts_skips_denied_department(make_client):
    row = {"skill": "a", "skill_version": 1, "experiment_id": "42", "verdict": "pass"}
    sql = FakeUserSql(rows={"common_evals": [row]}, denied={EQ})
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=sql)
    r = c.get("/api/results/matrix")
    assert r.json() == [{**row, "dept": "common"}]
    assert r.headers["x-eval-denied-departments"] == "equity_research"


def test_matrix_explicit_denied_dept_is_403(make_client):
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=FakeUserSql(denied={EQ}))
    r = c.get("/api/results/matrix", params={"dept": "equity_research"})
    assert r.status_code == 403 and r.json() == {"detail": {"reason": "evals_not_readable"}}


def test_matrix_every_department_denied_is_403_not_empty(make_client):
    sql = FakeUserSql(denied={"common_evals", EQ, "external_evals"})
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=sql)
    r = c.get("/api/results/matrix")
    assert r.status_code == 403 and r.json() == {"detail": {"reason": "evals_not_readable"}}


def test_cases_rejects_bad_run_id(make_client):
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=FakeUserSql())
    r = c.get("/api/results/cases", params={"run_id": "x'; --"})
    assert r.status_code == 422 and r.json() == {"detail": {"reason": "invalid_run_id"}}


def test_cases_reviewer_reads_the_cases_all_view(make_client):
    rows = [{"run_id": RUN, "case_id": 1, "dept": "common"}]
    sql = FakeUserSql(rows={"eval.cases_all": rows})
    c = make_client(user_w=reviewer_w(), cfg=make_cfg(), sql=sql)
    assert c.get("/api/results/cases", params={"run_id": RUN}).json() == rows
    assert len(sql.calls) == 1 and "eval.cases_all" in sql.calls[0][0]
    assert ", dept FROM" in sql.calls[0][0]  # the view carries dept
    assert sql.calls[0][1] == {"run_id": RUN}


def test_cases_reviewer_falls_back_when_view_refuses(make_client):
    sql = FakeUserSql(rows={"common_evals": [{"run_id": RUN, "case_id": 1}]}, denied={"eval.cases_all"})
    c = make_client(user_w=reviewer_w(), cfg=make_cfg(), sql=sql)
    assert c.get("/api/results/cases", params={"run_id": RUN}).json() == [
        {"run_id": RUN, "case_id": 1, "dept": "common"}
    ]


def test_cases_non_reviewer_never_touches_cases_all(make_client):
    sql = FakeUserSql(rows={"common_evals": [{"run_id": RUN, "case_id": 1}]})
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=sql)  # no SCIM answer, so not a reviewer
    c.get("/api/results/cases", params={"run_id": RUN})
    assert all("cases_all" not in statement for statement, _ in sql.calls)
    assert any("common_evals" in statement for statement, _ in sql.calls)


def test_cases_returns_rows_for_run(make_client):
    sql = FakeUserSql(rows={"common_evals": [{"run_id": RUN, "case_id": 1, "trigger_outcome": "correct"}]})
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=sql)
    rows = c.get("/api/results/cases", params={"run_id": RUN}).json()
    assert rows and rows[0]["trigger_outcome"] == "correct" and rows[0]["dept"] == "common"


def test_cases_every_department_denied_is_403_not_empty(make_client):
    sql = FakeUserSql(denied={"common_evals", EQ, "external_evals"})
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=sql)
    r = c.get("/api/results/cases", params={"run_id": RUN})
    assert r.status_code == 403 and r.json() == {"detail": {"reason": "evals_not_readable"}}


def test_partial_refusal_returns_readable_rows_and_names_refused_departments(make_client):
    row = {"run_id": RUN, "case_id": 1}
    sql = FakeUserSql(rows={"common_evals": [row]}, denied={EQ, "external_evals"})
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=sql)
    r = c.get("/api/results/cases", params={"run_id": RUN})
    assert r.status_code == 200 and r.json() == [{**row, "dept": "common"}]
    assert r.headers["x-eval-denied-departments"] == "equity_research,external"


def test_no_denied_header_when_nothing_is_refused(make_client):
    c = make_client(
        user_w=FakeWorkspace(), cfg=make_cfg(), sql=FakeUserSql(rows={"common_evals": [{"a": 1}]})
    )
    r = c.get("/api/results/matrix")
    assert "x-eval-denied-departments" not in r.headers


def test_empty_result_with_one_refusal_and_two_not_found_is_403(make_client):
    sql = FakeUserSql(denied={EQ}, missing={"common_evals", "external_evals"})
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=sql)
    for path, params in (("/api/results/matrix", {}), ("/api/results/cases", {"run_id": RUN})):
        r = c.get(path, params=params)
        assert r.status_code == 403 and r.json() == {"detail": {"reason": "evals_not_readable"}}


def test_not_found_department_is_skipped_and_is_not_a_refusal(make_client):
    row = {"skill": "a"}
    sql = FakeUserSql(rows={"common_evals": [row]}, missing={EQ, "external_evals"})
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=sql)
    r = c.get("/api/results/matrix")
    assert r.status_code == 200 and r.json() == [{**row, "dept": "common"}]
    assert "x-eval-denied-departments" not in r.headers


def test_all_departments_not_found_is_an_empty_list(make_client):
    sql = FakeUserSql(missing={"common_evals", EQ, "external_evals"})
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=sql)
    r = c.get("/api/results/matrix")
    assert r.status_code == 200 and r.json() == []


def test_sql_failure_is_502_without_server_text(make_client):
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=FakeUserSql(broken={"common_evals"}))
    r = c.get("/api/results/matrix")
    assert r.status_code == 502 and r.json() == {"detail": {"reason": "sql_failed"}}


def test_failed_reviewer_check_falls_back_and_never_touches_cases_all(make_client):
    api = FakeApi(raises={ME: RuntimeError("scim down")})
    sql = FakeUserSql(rows={"common_evals": [{"run_id": RUN, "case_id": 1}]})
    c = make_client(user_w=FakeWorkspace(api=api), cfg=make_cfg(), sql=sql)
    r = c.get("/api/results/cases", params={"run_id": RUN})
    assert r.status_code == 200 and r.json() == [{"run_id": RUN, "case_id": 1, "dept": "common"}]
    assert all("cases_all" not in statement for statement, _ in sql.calls)


MATRIX_KEYS = {
    "skill",
    "skill_version",
    "content_hash",
    "model_profile",
    "harness",
    "endpoint",
    "model_class",
    "mode",
    "verdict",
    "run_id",
    "experiment_id",
    "n_cases",
    "n_reviewed",
    "pass_rate",
    "spread",
    "error_rate",
    "accuracy",
    "precision",
    "recall",
    "pass_rate_without",
    "uplift_task_quality",
    "uplift_skill_format",
    "started_at",
    "profile_kind",
}
CASE_KEYS = {
    "run_id",
    "trace_id",
    "assessment_name",
    "case_id",
    "repeat",
    "mode",
    "user_input",
    "expected_skill",
    "invoked_skill",
    "trigger_outcome",
    "value",
    "rationale",
    "judge_model",
    "source_type",
    "reviewed",
}


def _selected(statement):
    return {c.strip() for c in statement.split(" FROM ")[0].removeprefix("SELECT ").split(",")}


def test_queries_select_explicit_columns_that_match_the_frontend_types(make_client):
    from backend import queries

    cfg = make_cfg()
    matrix_stmt, _ = queries.matrix_sql(cfg, "common", None)
    cases_stmt, _ = queries.cases_sql(cfg, "common", RUN)
    all_stmt, _ = queries.cases_all_sql(cfg, RUN)
    assert _selected(matrix_stmt) == MATRIX_KEYS
    assert _selected(cases_stmt) == CASE_KEYS
    assert _selected(all_stmt) == CASE_KEYS | {"dept"}
    for stmt in (matrix_stmt, cases_stmt, all_stmt):
        assert "*" not in stmt and "requested_by" not in stmt
    assert "ORDER BY started_at DESC, run_id" in matrix_stmt
    assert "ORDER BY case_id, repeat, assessment_name" in cases_stmt
    assert "ORDER BY case_id, repeat, assessment_name" in all_stmt


def test_reviewer_cases_all_rows_carry_dept_from_the_view(make_client):
    rows = [{"run_id": RUN, "case_id": 1, "dept": "equity_research"}]
    c = make_client(user_w=reviewer_w(), cfg=make_cfg(), sql=FakeUserSql(rows={"eval.cases_all": rows}))
    assert c.get("/api/results/cases", params={"run_id": RUN}).json()[0]["dept"] == "equity_research"


def test_over_cap_result_is_trimmed_and_marked_truncated(make_client):
    from backend import queries

    n = queries.RESULT_LIMIT
    sql = FakeUserSql(rows={"common_evals": [{"i": i} for i in range(n + 1)]})
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=sql)
    r = c.get("/api/results/matrix", params={"dept": "common"})
    assert len(r.json()) == n and r.headers["x-result-truncated"] == "true"
    r = c.get("/api/results/cases", params={"run_id": RUN})
    assert len(r.json()) == n and r.headers["x-result-truncated"] == "true"


def test_reviewer_over_cap_is_marked_truncated(make_client):
    from backend import queries

    rows = [{"i": i} for i in range(queries.RESULT_LIMIT + 1)]
    c = make_client(user_w=reviewer_w(), cfg=make_cfg(), sql=FakeUserSql(rows={"eval.cases_all": rows}))
    r = c.get("/api/results/cases", params={"run_id": RUN})
    assert len(r.json()) == queries.RESULT_LIMIT and r.headers["x-result-truncated"] == "true"


def test_at_cap_is_not_truncated(make_client):
    from backend import queries

    sql = FakeUserSql(rows={"common_evals": [{"i": i} for i in range(queries.RESULT_LIMIT)]})
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=sql)
    assert "x-result-truncated" not in c.get("/api/results/matrix", params={"dept": "common"}).headers


def test_matrix_by_fqn_maps_the_schema_to_the_department_key(make_client):
    sql = FakeUserSql(rows={EQ: [{"skill": "earnings-preview", "skill_version": 1}]})
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=sql)
    r = c.get("/api/results/matrix", params={"fqn": "skill_hub.dept_equity_research.earnings-preview"})
    assert r.status_code == 200
    assert [row["dept"] for row in r.json()] == ["equity_research"]
    assert len(sql.calls) == 1
    statement, params = sql.calls[0]
    assert EQ in statement and params == {"skill": "earnings-preview"}


def test_matrix_by_fqn_with_an_unknown_schema_is_404(make_client):
    sql = FakeUserSql()
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=sql)
    r = c.get("/api/results/matrix", params={"fqn": "skill_hub.dept_nope.earnings-preview"})
    assert r.status_code == 404 and r.json() == {"detail": {"reason": "skill_not_found"}}
    assert sql.calls == []


class OldViewSql(FakeUserSql):
    """Department views from before the model catalog (`old` schema fragments, default all): selecting
    profile_kind from them fails."""

    def __init__(self, *args, old=("_evals",), **kw):
        super().__init__(*args, **kw)
        self.old = old

    def query(self, statement, params=None):
        if "profile_kind" in statement and any(o in statement for o in self.old):
            self.calls.append((statement, params or {}))
            raise SqlError("statement failed")
        return super().query(statement, params)


def test_matrix_reads_a_view_from_before_the_model_catalog_without_profile_kind(make_client):
    rows = [{"skill": "audit-xls", "model_profile": "gpt", "run_id": RUN}]
    sql = OldViewSql(rows={"common_evals": rows})
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=sql)
    r = c.get("/api/results/matrix", params={"dept": "common"})
    assert r.status_code == 200 and r.json() == [{**rows[0], "dept": "common"}]
    assert "profile_kind" not in sql.calls[-1][0]


def test_the_legacy_matrix_query_differs_only_by_profile_kind():
    from backend import queries

    new, _ = queries.matrix_sql(make_cfg(), "common", None)
    old, _ = queries.matrix_sql(make_cfg(), "common", None, legacy=True)
    assert _selected(new) - _selected(old) == {"profile_kind"}


def test_matrix_only_a_department_on_an_old_view_loses_profile_kind(make_client):
    new_row = {"skill": "a", "model_profile": "nova", "profile_kind": "external_model", "run_id": RUN}
    old_row = {"skill": "b", "model_profile": "gpt", "run_id": RUN}
    sql = OldViewSql(rows={"common_evals": [old_row], EQ: [new_row]}, old=("common_evals",))
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=sql)
    r = c.get("/api/results/matrix")
    assert r.status_code == 200
    by_dept = {x["dept"]: x for x in r.json()}
    assert by_dept["equity_research"]["profile_kind"] == "external_model"
    assert "profile_kind" not in by_dept["common"]


def test_matrix_a_department_that_fails_on_both_reads_is_502(make_client):
    sql = OldViewSql(broken={EQ})
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=sql)
    assert c.get("/api/results/matrix", params={"dept": "equity_research"}).status_code == 502


def test_matrix_keeps_the_denied_header_when_a_department_used_the_fallback(make_client):
    row = {"skill": "b", "model_profile": "gpt", "run_id": RUN}
    sql = OldViewSql(rows={"common_evals": [row]}, denied={EQ}, old=("common_evals",))
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=sql)
    r = c.get("/api/results/matrix")
    assert r.status_code == 200 and r.json() == [{**row, "dept": "common"}]
    assert r.headers["x-eval-denied-departments"] == "equity_research"
