import mlflow
import pytest
from mlflow import MlflowClient

from skillhub.eval.outcomes import Outcome
from skillhub.eval.verdict import (
    CaseScore,
    case_score_from_assessments,
    compute_verdict,
    log_verdict,
    panel_majority,
    trigger_case_score,
    trigger_metrics,
    uplift,
    weighted_score,
)
from skillhub.evalspec import Criterion

KW = dict(fail_rate=0.5, warn_rate=0.8, max_error_rate=0.1, min_reviewed_cases=5)


def score(case_id, passed, *, repeat=0, reviewed=True, error=False, tq=None, sf=None):
    return CaseScore(
        case_id=case_id,
        repeat=repeat,
        reviewed=reviewed,
        error=error,
        passed=None if error else passed,
        task_quality=tq,
        skill_format=sf,
    )


def reviewed_set(n_cases, passes, repeat=0):
    return [score(i, passes[i], repeat=repeat) for i in range(n_cases)]


# --- panel majority and weighted scores ---


@pytest.mark.parametrize(
    "votes, expected",
    [
        ([True, True, False], True),
        ([True, False, False], False),
        ([True, False], False),  # tie is not a majority
        ([True, False, None], False),  # a valid vote exists and True is not a majority: not met
        ([True, True, None], True),
        ([None, True, True], True),  # 2 of 3 is a majority of the panel
        ([None, None], None),  # no judge answered
        ([], None),
        ([True, None, None], False),  # one judge of three cannot carry the panel
        ([False, None, None], False),
        ([True, None, None, None, None], False),
        ([False, False, None], False),
        ([True, True, None, None, None], False),  # 2 of 5 is not a majority of the panel
        ([True, True, True, None, None], True),
        ([True], True),
        ([None], None),
    ],
)
def test_panel_majority(votes, expected):
    assert panel_majority(votes) is expected


def test_weighted_score_uses_weights():
    assert weighted_score([(2, True), (1, False)]) == pytest.approx(2 / 3)
    assert weighted_score([]) is None


# --- case scores ---

CRITERIA = [
    Criterion(id="c1", text="a", type="task_quality", weight=2, source="s"),
    Criterion(id="c2", text="b", type="skill_format", weight=1, source="s"),
]


def test_effectiveness_case_passes_when_weighted_score_and_assertions_pass():
    cs = case_score_from_assessments(
        case_id=1,
        repeat=0,
        reviewed=True,
        criteria=CRITERIA,
        votes_by_criterion={"c1": [True, True, False], "c2": [False, False, True]},
        assertion_votes=[True, True, True],
        pass_score=0.6,
    )
    assert cs.error is False
    assert cs.task_quality == 1.0
    assert cs.skill_format == 0.0
    assert cs.passed is True  # total = 2/3 >= 0.6


def test_case_fails_below_pass_score():
    cs = case_score_from_assessments(
        case_id=1,
        repeat=0,
        reviewed=True,
        criteria=CRITERIA,
        votes_by_criterion={"c1": [False], "c2": [True]},
        assertion_votes=[True],
        pass_score=0.6,
    )
    assert cs.error is False and cs.passed is False  # 1/3 < 0.6


def test_effectiveness_case_fails_when_an_assertion_fails():
    cs = case_score_from_assessments(
        case_id=1,
        repeat=0,
        reviewed=True,
        criteria=CRITERIA,
        votes_by_criterion={"c1": [True], "c2": [True]},
        assertion_votes=[True, False],
        pass_score=0.6,
    )
    assert cs.passed is False and cs.error is False


def test_case_without_assertion_assessments_is_judged_on_the_rubric_alone():
    cs = case_score_from_assessments(
        case_id=1,
        repeat=0,
        reviewed=True,
        criteria=CRITERIA,
        votes_by_criterion={"c1": [True], "c2": [True]},
        assertion_votes=None,
        pass_score=0.6,
    )
    assert cs.passed is True


def test_expected_assertions_with_no_assessment_is_an_error():
    cs = case_score_from_assessments(
        case_id=1,
        repeat=0,
        reviewed=True,
        criteria=CRITERIA,
        votes_by_criterion={"c1": [True], "c2": [True]},
        assertion_votes=[],
        pass_score=0.6,
    )
    assert cs.error is True and cs.passed is None


def test_criterion_no_judge_answered_makes_the_case_an_error():
    cs = case_score_from_assessments(
        case_id=1,
        repeat=0,
        reviewed=True,
        criteria=CRITERIA,
        votes_by_criterion={"c1": [None, None], "c2": [True]},
        assertion_votes=[True],
        pass_score=0.6,
    )
    assert cs.error is True and cs.passed is None


@pytest.mark.parametrize(
    "votes, error",
    [
        ([True, False, None], False),
        ([True, None, None], False),
        ([True, True, None], False),
        ([None, None, None], True),
        ([], True),
    ],
)
def test_split_or_thin_panel_criterion_is_not_met_not_an_error(votes, error):
    cs = case_score_from_assessments(
        case_id=1,
        repeat=0,
        reviewed=True,
        criteria=CRITERIA,
        votes_by_criterion={"c1": votes, "c2": [True]},
        assertion_votes=[True],
        pass_score=0.0,
    )
    assert cs.error is error


def test_undecided_panel_criterion_is_not_met_in_the_score():
    cs = case_score_from_assessments(
        case_id=1,
        repeat=0,
        reviewed=True,
        criteria=CRITERIA,
        votes_by_criterion={"c1": [True, None, None], "c2": [True, True, None]},
        assertion_votes=[True, False, None],
        pass_score=0.0,
    )
    assert cs.error is False and cs.passed is False


def test_missing_criterion_assessment_is_an_error_not_a_fail():
    cs = case_score_from_assessments(
        case_id=1,
        repeat=0,
        reviewed=True,
        criteria=CRITERIA,
        votes_by_criterion={"c2": [True]},
        assertion_votes=[True],
        pass_score=0.6,
    )
    assert cs.error is True and cs.passed is None


def test_assertions_no_judge_answered_make_the_case_an_error():
    cs = case_score_from_assessments(
        case_id=1,
        repeat=0,
        reviewed=True,
        criteria=CRITERIA,
        votes_by_criterion={"c1": [True], "c2": [True]},
        assertion_votes=[None, None],
        pass_score=0.6,
    )
    assert cs.error is True


def test_explicit_error_flag_wins_over_votes():
    cs = case_score_from_assessments(
        case_id=1,
        repeat=0,
        reviewed=True,
        criteria=CRITERIA,
        votes_by_criterion={"c1": [True], "c2": [True]},
        assertion_votes=[True],
        pass_score=0.6,
        error=True,
    )
    assert cs.error is True and cs.passed is None and cs.task_quality is None


def test_trigger_case_score_maps_outcome_to_pass():
    assert trigger_case_score(1, 0, True, "correct").passed is True
    assert trigger_case_score(1, 0, True, Outcome.CORRECT_REJECT).passed is True
    for bad in ("wrong_skill", "missed", "false_trigger"):
        cs = trigger_case_score(1, 0, True, bad)
        assert cs.passed is False and cs.error is False
    assert trigger_case_score(1, 0, True, None).error is True
    assert trigger_case_score(1, 0, True, "bogus").error is True


# --- verdicts ---


def test_all_pass_is_pass():
    v = compute_verdict(reviewed_set(5, [True] * 5), **KW, judges_responded_ratio=1.0)
    assert v.verdict == "pass"
    assert v.pass_rate == 1.0
    assert v.n_reviewed == 5 and v.n_cases == 5


def test_pass_rate_exactly_at_fail_rate_is_warn_not_fail():
    v = compute_verdict(reviewed_set(6, [True, True, True, False, False, False]), **KW)
    assert v.pass_rate == 0.5
    assert v.verdict == "warn"


def test_below_fail_rate_is_fail():
    v = compute_verdict(reviewed_set(5, [True, False, False, False, False]), **KW)
    assert v.verdict == "fail"


def test_at_warn_rate_is_pass():
    v = compute_verdict(reviewed_set(5, [True, True, True, True, False]), **KW)
    assert v.pass_rate == pytest.approx(0.8)
    assert v.verdict == "pass"


def test_error_rate_above_max_is_inconclusive_even_when_passing():
    scores = reviewed_set(5, [True] * 5) + [score(9, None, error=True)]
    v = compute_verdict(scores, **KW)
    assert v.error_rate == pytest.approx(1 / 6)
    assert v.verdict == "inconclusive"


def test_error_rate_above_max_is_inconclusive_even_when_failing():
    scores = reviewed_set(5, [False] * 5) + [score(9, None, error=True)]
    assert compute_verdict(scores, **KW).verdict == "inconclusive"


def test_error_rate_at_max_is_not_inconclusive():
    scores = reviewed_set(9, [True] * 9) + [score(99, None, error=True)]
    v = compute_verdict(scores, **KW)
    assert v.error_rate == pytest.approx(0.1)
    assert v.verdict == "pass"


def test_errors_are_not_counted_as_fails():
    # 8 pass + 1 error out of 9 repeats: pass rate over valid results is 1.0, not 8/9
    scores = reviewed_set(8, [True] * 8) + [score(99, None, error=True)]
    v = compute_verdict(scores, **{**KW, "max_error_rate": 0.2})
    assert v.pass_rate == 1.0 and v.verdict == "pass"


def test_errored_reviewed_case_does_not_turn_a_fail_into_advisory():
    # probe: 5 reviewed cases, 1 errors on every repeat, 4 fail. Used to be advisory (non-blocking).
    scores = [score(0, None, error=True)] + [score(i, False) for i in range(1, 5)]
    v = compute_verdict(scores, **{**KW, "max_error_rate": 0.25})
    assert v.n_reviewed == 5
    assert v.verdict == "inconclusive"  # only 4 valid reviewed cases < 5; blocks like fail


def test_errored_reviewed_case_still_lets_a_real_fail_block():
    scores = [score(0, None, error=True)] + [score(i, False) for i in range(1, 6)]
    v = compute_verdict(scores, **{**KW, "max_error_rate": 0.25})
    assert v.n_reviewed == 6 and v.verdict == "fail"


def test_error_rate_over_reviewed_repeats_is_not_diluted_by_unreviewed_cases():
    scores = (
        [score(0, None, error=True)]
        + [score(i, True) for i in range(1, 5)]
        + [score(100 + i, True, reviewed=False) for i in range(20)]
    )
    # overall error rate 1/25 is fine, but 1 of 5 reviewed repeats errored
    v = compute_verdict(scores, **KW)
    assert v.error_rate == pytest.approx(1 / 25)
    assert v.verdict == "inconclusive"


def test_too_few_valid_reviewed_cases_is_inconclusive_not_advisory():
    scores = [score(i, None, error=True) for i in range(2)] + [score(i, True) for i in range(2, 5)]
    v = compute_verdict(scores, **{**KW, "max_error_rate": 0.5})
    assert v.n_reviewed == 5
    assert v.verdict == "inconclusive"  # 3 valid reviewed cases < min_reviewed_cases 5


def test_unreviewed_errors_are_counted_in_n_cases_only():
    scores = reviewed_set(5, [True] * 5) + [score(50, None, error=True, reviewed=False)]
    v = compute_verdict(scores, **{**KW, "max_error_rate": 0.2})
    assert v.n_cases == 6 and v.n_reviewed == 5 and v.verdict == "pass"


def test_empty_scores_are_inconclusive():
    v = compute_verdict([], **KW)
    assert v.verdict == "inconclusive" and v.n_cases == 0 and v.error_rate == 1.0


def test_all_errors_are_inconclusive():
    v = compute_verdict([score(i, None, error=True) for i in range(5)], **KW)
    assert v.verdict == "inconclusive" and v.pass_rate == 0.0


def test_most_judges_not_responding_is_inconclusive():
    v = compute_verdict(reviewed_set(5, [True] * 5), **KW, judges_responded_ratio=0.4)
    assert v.verdict == "inconclusive"


def test_half_the_judges_responding_is_not_inconclusive():
    v = compute_verdict(reviewed_set(5, [True] * 5), **KW, judges_responded_ratio=0.5)
    assert v.verdict == "pass"


def test_inconclusive_beats_advisory():
    v = compute_verdict(reviewed_set(2, [True] * 2), **KW, judges_responded_ratio=0.1)
    assert v.verdict == "inconclusive"


def test_fewer_than_min_reviewed_cases_is_advisory_even_with_perfect_pass_rate():
    v = compute_verdict(reviewed_set(4, [True] * 4), **KW)
    assert v.n_reviewed == 4
    assert v.verdict == "advisory"


def test_unreviewed_cases_never_gate():
    scores = reviewed_set(5, [True] * 5) + [score(100 + i, False, reviewed=False) for i in range(5)]
    v = compute_verdict(scores, **KW)
    assert v.pass_rate == 1.0
    assert v.verdict == "pass"
    assert v.n_reviewed == 5 and v.n_cases == 10


def test_unreviewed_only_run_is_advisory_even_with_zero_minimum():
    scores = [score(i, False, reviewed=False) for i in range(10)]
    assert compute_verdict(scores, **KW).verdict == "advisory"
    assert compute_verdict(scores, **{**KW, "min_reviewed_cases": 0}).verdict == "advisory"


def test_n_counts_are_distinct_cases_not_repeats():
    scores = [score(i, True, repeat=r) for i in range(2) for r in range(3)]  # 2 cases x 3 repeats
    v = compute_verdict(scores, **KW)
    assert v.n_reviewed == 2 and v.n_cases == 2
    assert v.verdict == "advisory"
    assert v.metrics["n_cases"] == 2.0 and v.metrics["n_reviewed"] == 2.0


def test_spread_is_stdev_of_per_repeat_pass_rates():
    r0 = [score(i, True, repeat=0) for i in range(5)]  # 1.0
    r1 = [score(i, i < 3, repeat=1) for i in range(5)]  # 0.6
    v = compute_verdict(r0 + r1, **KW)
    assert v.spread == pytest.approx(0.2)


def test_spread_is_zero_for_a_single_repeat():
    assert compute_verdict(reviewed_set(5, [True] * 5), **KW).spread == 0.0


def test_metrics_keys_present():
    v = compute_verdict(reviewed_set(5, [True] * 5), **KW)
    assert set(v.metrics) >= {"pass_rate", "spread", "error_rate", "n_cases", "n_reviewed"}


# --- uplift and trigger metrics ---


def test_uplift_is_difference_of_means_per_score_type():
    with_s = [CaseScore(1, 0, True, False, True, 1.0, 1.0), CaseScore(2, 0, True, False, True, 0.8, 0.6)]
    without_s = [CaseScore(1, 0, True, False, False, 0.5, 0.5), CaseScore(2, 0, True, False, False, 0.5, 0.5)]
    up = uplift(with_s, without_s)
    assert up["uplift_task_quality"] == pytest.approx(0.9 - 0.5)
    assert up["uplift_skill_format"] == pytest.approx(0.8 - 0.5)
    assert up["pass_rate_without"] == 0.0


def test_uplift_ignores_errors_and_unreviewed_when_reviewed_exist():
    with_s = [
        CaseScore(1, 0, True, False, True, 1.0, 1.0),
        CaseScore(2, 0, False, False, True, 0.0, 0.0),
        CaseScore(3, 0, True, True, None, None, None),
    ]
    without_s = [CaseScore(1, 0, True, False, True, 0.5, 0.5), CaseScore(2, 0, False, False, False, 1.0, 1.0)]
    up = uplift(with_s, without_s)
    assert up["uplift_task_quality"] == pytest.approx(0.5)
    assert up["pass_rate_without"] == 1.0


def test_uplift_omits_undefined_values_rather_than_inventing_zero():
    with_s = [CaseScore(1, 0, True, False, True, 1.0, None)]
    without_s = [CaseScore(1, 0, True, False, False, 0.5, None)]
    up = uplift(with_s, without_s)
    assert "uplift_skill_format" not in up
    assert up["uplift_task_quality"] == pytest.approx(0.5)
    assert uplift(with_s, []) == {}


def test_uplift_is_paired_so_a_one_sided_error_does_not_bias_it():
    with_s = [CaseScore(1, 0, True, False, True, 1.0, None), CaseScore(2, 0, True, True, None, None, None)]
    without_s = [
        CaseScore(1, 0, True, False, True, 1.0, None),
        CaseScore(2, 0, True, False, False, 0.0, None),
    ]
    up = uplift(with_s, without_s)
    assert up["uplift_task_quality"] == pytest.approx(0.0)  # only case 1 is paired
    assert up["pass_rate_without"] == 1.0


def test_uplift_unreviewed_fallback_applies_to_both_sides_together():
    # case 1 reviewed on both sides; case 2 unreviewed. Reviewed pairs exist, so case 2 must not leak in.
    with_s = [CaseScore(1, 0, True, False, True, 1.0, None), CaseScore(2, 0, False, False, True, 0.0, None)]
    without_s = [
        CaseScore(1, 0, True, False, True, 0.5, None),
        CaseScore(2, 0, False, False, True, 1.0, None),
    ]
    assert uplift(with_s, without_s)["uplift_task_quality"] == pytest.approx(0.5)
    # no reviewed pair at all: fall back to the unreviewed pairs for both sides
    w2 = [CaseScore(1, 0, False, False, True, 1.0, None)]
    wo2 = [CaseScore(1, 0, False, False, True, 0.25, None)]
    assert uplift(w2, wo2)["uplift_task_quality"] == pytest.approx(0.75)
    # reviewed on one side only is not a reviewed pair
    wo3 = [CaseScore(1, 0, True, False, True, 0.25, None)]
    assert uplift(w2, wo3)["uplift_task_quality"] == pytest.approx(0.75)


def test_uplift_averages_repeats_per_case_before_pairing():
    with_s = [CaseScore(1, 0, True, False, True, 1.0, None), CaseScore(1, 1, True, False, True, 0.0, None)]
    without_s = [CaseScore(1, 0, True, False, False, 0.25, None)]
    assert uplift(with_s, without_s)["uplift_task_quality"] == pytest.approx(0.25)


def test_uplift_with_no_paired_cases_is_empty():
    a = [CaseScore(1, 0, True, False, True, 1.0, None)]
    b = [CaseScore(2, 0, True, False, True, 1.0, None)]
    assert uplift(a, b) == {}


def test_trigger_metrics_accuracy_precision_recall_for_one_skill():
    pairs = [
        ("earnings_preview", "earnings_preview"),  # TP
        ("earnings_preview", None),  # FN (missed)
        (None, "earnings_preview"),  # FP (false trigger)
        (None, None),  # TN, correct reject
        ("morning_note", "earnings_preview"),  # FP for earnings_preview (wrong skill)
        ("morning_note", "morning_note"),  # correct for the sibling, not counted for this skill
    ]
    m = trigger_metrics(pairs, "earnings_preview")
    assert m["accuracy"] == pytest.approx(3 / 6)
    assert m["precision"] == pytest.approx(1 / 3)
    assert m["recall"] == pytest.approx(1 / 2)


def test_trigger_metrics_wrong_skill_invoked_is_a_false_negative():
    m = trigger_metrics([("a", "b"), ("a", "a")], "a")
    assert m["recall"] == 0.5 and m["precision"] == 1.0


def test_trigger_metrics_undefined_values_are_omitted():
    assert trigger_metrics([(None, None)], "earnings_preview") == {"accuracy": 1.0}
    assert trigger_metrics([], "x") == {}


# --- logging to a child run ---


@pytest.fixture
def run_id():
    # the autouse fixture in tests/conftest.py has already set the tracking store and active experiment
    with mlflow.start_run() as run:
        return run.info.run_id


def test_log_verdict_writes_metrics_and_verdict_tag(run_id):
    result = compute_verdict(reviewed_set(5, [True] * 5), **KW)
    log_verdict(run_id, result, extra_metrics={"accuracy": 0.9})
    stored = MlflowClient().get_run(run_id)
    assert stored.data.tags["verdict"] == "pass"
    assert stored.data.metrics["pass_rate"] == 1.0
    assert stored.data.metrics["accuracy"] == 0.9
    assert stored.data.metrics["n_reviewed"] == 5.0


def test_log_verdict_rejects_metric_names_the_sync_does_not_know(run_id):
    result = compute_verdict(reviewed_set(5, [True] * 5), **KW)
    with pytest.raises(ValueError, match="pass_rate_witout"):
        log_verdict(run_id, result, extra_metrics={"pass_rate_witout": 0.1})
    assert "verdict" not in MlflowClient().get_run(run_id).data.tags


def test_log_verdict_accepts_every_eval_runs_metric_column(run_id):
    result = compute_verdict(reviewed_set(5, [True] * 5), **KW)
    extras = {
        k: 0.5
        for k in (
            "accuracy",
            "precision",
            "recall",
            "pass_rate_without",
            "uplift_task_quality",
            "uplift_skill_format",
            "planted_bugs_recall",
        )
    }
    log_verdict(run_id, result, extra_metrics=extras)
    assert MlflowClient().get_run(run_id).data.metrics["planted_bugs_recall"] == 0.5


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_log_verdict_rejects_non_finite_values_before_any_write(run_id, bad):
    result = compute_verdict(reviewed_set(5, [True] * 5), **KW)
    with pytest.raises(ValueError, match="non-finite"):
        log_verdict(run_id, result, extra_metrics={"accuracy": bad})
    stored = MlflowClient().get_run(run_id)
    assert stored.data.metrics == {} and "verdict" not in stored.data.tags


def test_reviewed_cases_all_errored_is_inconclusive_not_advisory_from_unreviewed():
    # 2 reviewed cases error; 18 unreviewed pass. Overall error rate is below the max, so the
    # reviewed-only rule must decide, and unreviewed results must not stand in for them.
    scores = [score(i, None, error=True) for i in range(2)] + [
        score(100 + i, True, reviewed=False) for i in range(18)
    ]
    v = compute_verdict(scores, **{**KW, "max_error_rate": 0.5})
    assert v.n_reviewed == 2
    assert v.verdict == "inconclusive"
    assert v.pass_rate == 0.0  # not the unreviewed pass rate


def test_advisory_run_is_not_gated_by_reviewed_repeat_error_rate():
    # 3 reviewed repeats (< min 5), 1 errored; overall error rate is within max, reviewed rate is not.
    scores = [score(0, None, error=True), score(1, True), score(2, True)] + [
        score(100 + i, True, reviewed=False) for i in range(20)
    ]
    v = compute_verdict(scores, **KW)
    assert v.n_reviewed == 3 and v.verdict == "advisory"
