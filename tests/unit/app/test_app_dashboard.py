from app_fakes import FakeWorkspace
from backend import dashboard, queries
from backend.data import SqlDenied, SqlError
from conftest import make_cfg

EQ = "skill_hub.dept_equity_research_evals"
COMMON = "skill_hub.common_evals"
EXTERNAL = "skill_hub.external_evals"
SERVING = "system.serving"


def section(statement: str, params: dict) -> str:
    """Which section a dashboard statement answers, from its text (queries.dashboard_sql)."""
    if "endpoint_usage" in statement:
        return "tokens"
    if "skill_versions" in statement:
        return "journal"
    if "eval_cases" in statement:
        return "routing"
    if "uplift_task_quality, error_rate" in statement:
        return "cells"
    if "pass_rate_without" in statement:
        return "uplift"
    if params.get("mode") == "trigger":
        return "trigger_trend"
    if params.get("mode") == "with_skill":
        return "pass_rate_trend"
    raise AssertionError(f"unknown statement: {statement}")


def keeps(statement: str, row: dict) -> bool:
    """What the hub.yaml filter of a statement does to a row (a row without profile_kind is a hub.yaml one)."""
    if queries.HUB_YAML_ONLY not in statement:
        return True
    return (row.get("profile_kind") or "hub_yaml") == "hub_yaml"


class DashSql:
    """Stands in for UserSql. `data` maps a schema fragment to {section: rows}. `denied` schema fragments raise
    SqlDenied, `missing` ones raise a not_found SqlDenied, and `broken` ones raise SqlError."""

    def __init__(self, data=None, denied=(), missing=(), broken=()):
        self.data = data or {}
        self.denied = set(denied)
        self.missing = set(missing)
        self.broken = set(broken)
        self.calls: list[tuple[str, dict]] = []

    def query(self, statement, params=None):
        params = dict(params or {})
        self.calls.append((statement, params))
        for frag in self.broken:
            if frag in statement:
                raise SqlError("statement failed")
        for frag in self.denied:
            if frag in statement:
                raise SqlDenied("no SELECT")
        for frag in self.missing:
            if frag in statement:
                raise SqlDenied("no table", not_found=True)
        name = section(statement, params)
        for frag, sections in self.data.items():
            if frag in statement:
                return [r for r in sections.get(name, []) if keeps(statement, r)]
        return []


def summary_row(**kw):
    base = {
        "skills": 0,
        "accuracy_sum": None,
        "accuracy_n": 0,
        "uplift_sum": None,
        "uplift_n": 0,
        "error_sum": None,
        "error_n": 0,
    }
    return {**base, **kw}


def cell(skill, version, mode, verdict, **kw):
    return {
        "skill": skill,
        "skill_version": version,
        "model_profile": "claude-sonnet",
        "mode": mode,
        "verdict": verdict,
        "accuracy": None,
        "uplift_task_quality": None,
        "error_rate": 0.0,
        **kw,
    }


def common_data():
    return {
        # sales-brief is live at v2, so its v1 row never counts; audit-xls has no live version and keeps its newest.
        "cells": [
            cell("sales-brief", 1, "trigger", "fail", accuracy=0.1, error_rate=0.5),
            cell("sales-brief", 2, "trigger", "pass", accuracy=0.75),
            cell("sales-brief", 2, "with_skill", "pass", uplift_task_quality=0.3),
            cell("audit-xls", 1, "trigger", "pass", accuracy=0.75),
            cell("audit-xls", 1, "with_skill", "inconclusive", uplift_task_quality=0.1, error_rate=0.2),
        ],
        "uplift": [
            {
                "skill": "sales-brief",
                "skill_version": 2,
                "model_profile": "claude-sonnet",
                "pass_rate_without": 0.5,
                "pass_rate": 0.8,
                "uplift_task_quality": 0.3,
                "uplift_skill_format": 0.1,
            },
        ],
        "journal": [{"skill": "sales-brief", "action": "switched", "to_version": 2}],
        "routing": [
            {
                "model_profile": "claude-sonnet",
                "expected_skill": "sales-brief",
                "invoked_skill": "sales-brief",
                "trigger_outcome": "correct",
                "n": 4,
            },
        ],
        "trigger_trend": [
            {
                "skill": "sales-brief",
                "skill_version": 2,
                "model_profile": "claude-sonnet",
                "day": "2026-10-01",
                "accuracy": 0.75,
                "runs": 2,
            },
        ],
        "pass_rate_trend": [
            {
                "skill": "sales-brief",
                "skill_version": 2,
                "model_profile": "claude-sonnet",
                "day": "2026-10-01",
                "pass_rate": 0.8,
                "runs": 1,
            },
        ],
    }


def tokens_rows():
    return [
        {
            "endpoint_name": "ep-claude",
            "day": "2026-10-01",
            "source": "eval",
            "input_tokens": 120,
            "output_tokens": 30,
        }
    ]


def test_dashboard_returns_every_section_tagged_with_its_department(make_client):
    sql = DashSql({COMMON: common_data(), SERVING: {"tokens": tokens_rows()}})
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=sql)
    r = c.get("/api/results/dashboard")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {
        "summary",
        "verdicts",
        "uplift",
        "routing",
        "trigger_trend",
        "pass_rate_trend",
        "health",
        "tokens",
    }
    assert {"dept": "common", "model_profile": "claude-sonnet", "verdict": "inconclusive", "n": 1} in body[
        "verdicts"
    ]
    assert next(v for v in body["verdicts"] if v["verdict"] == "pass") == {
        "dept": "common",
        "model_profile": "claude-sonnet",
        "verdict": "pass",
        "n": 3,
    }
    assert body["uplift"][0]["dept"] == "common" and body["uplift"][0]["uplift_task_quality"] == 0.3
    assert body["routing"][0]["n"] == 4 and body["routing"][0]["invoked_skill"] == "sales-brief"
    assert body["trigger_trend"][0]["accuracy"] == 0.75 and body["trigger_trend"][0]["day"] == "2026-10-01"
    assert body["pass_rate_trend"][0]["pass_rate"] == 0.8
    assert body["health"][0] == {
        "dept": "common",
        "model_profile": "claude-sonnet",
        "cells": 4,
        "error_rate": 0.05,
        "inconclusive_rate": 0.25,
    }
    assert body["tokens"] == [
        {
            "model_profile": "claude-sonnet",
            "endpoint_name": "ep-claude",
            "day": "2026-10-01",
            "source": "eval",
            "input_tokens": 120,
            "output_tokens": 30,
        }
    ]


def test_summary_counts_the_live_version_only(make_client):
    sql = DashSql({COMMON: common_data(), SERVING: {"tokens": []}})
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=sql)
    summary = c.get("/api/results/dashboard").json()["summary"]
    assert summary["skills"] == 2
    assert summary["verdicts"] == {"pass": 3, "warn": 0, "fail": 0, "inconclusive": 1, "advisory": 0}
    assert summary["trigger_accuracy"] == 0.75
    assert summary["uplift_task_quality"] == 0.2
    assert summary["error_rate"] == 0.05


def test_every_statement_reads_only_the_departments_the_user_can_read_by_schema(make_client):
    sql = DashSql({COMMON: common_data(), SERVING: {"tokens": []}})
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=sql)
    c.get("/api/results/dashboard")
    evals = [s for s, _ in sql.calls if "_evals." in s]
    assert evals
    for s in evals:  # each statement names exactly one department's schema
        assert sum(frag in s for frag in (COMMON, EQ, EXTERNAL)) == 1, s


def test_denied_department_is_left_out_and_named(make_client):
    sql = DashSql({COMMON: common_data(), EQ: common_data(), SERVING: {"tokens": []}}, denied={EQ})
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=sql)
    r = c.get("/api/results/dashboard")
    assert r.status_code == 200
    assert r.headers["x-eval-denied-departments"] == "equity_research"
    assert {row["dept"] for row in r.json()["verdicts"]} == {"common"}
    assert r.json()["summary"]["skills"] == 2  # the refused department adds nothing


def test_a_refusal_in_any_statement_leaves_the_whole_department_out(make_client):
    class OneStatementRefused(DashSql):
        def query(self, statement, params=None):
            if "pass_rate_without" in statement and EQ in statement:
                self.calls.append((statement, dict(params or {})))
                raise SqlDenied("no SELECT")
            return super().query(statement, params)

    sql = OneStatementRefused({COMMON: common_data(), EQ: common_data(), SERVING: {"tokens": []}})
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=sql)
    r = c.get("/api/results/dashboard")
    assert r.headers["x-eval-denied-departments"] == "equity_research"
    assert all(row["dept"] == "common" for row in r.json()["uplift"])


def test_every_department_refused_is_403(make_client):
    sql = DashSql(denied={COMMON, EQ, EXTERNAL})
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=sql)
    r = c.get("/api/results/dashboard")
    assert r.status_code == 403 and r.json() == {"detail": {"reason": "evals_not_readable"}}


def test_refusal_with_no_matrix_rows_read_is_403_not_an_empty_dashboard(make_client):
    sql = DashSql({COMMON: {"cells": []}}, denied={EQ, EXTERNAL})
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=sql)
    r = c.get("/api/results/dashboard")
    assert r.status_code == 403 and r.json() == {"detail": {"reason": "evals_not_readable"}}


def test_empty_results_give_zero_tiles_and_empty_sections(make_client):
    sql = DashSql({COMMON: {}, EQ: {}, EXTERNAL: {}, SERVING: {"tokens": []}})
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=sql)
    r = c.get("/api/results/dashboard")
    assert r.status_code == 200
    body = r.json()
    assert body["summary"] == {
        "skills": 0,
        "verdicts": {"pass": 0, "warn": 0, "fail": 0, "inconclusive": 0, "advisory": 0},
        "trigger_accuracy": None,
        "uplift_task_quality": None,
        "error_rate": None,
    }
    for key in ("verdicts", "uplift", "routing", "trigger_trend", "pass_rate_trend", "health"):
        assert body[key] == []
    assert body["tokens"] == []


def test_missing_eval_tables_are_empty_not_refused(make_client):
    sql = DashSql({SERVING: {"tokens": []}}, missing={COMMON, EQ, EXTERNAL})
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=sql)
    r = c.get("/api/results/dashboard")
    assert r.status_code == 200 and "x-eval-denied-departments" not in r.headers
    assert r.json()["verdicts"] == []


def test_tokens_are_null_when_the_serving_tables_are_denied(make_client):
    sql = DashSql({COMMON: common_data(), SERVING: {"tokens": tokens_rows()}}, denied={SERVING})
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=sql)
    r = c.get("/api/results/dashboard")
    assert r.status_code == 200 and r.json()["tokens"] is None
    assert r.json()["verdicts"]  # the rest of the page still answers


def test_tokens_are_null_when_the_serving_read_fails(make_client):
    sql = DashSql({COMMON: common_data()}, broken={SERVING})
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=sql)
    r = c.get("/api/results/dashboard")
    assert r.status_code == 200 and r.json()["tokens"] is None


def test_tokens_are_empty_without_serving_models_and_never_queried(make_client):
    cfg = make_cfg()
    cfg.models = {}
    sql = DashSql({COMMON: common_data()})
    c = make_client(user_w=FakeWorkspace(), cfg=cfg, sql=sql)
    assert c.get("/api/results/dashboard").json()["tokens"] == []
    assert all("endpoint_usage" not in s for s, _ in sql.calls)


def test_tokens_statement_binds_endpoints_and_the_window():
    stmt, params = queries.tokens_sql(["ep-claude", "ep-gemini"], "ci-eval-app-id")
    assert params == {"e0": "ep-claude", "e1": "ep-gemini", "ci_eval_principal": "ci-eval-app-id"}
    assert "IN (:e0, :e1)" in stmt and "ep-claude" not in stmt
    assert "INTERVAL 30 DAYS" in stmt and "system.serving.endpoint_usage" in stmt


def test_tokens_statement_splits_try_from_eval_by_usage_context_and_requester():
    stmt, _ = queries.tokens_sql(["ep-claude"], "ci-eval-app-id")
    assert "usage_context['skillhub_source'] = 'try' THEN 'try'" in stmt
    assert "usage_context['skillhub_source'] = 'eval' OR u.requester = :ci_eval_principal THEN 'eval'" in stmt
    assert "WHERE s.source IS NOT NULL" in stmt  # other rows are left out
    assert "GROUP BY l.endpoint_name, CAST(s.request_time AS DATE), s.source" in stmt


def test_tokens_statement_never_interpolates_the_principal():
    stmt, params = queries.tokens_sql(["ep-claude"], "ci'-eval; DROP")
    assert "ci'-eval" not in stmt and "DROP" not in stmt
    assert params["ci_eval_principal"] == "ci'-eval; DROP"


def test_tokens_statement_without_a_principal_counts_only_tagged_rows():
    stmt, params = queries.tokens_sql(["ep-claude"], None)
    assert "ci_eval_principal" not in stmt and "requester" not in stmt
    assert set(params) == {"e0"}
    assert "usage_context['skillhub_source'] = 'eval' THEN 'eval'" in stmt


def test_tokens_read_binds_the_configured_principal_from_the_app_env(make_client, monkeypatch):
    monkeypatch.setenv("SKILLHUB_CI_EVAL_PRINCIPAL", "  ci-eval-app-id  ")
    sql = DashSql({COMMON: common_data(), SERVING: {"tokens": tokens_rows()}})
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=sql)
    assert c.get("/api/results/dashboard").status_code == 200
    stmt, params = next((s, p) for s, p in sql.calls if "endpoint_usage" in s)
    assert params["ci_eval_principal"] == "ci-eval-app-id"
    assert "ci-eval-app-id" not in stmt


def test_tokens_read_without_the_principal_env_leaves_it_out(make_client, monkeypatch):
    monkeypatch.delenv("SKILLHUB_CI_EVAL_PRINCIPAL", raising=False)
    sql = DashSql({COMMON: common_data(), SERVING: {"tokens": tokens_rows()}})
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=sql)
    assert c.get("/api/results/dashboard").json()["tokens"][0]["source"] == "eval"
    stmt, params = next((s, p) for s, p in sql.calls if "endpoint_usage" in s)
    assert "ci_eval_principal" not in params and "requester" not in stmt


def test_tokens_rows_keep_their_source_for_both_sources(make_client, monkeypatch):
    monkeypatch.setenv("SKILLHUB_CI_EVAL_PRINCIPAL", "ci-eval-app-id")
    rows = [
        {
            "endpoint_name": "ep-claude",
            "day": "2026-10-01",
            "source": "eval",
            "input_tokens": 120,
            "output_tokens": 30,
        },
        {
            "endpoint_name": "ep-claude",
            "day": "2026-10-01",
            "source": "try",
            "input_tokens": 40,
            "output_tokens": 9,
        },
    ]
    sql = DashSql({COMMON: common_data(), SERVING: {"tokens": rows}})
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=sql)
    tokens = c.get("/api/results/dashboard").json()["tokens"]
    assert [(t["source"], t["input_tokens"]) for t in tokens] == [("eval", 120), ("try", 40)]


def test_tokens_statement_for_a_refused_read_is_still_null_with_a_principal(make_client, monkeypatch):
    monkeypatch.setenv("SKILLHUB_CI_EVAL_PRINCIPAL", "ci-eval-app-id")
    sql = DashSql({COMMON: common_data()}, denied={SERVING})
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=sql)
    r = c.get("/api/results/dashboard")
    assert r.status_code == 200 and r.json()["tokens"] is None and r.json()["verdicts"]


def test_dashboard_statements_bind_values_and_name_only_the_department_schema():
    cfg = make_cfg()
    statements = queries.dashboard_sql(cfg, "equity_research")
    assert set(statements) == {"cells", "uplift", "journal", "routing", "trigger_trend", "pass_rate_trend"}
    for name, (stmt, params) in statements.items():
        assert EQ in stmt and COMMON not in stmt and EXTERNAL not in stmt, name
        assert set(params) <= {"mode"}, name
    assert statements["trigger_trend"][1] == {"mode": "trigger"}
    assert statements["pass_rate_trend"][1] == {"mode": "with_skill"}
    assert "CAST(started_at AS DATE)" in statements["trigger_trend"][0]


def test_summary_combines_departments_as_exact_means():
    rows = [
        summary_row(skills=1, accuracy_sum=1.0, accuracy_n=1),
        summary_row(skills=3, accuracy_sum=0.5, accuracy_n=3),
    ]
    got = dashboard.summary(rows, [])
    assert got["skills"] == 4
    assert got["trigger_accuracy"] == 1.5 / 4


def test_live_rows_keeps_the_live_version_and_falls_back_to_the_newest_tested():
    rows = [
        {"skill": "a", "skill_version": 1, "model_profile": "m"},
        {"skill": "a", "skill_version": 2, "model_profile": "m"},
        {"skill": "b", "skill_version": 1, "model_profile": "m"},
        {"skill": "b", "skill_version": 3, "model_profile": "m"},
    ]
    journal = [
        {"skill": "a", "action": "switched", "to_version": 1},
        {"skill": "a", "action": "switched", "to_version": 2},
        {"skill": "a", "action": "switched", "to_version": 1},  # rolled back to 1
    ]
    live = dashboard.live_rows(rows, journal)
    assert [(r["skill"], r["skill_version"]) for r in live] == [("a", 1), ("b", 3)]


def test_live_rows_drops_a_retired_skill_back_to_the_newest_version():
    rows = [
        {"skill": "a", "skill_version": 1, "model_profile": "m"},
        {"skill": "a", "skill_version": 2, "model_profile": "m"},
    ]
    journal = [
        {"skill": "a", "action": "switched", "to_version": 1},
        {"skill": "a", "action": "retired", "to_version": None},
    ]
    assert [r["skill_version"] for r in dashboard.live_rows(rows, journal)] == [2]


def hashed(skill, version, content, started, verdict, mode="trigger"):
    return cell(skill, version, mode, verdict, content_hash=content, started_at=started)


def test_live_rows_counts_a_pre_release_row_by_the_live_content_hash():
    journal = [{"skill": "a", "action": "switched", "to_version": 1, "content_hash": "sha256:h1"}]
    rows = [
        hashed("a", 0, "sha256:h1", "2026-10-30", "pass"),
        hashed("a", 0, "sha256:other", "2026-10-30", "fail", mode="with_skill"),
    ]
    live = dashboard.live_rows(rows, journal)
    assert [(r["skill_version"], r["mode"]) for r in live] == [(0, "trigger")]


def test_live_rows_newest_run_per_model_and_mode_replaces_the_pre_release_row():
    journal = [{"skill": "a", "action": "switched", "to_version": 1, "content_hash": "sha256:h1"}]
    rows = [
        hashed("a", 0, "sha256:h1", "2026-10-29", "fail"),
        hashed("a", 1, "sha256:h1", "2026-10-30", "pass"),
    ]
    live = dashboard.live_rows(rows, journal)
    assert [(r["skill_version"], r["verdict"]) for r in live] == [(1, "pass")]


def test_summary_counts_a_release_recorded_before_publishing(make_client):
    journal = [{"skill": "audit-xls", "action": "switched", "to_version": 1, "content_hash": "sha256:h1"}]
    data = {
        "cells": [hashed("audit-xls", 0, "sha256:h1", "2026-10-30", "pass", mode="trigger")],
        "journal": journal,
    }
    sql = DashSql({COMMON: data, SERVING: {"tokens": []}})
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=sql)
    body = c.get("/api/results/dashboard").json()
    assert body["summary"]["verdicts"]["pass"] == 1 and body["summary"]["skills"] == 1


def test_dashboard_statements_read_the_content_hash_and_start_time_for_the_live_rule():
    statements = queries.dashboard_sql(make_cfg(), "common")
    for name in ("cells", "uplift"):
        assert "content_hash" in statements[name][0] and "started_at" in statements[name][0], name
    assert "content_hash" in statements["journal"][0]


def test_verdict_counts_and_health_count_a_null_verdict_as_inconclusive():
    cells = [
        cell("a", 1, "trigger", None, error_rate=None),
        cell("a", 1, "with_skill", "pass", error_rate=0.2),
    ]
    assert dashboard.verdict_counts(cells) == [
        {"model_profile": "claude-sonnet", "verdict": "inconclusive", "n": 1},
        {"model_profile": "claude-sonnet", "verdict": "pass", "n": 1},
    ]
    assert dashboard.health(cells) == [
        {"model_profile": "claude-sonnet", "cells": 2, "error_rate": 0.2, "inconclusive_rate": 0.5}
    ]


def test_a_catalog_model_verdict_is_not_counted_on_the_dashboard(make_client):
    data = common_data()
    data["cells"] = [
        *data["cells"],
        cell("sales-brief", 2, "trigger", "fail", model_profile="nova-lite", profile_kind="external_model"),
    ]
    sql = DashSql({COMMON: data})
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=sql)
    body = c.get("/api/results/dashboard").json()
    assert all(v["model_profile"] != "nova-lite" for v in body["verdicts"])
    assert all(h["model_profile"] != "nova-lite" for h in body["health"])
    assert body["summary"]["verdicts"]["fail"] == 0


class OldViewDashSql(DashSql):
    """Department views from before the model catalog: a statement that names profile_kind fails."""

    def query(self, statement, params=None):
        if "profile_kind" in statement and COMMON in statement:
            self.calls.append((statement, dict(params or {})))
            raise SqlError("statement failed")
        return super().query(statement, params)


def test_a_department_whose_views_predate_the_catalog_is_read_without_the_filter(make_client):
    sql = OldViewDashSql({COMMON: common_data(), EQ: common_data()})
    c = make_client(user_w=FakeWorkspace(), cfg=make_cfg(), sql=sql)
    r = c.get("/api/results/dashboard")
    assert r.status_code == 200 and r.json()["verdicts"]
    cells = [s for s, _ in sql.calls if "uplift_task_quality, error_rate" in s]
    assert any("profile_kind" not in s and COMMON in s for s in cells)
    assert all("profile_kind" in s for s in cells if EQ in s)  # the other department keeps the filter
