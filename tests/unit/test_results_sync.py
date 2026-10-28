import json
import sys
from types import SimpleNamespace as NS

import pytest

from skillhub.naming import evals_schema
from skillhub.results import sync as sync_mod
from skillhub.results import tables
from tests.fakes import make_cfg


class ScriptedSql:
    """Records statements. COUNT returns 1 for run ids in `existing`; INSERT reports rows submitted."""

    def __init__(self, existing=()):
        self.existing = set(existing)
        self.calls: list[tuple[str, dict | None]] = []

    def execute(self, statement, params=None):
        self.calls.append((statement, params))
        if statement.lstrip().upper().startswith("SELECT COUNT"):
            return [{"n": int((params or {}).get("rid") in self.existing)}]
        if statement.lstrip().upper().startswith("INSERT"):
            return [{"num_affected_rows": len(json.loads(params["rows"]))}]
        return []

    def inserts(self, table):
        return [(s, p) for s, p in self.calls if s.startswith("INSERT") and f".{table} " in s]


CI_EVAL = "ci-eval-app-id"
TRUSTED = frozenset({CI_EVAL})


def run(
    run_id="r1",
    dept="common",
    n_cases=2,
    repeats=1,
    run_key="pr12_ab12cd3",
    parent="p1",
    verdict="pass",
    user_id=CI_EVAL,
    experiment_id="11",
    skill="earnings-preview",
):
    tags = {"run_key": run_key, "verdict": verdict}
    if parent is not None:  # only child runs carry parent_run_id
        tags["parent_run_id"] = parent
    return NS(
        info=NS(
            run_id=run_id,
            experiment_id=experiment_id,
            user_id=user_id,
            start_time=1_700_000_000_000,
            end_time=1_700_000_100_000,
        ),
        data=NS(
            params={
                "dept": dept,
                "skill": skill,
                "skill_fqn": f"hub.dept_{dept}.earnings-preview",
                "skill_version": "3",
                "content_hash": "sha256:x",
                "rubric_hash": "sha256:y",
                "dataset_digest": "d1",
                "model_profile": "gpt",
                "endpoint": "ep-gpt",
                "model_class": "frontier",
                "mode": "trigger",
                "judges": "gpt",
                "prompt_versions": "{}",
                "git_sha": "abc",
                "trigger_source": "pr",
                "requested_by": "u",
                "run_key": run_key,  # the runner logs run_key as a param and a tag
                "repeats": str(repeats),
            },
            tags=tags,
            metrics={
                "n_cases": float(n_cases),
                "pass_rate": 0.9,
                "spread": 0.1,
                "error_rate": 0.0,
                "n_reviewed": 1.0,
                "accuracy": 0.8,
            },
        ),
    )


def assess(name, value, source="CODE", ms=1, metadata=None, rationale=None):
    return NS(
        name=name,
        value=value,
        rationale=rationale,
        source=NS(source_type=source),
        metadata=metadata or {},
        create_time_ms=ms,
    )


def trace(trace_id, case_id, assessments, prompt="Preview ACME", spans=(), mode="trigger", repeat=0):
    return NS(
        info=NS(
            trace_id=trace_id,
            request_preview=prompt,
            assessments=list(assessments),
            tags={"case_id": str(case_id), "repeat": str(repeat), "mode": mode, "reviewed": "true"},
        ),
        data=NS(request=json.dumps({"prompt": prompt}), spans=list(spans)),
    )


A = assess("rubric_c1__gpt", True, source="LLM_JUDGE")


def tool_span(fqn):
    return NS(span_type="TOOL", name=f"skill_{fqn}", attributes={"skill.fqn": fqn})


class FakeTime:
    """Injected in place of the time module inside sync: sleep advances the clock."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.now += s


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(sync_mod, "time", FakeTime())
    monkeypatch.setattr(sync_mod, "configure_mlflow", lambda cfg: None)


class _Page(list):
    token = None


ROOT = "/Shared/skill-hub/skill_hub"


def _experiments(monkeypatch, *pairs):
    monkeypatch.setattr(
        sync_mod, "skill_experiments", lambda cfg: [NS(name=name, experiment_id=eid) for name, eid in pairs]
    )


def _search(monkeypatch, runs, calls=None):
    class C:
        def search_runs(self, experiment_ids, filter_string, max_results, page_token):
            assert filter_string == "tags.run_key = 'pr12_ab12cd3'"
            if calls is not None:
                calls.append(sorted(experiment_ids))
            return _Page([r for r in runs if r.info.experiment_id in experiment_ids])

    monkeypatch.setattr(sync_mod, "MlflowClient", C)


def test_child_runs_filter_on_run_key_and_parent(monkeypatch):
    _experiments(monkeypatch, (f"{ROOT}/common/earnings-preview", "11"))
    _search(monkeypatch, [run("r1"), run("parent", parent=None)])
    got = sync_mod.child_runs(make_cfg(), "pr12_ab12cd3", trusted=TRUSTED)
    assert [r.info.run_id for r in got] == ["r1"]


def test_child_runs_are_searched_per_department(monkeypatch):
    calls = []
    _experiments(
        monkeypatch, (f"{ROOT}/common/a", "11"), (f"{ROOT}/common/b", "12"), (f"{ROOT}/external/c", "13")
    )
    _search(monkeypatch, [], calls)
    sync_mod.child_runs(make_cfg(), "pr12_ab12cd3", trusted=TRUSTED)
    assert calls == [["11", "12"], ["13"]]


def test_child_runs_skip_untrusted_authors_with_a_counted_warning(monkeypatch, caplog):
    """A forged run by any other workspace user never reaches sync or the gates."""
    _experiments(monkeypatch, (f"{ROOT}/common/earnings-preview", "11"))
    forged = [run("f1", user_id="mallory@example.com"), run("f2", user_id=None)]
    _search(monkeypatch, [run("r1"), *forged])
    with caplog.at_level("WARNING", logger=sync_mod.__name__):
        got = sync_mod.child_runs(make_cfg(), "pr12_ab12cd3", trusted=TRUSTED)
    assert [r.info.run_id for r in got] == ["r1"]
    assert "skipped 2 child runs by untrusted authors" in caplog.text


def test_the_author_falls_back_to_the_mlflow_user_tag(monkeypatch, caplog):
    """Databricks returns no run.info.user_id; the MLflow client's mlflow.user tag names the author."""
    _experiments(monkeypatch, (f"{ROOT}/common/earnings-preview", "11"))
    by_tag = run("t1", user_id=None)
    by_tag.data.tags["mlflow.user"] = CI_EVAL
    stranger = run("t2", user_id=None)
    stranger.data.tags["mlflow.user"] = "mallory@example.com"
    _search(monkeypatch, [by_tag, stranger])
    with caplog.at_level("WARNING", logger=sync_mod.__name__):
        got = sync_mod.child_runs(make_cfg(), "pr12_ab12cd3", trusted=TRUSTED)
    assert [r.info.run_id for r in got] == ["t1"]
    assert sync_mod.run_author(by_tag) == CI_EVAL
    assert "skipped 1 child runs by untrusted authors" in caplog.text


def test_child_runs_skip_a_run_whose_dept_or_skill_differs_from_its_experiment(monkeypatch, caplog):
    """No confused deputy: a run in department A's experiment can't write into department B's tables."""
    _experiments(monkeypatch, (f"{ROOT}/common/earnings-preview", "11"))
    _search(monkeypatch, [run("r1"), run("x1", dept="external"), run("x2", skill="other")])
    with caplog.at_level("WARNING", logger=sync_mod.__name__):
        got = sync_mod.child_runs(make_cfg(), "pr12_ab12cd3", trusted=TRUSTED)
    assert [r.info.run_id for r in got] == ["r1"]
    assert "and 2 whose dept or skill differs" in caplog.text


def test_child_runs_skip_a_run_with_a_runner_param_set_as_a_tag(monkeypatch, caplog):
    """The runner logs dept as a param only. A dept tag was set by someone else: the run is not trusted."""
    _experiments(monkeypatch, (f"{ROOT}/common/earnings-preview", "11"))
    r = run("r1")
    r.data.tags["dept"] = "external"
    _search(monkeypatch, [r, run("r2")])
    with caplog.at_level("WARNING", logger=sync_mod.__name__):
        got = sync_mod.child_runs(make_cfg(), "pr12_ab12cd3", trusted=TRUSTED)
    assert [x.info.run_id for x in got] == ["r2"]
    assert "skipped tampered child run r1: runner param(s) set as tags: dept" in caplog.text
    assert "1 tampered" in caplog.text


def _planted_verdict_param():
    """The probe: the runner's verdict tag says fail, a planted param says pass."""
    r = run("r1", verdict="fail")
    r.data.params["verdict"] = "pass"
    return r


def test_a_planted_verdict_param_is_tampering():
    assert sync_mod.tampering(_planted_verdict_param()) == "runner tag(s) logged as params: verdict"
    assert sync_mod.tampering(run("r2")) is None


def test_tag_fail_plus_param_pass_never_syncs_as_pass(monkeypatch, caplog):
    """Regression: the run is skipped, so no eval_runs row says pass. The gates then miss its cell
    and fail closed."""
    _experiments(monkeypatch, (f"{ROOT}/common/earnings-preview", "11"))
    _search(monkeypatch, [_planted_verdict_param(), run("r2", n_cases=0)])
    monkeypatch.setattr(sync_mod, "_traces", lambda r: [])
    sql = ScriptedSql()
    with caplog.at_level("WARNING", logger=sync_mod.__name__):
        sync_mod.sync_run(make_cfg(), sql, "pr12_ab12cd3", trusted=TRUSTED)
    rows = [row for _, p in sql.inserts("eval_runs") for row in json.loads(p["rows"])]
    assert [row["run_id"] for row in rows] == ["r2"]
    assert "pass" not in {row["verdict"] for row in rows if row["run_id"] == "r1"}
    assert "skipped tampered child run r1" in caplog.text


def test_a_lone_planted_run_leaves_nothing_to_sync(monkeypatch):
    _experiments(monkeypatch, (f"{ROOT}/common/earnings-preview", "11"))
    _search(monkeypatch, [_planted_verdict_param()])
    with pytest.raises(sync_mod.SyncIncomplete, match="no trusted child runs"):
        sync_mod.sync_run(make_cfg(), ScriptedSql(), "pr12_ab12cd3", trusted=TRUSTED)


@pytest.mark.parametrize("key", sorted(sync_mod.CHILD_TAGS - sync_mod.CHILD_PARAMS))
def test_every_runner_tag_logged_as_a_param_is_tampering(key):
    r = run("r1")
    r.data.params[key] = "x"
    assert key in (sync_mod.tampering(r) or "")


@pytest.mark.parametrize("key", sorted(sync_mod.CHILD_PARAMS - sync_mod.CHILD_TAGS))
def test_every_runner_param_set_as_a_tag_is_tampering(key):
    r = run("r1")
    r.data.tags[key] = "x"
    assert key in (sync_mod.tampering(r) or "")


def test_a_run_key_tag_that_differs_from_the_param_is_tampering(monkeypatch):
    """Retagging another eval's run into this run key is caught by the immutable run_key param."""
    _experiments(monkeypatch, (f"{ROOT}/common/earnings-preview", "11"))
    r = run("r1", run_key="pr12_ab12cd3")
    r.data.params["run_key"] = "merge_old_a1"
    _search(monkeypatch, [r])
    assert "run_key tag differs" in sync_mod.tampering(r)
    assert sync_mod.child_runs(make_cfg(), "pr12_ab12cd3", trusted=TRUSTED) == []


def test_run_row_reads_tags_and_params_only_from_where_the_runner_writes_them():
    r = run("r1", verdict="fail")
    r.data.params["harness"] = "planted"  # tampering would skip the run; run_row still ignores the param
    r.data.tags["content_hash"] = "sha256:planted"
    row = sync_mod.run_row(r, "now")
    assert (row["verdict"], row["harness"], row["content_hash"]) == ("fail", "reference", "sha256:x")


@pytest.mark.parametrize(
    "name",
    [
        "/Shared/skill-hub/skill_hub/stray",  # too short
        "/Shared/skill-hub/skill_hub/a/b/c",  # too deep
        "/Shared/skill-hub/skill_hub/ghost/x",  # unknown department
        "/Shared/skill-hub/common/old-layout",  # older path without the catalog
    ],
)
def test_child_runs_skip_bad_experiments_instead_of_failing(monkeypatch, caplog, name):
    """One badly named experiment must not fail every sync."""
    _experiments(monkeypatch, (name, "99"), (f"{ROOT}/common/earnings-preview", "11"))
    _search(monkeypatch, [run("r1")])
    with caplog.at_level("WARNING", logger=sync_mod.__name__):
        got = sync_mod.child_runs(make_cfg(), "pr12_ab12cd3", trusted=TRUSTED)
    assert [r.info.run_id for r in got] == ["r1"]
    assert name in caplog.text


def test_child_runs_default_trust_comes_from_trusted_run_authors(monkeypatch):
    _experiments(monkeypatch, (f"{ROOT}/common/earnings-preview", "11"))
    _search(monkeypatch, [run("r1"), run("r2", user_id="app-id")])
    monkeypatch.setattr(sync_mod, "trusted_run_authors", lambda: frozenset({"app-id"}))
    assert [r.info.run_id for r in sync_mod.child_runs(make_cfg(), "pr12_ab12cd3")] == ["r2"]


def test_sync_run_passes_trusted_authors_to_child_runs(monkeypatch):
    seen = {}

    def fake(cfg, key, *, trusted=None):
        seen["trusted"] = trusted
        return [run("r1", n_cases=0)]

    monkeypatch.setattr(sync_mod, "child_runs", fake)
    monkeypatch.setattr(sync_mod, "_traces", lambda r: [])
    sync_mod.sync_run(make_cfg(), ScriptedSql(), "pr12_ab12cd3", trusted=TRUSTED)
    assert seen["trusted"] == TRUSTED


def test_run_row_records_the_run_author():
    assert sync_mod.run_row(run("r1"), "now")["run_author"] == CI_EVAL


def test_child_run_without_verdict_is_not_yet_complete(monkeypatch):
    r = run("r1")
    del r.data.tags["verdict"]  # verdict.py logs it last
    monkeypatch.setattr(sync_mod, "child_runs", lambda *a, **kw: [r])
    with pytest.raises(sync_mod.SyncIncomplete, match="verdict"):
        sync_mod.sync_run(make_cfg(), ScriptedSql(), "pr12_ab12cd3", timeout_s=5)


def rubric(reviewed=False):
    from skillhub.evalspec import Criterion, Rubric

    return Rubric(
        skill_name="earnings-preview",
        source_hash="sha256:src",
        generated_by="gpt",
        reviewed=reviewed,
        criteria=[
            Criterion(id=f"c{i}", text=f"Criterion {i}", type="task_quality", weight=3, source="SKILL.md")
            for i in range(1, 6)
        ],
    )


def test_record_rubric_is_insert_only_and_keyed_on_rubric_hash():
    from skillhub.evalspec import rubric_hash

    sql = ScriptedSql()
    sync_mod.record_rubric(make_cfg(), sql, "common", rubric())
    stmts = sql.inserts("rubrics")
    assert len(stmts) == 1
    stmt, params = stmts[0]
    assert f"{evals_schema(make_cfg(), 'common')}.rubrics" in stmt
    assert "WHERE NOT EXISTS" in stmt and "x.`rubric_hash` <=> r.`rubric_hash`" in stmt
    assert not any(w in stmt for w in ("UPDATE", "MERGE", "DELETE"))
    row = json.loads(params["rows"])[0]
    assert row["rubric_hash"] == rubric_hash(rubric()) and row["reviewed"] is False


def test_dedupes_repeated_assessments():
    t = trace(
        "t1",
        1,
        [
            assess("rubric_c1__gpt", True, source="LLM_JUDGE", ms=1),
            assess(
                "rubric_c1__gpt", False, source="LLM_JUDGE", ms=5
            ),  # repeated append-only row: latest wins
            assess("rubric_c1__gpt", True, source="HUMAN", ms=9),  # human label kept beside the judge
            assess("skill_trigger", "correct", ms=2, metadata={"invoked_skill": "earnings-preview"}),
        ],
    )
    rows = sync_mod.case_rows("r1", [t])
    judge = [r for r in rows if r["assessment_name"] == "rubric_c1__gpt" and r["source_type"] == "LLM_JUDGE"]
    human = [r for r in rows if r["assessment_name"] == "rubric_c1__gpt" and r["source_type"] == "HUMAN"]
    assert len(judge) == 1 and judge[0]["value"] == "false"
    assert len(human) == 1 and human[0]["value"] == "true"
    assert len(rows) == 3


def test_invoked_skill_from_metadata_then_tool_span():
    meta = trace(
        "t1",
        1,
        [
            assess(
                "skill_trigger",
                "correct",
                metadata={"invoked_skill": "earnings-preview", "expected_skill": "earnings-preview"},
            )
        ],
    )
    span = trace(
        "t2", 2, [assess("skill_trigger", "wrong_skill")], spans=[tool_span("hub.dept_common.model-update")]
    )
    r1 = sync_mod.case_rows("r1", [meta])[0]
    r2 = sync_mod.case_rows("r1", [span])[0]
    assert (r1["invoked_skill"], r1["expected_skill"]) == ("earnings-preview", "earnings-preview")
    assert r2["invoked_skill"] == "model-update"
    assert r2["trigger_outcome"] == "wrong_skill"


def test_human_pass_fail_is_normalised_to_booleans():
    t = trace("t1", 1, [assess("assertions__gpt", "pass", source="HUMAN")])
    assert sync_mod.case_rows("r1", [t])[0]["value"] == "true"


def test_wait_for_traces_until_count_matches(monkeypatch):
    r = run(n_cases=2, repeats=2)  # expects 4 traces
    seq = iter(
        [
            [trace("a", 1, [A])],
            [trace("a", 1, [A]), trace("b", 2, [A])],
            [trace(f"{c}{k}", c, [A], repeat=k) for c in (1, 2) for k in (0, 1)],
        ]
    )
    monkeypatch.setattr(sync_mod.mlflow, "search_traces", lambda **kw: next(seq))
    traces, _ = sync_mod.wait_for_traces(r, timeout_s=60)
    assert len(traces) == 4


def test_missing_verdict_is_rejected_up_front_and_nothing_is_written(monkeypatch):
    r = run("r1", n_cases=3, repeats=1)
    del r.data.tags["verdict"]
    monkeypatch.setattr(sync_mod, "child_runs", lambda *a, **kw: [r])
    monkeypatch.setattr(sync_mod.mlflow, "search_traces", lambda **kw: [trace("a", 1, [A])])
    sql = ScriptedSql()
    with pytest.raises(sync_mod.SyncIncomplete, match="verdict"):
        sync_mod.sync_run(make_cfg(), sql, "pr12_ab12cd3", timeout_s=5)
    assert not any(s.startswith("INSERT") for s, _ in sql.calls)


def test_timeout_with_verdict_writes_available_rows_and_warns(monkeypatch, caplog):
    monkeypatch.setattr(sync_mod, "child_runs", lambda *a, **kw: [run("r1", n_cases=3, repeats=1)])
    monkeypatch.setattr(sync_mod.mlflow, "search_traces", lambda **kw: [trace("a", 1, [A])])
    sql = ScriptedSql()
    with caplog.at_level("WARNING", logger="skillhub.results.sync"):
        report = sync_mod.sync_run(make_cfg(), sql, "pr12_ab12cd3", timeout_s=5)
    assert report.runs_inserted == 1 and report.cases_inserted == 1
    assert "1 of 3 cells ready" in caplog.text and "r1" in caplog.text


def test_duplicate_error_traces_do_not_fill_the_cell_count(monkeypatch):
    r = run(n_cases=2, repeats=1)
    e1, e2 = trace("e1", 1, []), trace("e2", 1, [])  # predict retries: two ERROR traces, one cell
    e1.info.state = e2.info.state = NS(value="ERROR")
    monkeypatch.setattr(sync_mod.mlflow, "search_traces", lambda **kw: [e1, e2])
    assert len(sync_mod._traces(r)) == 2  # the raw count equals n_cases * repeats
    with pytest.raises(sync_mod.SyncIncomplete, match="1 of 2 cells"):
        sync_mod.wait_for_traces(r, timeout_s=10)


def test_mid_run_failed_child_syncs_with_run_row_and_no_cases(monkeypatch):
    r = run("r1", n_cases=0, verdict="inconclusive")
    del r.data.params["repeats"]
    monkeypatch.setattr(sync_mod, "child_runs", lambda *a, **kw: [r])
    monkeypatch.setattr(sync_mod.mlflow, "search_traces", lambda **kw: [])
    sql = ScriptedSql()
    report = sync_mod.sync_run(make_cfg(), sql, "pr12_ab12cd3", timeout_s=5)
    assert report.runs_inserted == 1 and report.cases_inserted == 0
    assert len(sql.inserts("eval_runs")) == 1 and not sql.inserts("eval_cases")


def test_sync_inserts_insert_only_and_cases_before_run(monkeypatch):
    monkeypatch.setattr(sync_mod, "child_runs", lambda *a, **kw: [run("r1", n_cases=1)])
    monkeypatch.setattr(
        sync_mod.mlflow,
        "search_traces",
        lambda **kw: [
            trace(
                "a", 1, [assess("skill_trigger", "correct", metadata={"invoked_skill": "earnings-preview"})]
            )
        ],
    )
    sql = ScriptedSql()
    report = sync_mod.sync_run(make_cfg(), sql, "pr12_ab12cd3", timeout_s=5)
    assert report.runs_inserted == 1 and report.cases_inserted == 1
    stmts = [s for s, _ in sql.calls if s.startswith("INSERT")]
    assert "eval_cases" in stmts[0] and "eval_runs" in stmts[1]
    for s in stmts:
        assert "WHERE NOT EXISTS" in s
        assert not any(w in s for w in ("UPDATE", "MERGE", "DELETE"))


def test_existing_run_keeps_its_row_but_picks_up_late_case_rows(monkeypatch):
    monkeypatch.setattr(sync_mod, "child_runs", lambda *a, **kw: [run("r1")])
    monkeypatch.setattr(sync_mod.mlflow, "search_traces", lambda **kw: [trace("a", 1, [A])])
    sql = ScriptedSql(existing=["r1"])
    report = sync_mod.sync_run(make_cfg(), sql, "pr12_ab12cd3", timeout_s=5)
    assert report.runs_inserted == 0 and report.cases_inserted == 1
    assert len(sql.inserts("eval_cases")) == 1 and not sql.inserts("eval_runs")


def test_rebuild_rereads_existing_runs(monkeypatch):
    monkeypatch.setattr(sync_mod, "child_runs", lambda *a, **kw: [run("r1", n_cases=1)])
    seen = []
    monkeypatch.setattr(
        sync_mod.mlflow, "search_traces", lambda **kw: seen.append(kw) or [trace("a", 1, [A])]
    )
    sync_mod.sync_run(make_cfg(), ScriptedSql(existing=["r1"]), "pr12_ab12cd3", timeout_s=5, rebuild=True)
    assert seen and seen[0]["run_id"] == "r1" and seen[0]["locations"] == ["11"]


def test_missing_repeats_param_is_incomplete():
    r = run()
    del r.data.params["repeats"]
    with pytest.raises(sync_mod.SyncIncomplete, match="repeats"):
        sync_mod._expected_cells(r)


def test_run_key_is_validated():
    with pytest.raises(ValueError):
        sync_mod.child_runs(make_cfg(), "Bad-Key", trusted=TRUSTED)


def test_wait_times_out_when_a_trace_has_no_assessment(monkeypatch):
    r = run(n_cases=1, repeats=1)
    monkeypatch.setattr(sync_mod.mlflow, "search_traces", lambda **kw: [trace("a", 1, [])])
    with pytest.raises(sync_mod.SyncIncomplete, match="0 of 1 cells"):
        sync_mod.wait_for_traces(r, timeout_s=20)


def test_wait_polls_until_assessments_arrive(monkeypatch):
    r = run(n_cases=1, repeats=1)
    seq = iter([[trace("a", 1, [])], [trace("a", 1, [A])]])
    monkeypatch.setattr(sync_mod.mlflow, "search_traces", lambda **kw: next(seq))
    traces, waited = sync_mod.wait_for_traces(r, timeout_s=60)
    assert len(traces) == 1 and waited == sync_mod.POLL_S


def test_reviewed_tag_is_compared_case_insensitively():
    t = trace("a", 1, [A])
    t.info.tags["reviewed"] = "True"
    assert sync_mod.case_rows("r1", [t])[0]["reviewed"] is True
    t.info.tags["reviewed"] = "false"
    assert sync_mod.case_rows("r1", [t])[0]["reviewed"] is False


def test_field_lists_equal_the_ddl_columns():
    cols = tables.table_columns()
    assert [n for n, _ in sync_mod.RUN_FIELDS] == cols["eval_runs"]
    assert [n for n, _ in sync_mod.CASE_FIELDS] == cols["eval_cases"]
    assert [n for n, _ in sync_mod.RUBRIC_FIELDS] == cols["rubrics"]


def test_insert_names_its_columns_and_joins_null_safe(monkeypatch):
    monkeypatch.setattr(sync_mod, "child_runs", lambda *a, **kw: [run("r1", n_cases=1)])
    monkeypatch.setattr(sync_mod.mlflow, "search_traces", lambda **kw: [trace("a", 1, [A])])
    sql = ScriptedSql()
    sync_mod.sync_run(make_cfg(), sql, "pr12_ab12cd3", timeout_s=5)
    stmt, _ = sql.inserts("eval_cases")[0]
    assert "(`run_id`, `trace_id`, `assessment_name`" in stmt.split(" SELECT ")[0]
    assert "x.`trace_id` <=> r.`trace_id`" in stmt and "x.`source_type` <=> r.`source_type`" in stmt


def test_sync_command_applies_make_run_key(monkeypatch):
    from typer.testing import CliRunner

    from skillhub import cli
    from skillhub.results import sync as real

    seen = {}
    monkeypatch.setattr(cli, "_cfg", lambda ctx: make_cfg())
    monkeypatch.setattr("skillhub.auth.workspace_client", lambda: object())
    # skillhub.sql is not always present; stub the module so the lazy import resolves
    monkeypatch.setitem(sys.modules, "skillhub.sql", NS(SqlClient=lambda w, wid: object()))

    def fake_sync(cfg, sql, key, *, timeout_s=300, rebuild=False):
        seen.update(key=key, rebuild=rebuild)
        return real.SyncReport(key, 1, 2, 0.0)

    monkeypatch.setattr(real, "sync_run", fake_sync)
    res = CliRunner().invoke(cli.app, ["sync", "--run-key", "PR12-ab12cd3", "--rebuild"])
    assert res.exit_code == 0, res.output
    assert seen == {"key": "pr12_ab12cd3", "rebuild": True}
    assert json.loads(res.output)["cases_inserted"] == 2


def test_no_child_runs_is_incomplete(monkeypatch):
    monkeypatch.setattr(sync_mod, "child_runs", lambda *a, **kw: [])
    with pytest.raises(sync_mod.SyncIncomplete, match="no trusted child runs for run_key pr12_ab12cd3"):
        sync_mod.sync_run(make_cfg(), ScriptedSql(), "pr12_ab12cd3", timeout_s=5)


def test_errored_unassessed_trace_does_not_block_the_wait(monkeypatch):
    r = run(n_cases=2, repeats=1)
    bad = trace("b", 2, [])
    bad.info.state = NS(value="ERROR")
    monkeypatch.setattr(sync_mod.mlflow, "search_traces", lambda **kw: [trace("a", 1, [A]), bad])
    traces, _ = sync_mod.wait_for_traces(r, timeout_s=20)
    assert len(traces) == 2


def test_errored_trace_still_counts_toward_the_expected_total(monkeypatch):
    r = run(n_cases=2, repeats=1)
    bad = trace("b", 2, [])
    bad.info.state = NS(value="ERROR")
    monkeypatch.setattr(sync_mod.mlflow, "search_traces", lambda **kw: [bad])
    with pytest.raises(sync_mod.SyncIncomplete, match="1 of 2 cells"):
        sync_mod.wait_for_traces(r, timeout_s=10)


def test_missing_n_cases_metric_is_incomplete():
    r = run()
    del r.data.metrics["n_cases"]
    with pytest.raises(sync_mod.SyncIncomplete, match="n_cases"):
        sync_mod._expected_cells(r)


def test_trace_without_case_tags_is_incomplete():
    t = trace("a", 1, [A])
    del t.info.tags["case_id"]
    with pytest.raises(sync_mod.SyncIncomplete, match="case_id"):
        sync_mod.case_rows("r1", [t])


def _invoke_sync(monkeypatch, fake_sync, key="pr12-ab12cd3"):
    from typer.testing import CliRunner

    from skillhub import cli

    monkeypatch.setattr(cli, "_cfg", lambda ctx: make_cfg())
    monkeypatch.setattr("skillhub.auth.workspace_client", lambda: object())
    monkeypatch.setitem(sys.modules, "skillhub.sql", NS(SqlClient=lambda w, wid: object()))
    monkeypatch.setattr(sync_mod, "sync_run", fake_sync)
    return CliRunner().invoke(cli.app, ["sync", "--run-key", key])


def test_sync_command_exits_1_on_sync_incomplete(monkeypatch):
    def fake(*a, **k):
        raise sync_mod.SyncIncomplete("no child runs for run_key x")

    res = _invoke_sync(monkeypatch, fake)
    assert res.exit_code == 1 and "no child runs" in res.output
    assert "Traceback" not in res.output


def test_sync_command_exits_1_on_bad_run_key_without_traceback(monkeypatch):
    res = _invoke_sync(monkeypatch, lambda *a, **k: None, key="---")
    assert res.exit_code == 1 and "error:" in res.output and "Traceback" not in res.output


def test_sync_command_exits_1_on_unknown_department(monkeypatch):
    def fake(*a, **k):
        raise ValueError("run r1 names unknown department 'x'")

    res = _invoke_sync(monkeypatch, fake)
    assert res.exit_code == 1 and "unknown department" in res.output


def test_experiment_dept_parses_experiment_path():
    from skillhub.naming import experiment_dept, experiment_parts, experiment_path

    cfg = make_cfg()
    path = experiment_path(cfg, "common", "earnings-preview")
    assert path == "/Shared/skill-hub/skill_hub/common/earnings-preview"
    assert experiment_dept(cfg, path) == "common"
    assert experiment_parts(cfg, path) == ("common", "earnings-preview")
    for bad in ("/Users/x/y", "/Shared/skill-hub/common/x", "/Shared/skill-hub/other_hub/common/x"):
        with pytest.raises(ValueError):
            experiment_dept(cfg, bad)


def test_error_retry_next_to_unscored_ok_trace_is_not_ready():
    err, ok = trace("e", 1, []), trace("o", 1, [])
    err.info.state = NS(value="ERROR")
    ready, seen = sync_mod._ready_cells([err, ok])
    assert ready == set() and seen == {("1", "0")}
    ok.info.assessments = [A]
    assert sync_mod._ready_cells([err, ok])[0] == {("1", "0")}


def test_partial_path_drops_untagged_and_modeless_traces_and_counts_them(monkeypatch, caplog):
    no_mode, untagged = trace("m", 2, [A]), trace("u", 3, [A])
    del no_mode.info.tags["mode"]
    del untagged.info.tags["case_id"]
    r = run("r1", n_cases=3, repeats=1)
    monkeypatch.setattr(
        sync_mod.mlflow, "search_traces", lambda **kw: [trace("a", 1, [A]), no_mode, untagged]
    )
    with caplog.at_level("WARNING", logger="skillhub.results.sync"):
        traces, _ = sync_mod.wait_for_traces(r, timeout_s=5, allow_partial=True)
    assert [t.info.trace_id for t in traces] == ["a"]
    assert "2 untagged traces dropped" in caplog.text and "re-run sync" in caplog.text


def test_a_scored_run_stops_waiting_once_traces_stop_changing(monkeypatch, caplog):
    """A run with a verdict is finished. When a cell never gets an assessment (seen live: one OK trace
    with none), sync waits only until nothing changes for STALL_S, not for the whole timeout."""
    r = run("r1", n_cases=2, repeats=1)
    monkeypatch.setattr(
        sync_mod.mlflow, "search_traces", lambda **kw: [trace("a", 1, [A]), trace("b", 2, [])]
    )
    with caplog.at_level("WARNING", logger="skillhub.results.sync"):
        traces, waited = sync_mod.wait_for_traces(r, timeout_s=300, allow_partial=True)
    assert len(traces) == 2
    assert sync_mod.STALL_S <= waited < 300
    assert "1 of 2 cells ready" in caplog.text


def test_an_unscored_run_still_waits_for_the_full_timeout(monkeypatch):
    r = run("r1", n_cases=2, repeats=1)
    monkeypatch.setattr(
        sync_mod.mlflow, "search_traces", lambda **kw: [trace("a", 1, [A]), trace("b", 2, [])]
    )
    with pytest.raises(sync_mod.SyncIncomplete):
        sync_mod.wait_for_traces(r, timeout_s=300)
    assert sync_mod.time.monotonic() >= 300


def test_child_run_without_harness_tag_syncs_reference(monkeypatch):
    r = run("r1", n_cases=0, verdict="inconclusive")
    del r.data.params["repeats"]
    monkeypatch.setattr(sync_mod, "child_runs", lambda *a, **kw: [r])
    monkeypatch.setattr(sync_mod.mlflow, "search_traces", lambda **kw: [])
    sql = ScriptedSql()
    sync_mod.sync_run(make_cfg(), sql, "pr12_ab12cd3", timeout_s=5)
    rows = json.loads(sql.inserts("eval_runs")[0][1]["rows"])
    assert rows[0]["harness"] == "reference"


def test_child_run_with_harness_tag_syncs_that_harness(monkeypatch):
    r = run("r1", n_cases=0, verdict="inconclusive")
    del r.data.params["repeats"]
    r.data.tags["harness"] = "codex"
    monkeypatch.setattr(sync_mod, "child_runs", lambda *a, **kw: [r])
    monkeypatch.setattr(sync_mod.mlflow, "search_traces", lambda **kw: [])
    sql = ScriptedSql()
    sync_mod.sync_run(make_cfg(), sql, "pr12_ab12cd3", timeout_s=5)
    rows = json.loads(sql.inserts("eval_runs")[0][1]["rows"])
    assert rows[0]["harness"] == "codex"
