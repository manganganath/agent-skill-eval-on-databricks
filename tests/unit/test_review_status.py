from pathlib import Path

import pytest

from skillhub.evalspec import load_evals, load_rubric

SIX = {
    "earnings-analysis": "equity_research",
    "earnings-preview": "equity_research",
    "model-update": "equity_research",
    "morning-note": "equity_research",
    "audit-xls": "common",
    "xlsx-author": "common",
}
MIN_REVIEWED = 5  # defaults.min_reviewed_cases in config/hub.yaml


def _dir(skill):
    return Path("skills") / SIX[skill] / skill / "evals"


@pytest.mark.parametrize("skill", sorted(SIX))
def test_enough_reviewed_trigger_cases(skill):
    ef = load_evals(_dir(skill) / "evals.json")
    reviewed = [c for c in ef.evals if c.kind == "trigger" and c.reviewed]
    assert len(reviewed) >= MIN_REVIEWED, f"{skill}: {len(reviewed)} reviewed trigger cases"


@pytest.mark.parametrize("skill", sorted(SIX))
def test_enough_reviewed_effectiveness_cases(skill):
    ef = load_evals(_dir(skill) / "evals.json")
    reviewed = [c for c in ef.evals if c.kind == "effectiveness" and c.reviewed]
    assert len(reviewed) >= MIN_REVIEWED


@pytest.mark.parametrize("skill", sorted(SIX))
def test_rubric_reviewed(skill):
    assert load_rubric(_dir(skill) / "rubric.json").reviewed is True
