# tests/unit/eval/test_outcomes.py
import pytest

from skillhub.eval.outcomes import Outcome, case_outcome


@pytest.mark.parametrize(
    "expected, invoked, outcome",
    [
        ("earnings_preview", "earnings_preview", Outcome.CORRECT),
        (None, None, Outcome.CORRECT_REJECT),
        ("earnings_preview", "morning_note", Outcome.WRONG_SKILL),
        ("earnings_preview", None, Outcome.MISSED),
        (None, "earnings_preview", Outcome.FALSE_TRIGGER),
        # near-miss for X (expected = sibling Y): X firing counts as wrong_skill; Y firing is correct
        ("morning_note", "morning_note", Outcome.CORRECT),
        ("morning_note", "earnings_preview", Outcome.WRONG_SKILL),
    ],
)
def test_case_outcome_rules(expected, invoked, outcome):
    assert case_outcome(expected, invoked) is outcome


def test_outcome_values_are_the_contract_strings():
    assert {o.value for o in Outcome} == {
        "correct",
        "correct_reject",
        "wrong_skill",
        "missed",
        "false_trigger",
    }
