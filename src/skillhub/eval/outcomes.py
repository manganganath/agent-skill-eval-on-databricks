from __future__ import annotations

from enum import StrEnum


class Outcome(StrEnum):
    CORRECT = "correct"
    CORRECT_REJECT = "correct_reject"
    WRONG_SKILL = "wrong_skill"
    MISSED = "missed"
    FALSE_TRIGGER = "false_trigger"


def case_outcome(expected: str | None, invoked: str | None) -> Outcome:
    """Compare the expected skill NAME with the first invoked skill NAME."""
    if expected is None:
        return Outcome.CORRECT_REJECT if invoked is None else Outcome.FALSE_TRIGGER
    if invoked is None:
        return Outcome.MISSED
    return Outcome.CORRECT if invoked == expected else Outcome.WRONG_SKILL
