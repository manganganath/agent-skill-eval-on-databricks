# tests/unit/eval/test_scorers.py
import json

import mlflow
import pytest
from mlflow.entities import SpanType

from skillhub.eval.scorers import (
    invoked_skill_names,
    load_planted_bugs,
    make_planted_bugs_scorer,
    make_skill_trigger_scorer,
    planted_bugs_check,
    planted_bugs_feedback,
    skill_trigger_feedback,
)
from skillhub.evalspec import EvalCase


def make_trace(
    tmp_path,
    tool_fqns: list[str],
    final_text: str = "answer",
    case_id: int | None = None,
    hallucinated: list[str] = (),
):
    """Build a real trace with TOOL spans (the autouse fixture isolates the tracking store)."""
    with mlflow.start_span(name="agent_run", span_type=SpanType.AGENT) as root:
        root.set_outputs({"final_text": final_text})
        if case_id is not None:
            mlflow.update_current_trace(tags={"case_id": str(case_id)})
        for fqn in tool_fqns:
            with mlflow.start_span(name="skill_" + fqn, span_type=SpanType.TOOL) as t:
                t.set_attributes({"skill.fqn": fqn, "tool.name": "skill_" + fqn, "tool.args": "{}"})
        for name in hallucinated:  # offered-set miss: loop records the span but sets no skill.fqn
            with mlflow.start_span(name=name, span_type=SpanType.TOOL) as t:
                t.set_attributes({"tool.name": name, "tool.args": "{}"})
        trace_id = root.trace_id
    mlflow.flush_trace_async_logging()
    return mlflow.get_trace(trace_id)


def test_invoked_names_are_last_segment_in_call_order(tmp_path):
    t = make_trace(tmp_path, ["cat.dept_x.earnings_preview", "cat.common.audit_xls"])
    assert invoked_skill_names(t) == ["earnings_preview", "audit_xls"]


def test_no_tool_spans_means_no_invocation(tmp_path):
    t = make_trace(tmp_path, [])
    assert invoked_skill_names(t) == []
    fb = skill_trigger_feedback(t, None)
    assert fb.name == "skill_trigger"
    assert fb.value == "correct_reject"
    assert fb.metadata == {"invoked_skill": None, "expected_skill": None}


def test_correct_trigger_scored_from_trace(tmp_path):
    t = make_trace(tmp_path, ["cat.dept_x.earnings_preview"])
    fb = skill_trigger_feedback(t, "earnings_preview")
    assert fb.value == "correct"
    assert fb.metadata == {"invoked_skill": "earnings_preview", "expected_skill": "earnings_preview"}


def test_wrong_skill_when_sibling_fires(tmp_path):
    t = make_trace(tmp_path, ["cat.dept_x.morning_note"])
    fb = skill_trigger_feedback(t, "earnings_preview")  # a near-miss case expects the sibling
    assert fb.value == "wrong_skill"


def test_false_trigger_on_unrelated_input(tmp_path):
    t = make_trace(tmp_path, ["cat.dept_x.earnings_preview"])
    fb = skill_trigger_feedback(t, None)
    assert fb.value == "false_trigger"


def test_trigger_scorer_takes_labels_from_evals_json_not_from_a_stale_dataset_row(tmp_path):
    """Case 7 is a reviewed negative. An earlier or unmerged evals.json planted expected_skill="x" in
    its dataset row; when x fires on this unrelated prompt the case must be a false trigger, not correct."""
    cases = {7: EvalCase(id=7, prompt="Summarise this news article", should_trigger=False)}
    scorer = make_skill_trigger_scorer(cases, "x")
    t = make_trace(tmp_path, ["cat.dept_x.x"], case_id=7)
    stale = {"should_trigger": True, "expected_skill": "x", "reviewed": True}
    fb = scorer.run(
        inputs={"prompt": "Summarise this news article", "case_id": 7}, expectations=stale, trace=t
    )
    assert fb.value == "false_trigger"
    assert fb.metadata == {"invoked_skill": "x", "expected_skill": None}


def test_trigger_scorer_resolves_the_case_from_inputs_and_errors_on_an_unknown_case(tmp_path):
    cases = {1: EvalCase(id=1, prompt="Preview ACME", should_trigger=True)}
    scorer = make_skill_trigger_scorer(cases, "earnings_preview")
    untagged = make_trace(tmp_path, ["cat.dept_x.earnings_preview"])
    assert scorer.run(inputs={"prompt": "Preview ACME", "case_id": 1}, trace=untagged).value == "correct"
    unknown = make_trace(tmp_path, [], case_id=99)
    fb = scorer.run(inputs={"prompt": "?", "case_id": 99}, trace=unknown)
    assert fb.value is None and fb.error is not None


BUGS = [
    {"id": "bs_imbalance", "match": ["off by 1,250", "balance sheet"]},
    {"id": "hardcode", "match": ["hard-coded", "hardcoded"]},
    {"id": "broken_link", "match": ["#REF", "cross-sheet link"]},
    {"id": "sign", "match": ["working capital sign"]},
]


def test_planted_bugs_recall_matches_manifest_in_final_text(tmp_path):
    t = make_trace(
        tmp_path, [], final_text="Found: BS off by 1,250; hard-coded EBITDA in D12. Missed the link."
    )
    fb = planted_bugs_feedback(t, BUGS)
    assert fb.name == "planted_bugs_found"
    assert fb.value == pytest.approx(2 / 4)
    assert fb.metadata == {"found": ["bs_imbalance", "hardcode"], "total": 4}


def test_planted_bugs_with_empty_manifest_is_zero_with_total_zero(tmp_path):
    t = make_trace(tmp_path, [])
    fb = planted_bugs_feedback(t, [])
    assert fb.value == 0.0
    assert fb.metadata == {"found": [], "total": 0}


def test_planted_bugs_unknown_bug_scope_is_an_error_not_zero(tmp_path):
    t = make_trace(tmp_path, [], final_text="BS off by 1,250")
    fb = planted_bugs_feedback(t, BUGS, ["bs_imbalanse"])
    assert fb.name == "planted_bugs_found"
    assert fb.value is None
    assert fb.error is not None and "bs_imbalanse" in str(fb.error.error_message)


def test_planted_check_scores_only_cases_that_include_the_workbook(tmp_path):
    cases = {
        1: EvalCase(id=1, prompt="audit", should_trigger=True, fixtures=["northwind/model.xlsx"]),
        2: EvalCase(id=2, prompt="press", should_trigger=True, fixtures=["northwind/press_release.md"]),
    }
    in_scope = make_trace(tmp_path, [], final_text="BS off by 1,250", case_id=1)
    fb = planted_bugs_check(in_scope, cases, BUGS)
    assert fb.name == "planted_bugs_found" and fb.value == pytest.approx(1 / 4)
    out_of_scope = make_trace(tmp_path, [], final_text="BS off by 1,250", case_id=2)
    assert planted_bugs_check(out_of_scope, cases, BUGS) is None


def test_load_planted_bugs_reads_the_manifest_bugs(tmp_path):
    (tmp_path / "northwind").mkdir()
    (tmp_path / "northwind" / "model_bugs.json").write_text(
        json.dumps({"bugs": [{"id": "x", "match": ["y"]}]})
    )
    assert load_planted_bugs(tmp_path) == [{"id": "x", "match": ["y"]}]


def test_hallucinated_skill_call_does_not_count(tmp_path):
    t = make_trace(tmp_path, [], hallucinated=["skill_cat__dept_x__ghost"])
    assert invoked_skill_names(t) == []
    assert skill_trigger_feedback(t, None).value == "correct_reject"


def test_planted_bugs_match_is_case_insensitive_on_final_text(tmp_path):
    t = make_trace(tmp_path, [], final_text="Working Capital SIGN is flipped; HARD-CODED value")
    fb = planted_bugs_feedback(t, BUGS)
    assert fb.metadata["found"] == ["hardcode", "sign"]


def test_bug_scope_limits_recall_to_scoped_ids(tmp_path):
    cases = {
        1: EvalCase(
            id=1,
            prompt="audit",
            should_trigger=True,
            fixtures=["northwind/model.xlsx"],
            bug_scope=["hardcode", "sign"],
        ),
    }
    t = make_trace(tmp_path, [], final_text="hard-coded EBITDA", case_id=1)
    fb = planted_bugs_check(t, cases, BUGS)
    assert fb.value == pytest.approx(1 / 2)
    assert fb.metadata == {"found": ["hardcode"], "total": 2}


def test_max_turns_error_run_scores_on_empty_text(tmp_path):
    t = make_trace(tmp_path, [], final_text="")
    assert planted_bugs_feedback(t, BUGS).value == 0.0


def test_planted_bugs_scorer_returns_none_out_of_scope(tmp_path):
    (tmp_path / "northwind").mkdir()
    (tmp_path / "northwind" / "model_bugs.json").write_text(json.dumps({"bugs": BUGS}))
    cases = {2: EvalCase(id=2, prompt="p", should_trigger=True, fixtures=[])}
    sc = make_planted_bugs_scorer(cases, tmp_path)
    t = make_trace(tmp_path, [], case_id=2)
    assert sc(trace=t) is None
