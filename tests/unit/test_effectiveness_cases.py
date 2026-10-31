from pathlib import Path

import pytest

from skillhub.evalspec import load_evals

ROOT = Path(__file__).resolve().parents[2]

SIX = {
    "earnings-analysis": "equity_research",
    "earnings-preview": "equity_research",
    "model-update": "equity_research",
    "morning-note": "equity_research",
    "audit-xls": "common",
    "xlsx-author": "common",
}


@pytest.mark.parametrize("skill", sorted(SIX))
def test_five_effectiveness_cases_with_fixtures(skill):
    path = ROOT / "skills" / SIX[skill] / skill / "evals" / "evals.json"
    ef = load_evals(path)
    assert ef.skill_name == skill
    eff = [c for c in ef.evals if c.kind == "effectiveness"]
    assert len(eff) >= 5
    assert {101, 102, 103, 104, 105} <= {c.id for c in eff}
    for c in eff:
        assert c.origin == "human", c.id
        assert 2 <= len(c.assertions) <= 4, c.id
        assert c.fixtures, c.id
        for f in c.fixtures:
            assert (ROOT / "fixtures" / f).exists(), f"{skill} case {c.id}: missing {f}"


SHIPPED = sorted((ROOT / "skills").glob("*/*/evals/evals.json"))


@pytest.mark.parametrize("path", SHIPPED, ids=lambda p: f"{p.parts[-4]}/{p.parts[-3]}")
def test_assertion_cases_leave_the_planned_cases_and_cells_of_the_shipped_skills_unchanged(path):
    """No shipped trigger case has assertions, so each mode runs the cases of its own kind, the skill plans the
    modes and cells it planned before assertion cases ran in the effectiveness modes, and it still needs its rubric."""
    from skillhub.evalspec import (
        CASE_KIND,
        REQUEST_MODES,
        expand_modes,
        planned_cells,
        rubric_optional,
        run_modes,
    )

    ef = load_evals(path)
    assert not any(c.is_assertion_case for c in ef.evals)
    for kind in ("trigger", "effectiveness"):
        assert [c.id for c in ef.evals if kind in c.run_kinds] == [c.id for c in ef.evals if c.kind == kind]
    kinds = {c.kind for c in ef.evals}
    before = [m for m in expand_modes(REQUEST_MODES) if CASE_KIND[m] in kinds]
    assert run_modes(ef, REQUEST_MODES) == before
    gating = [m for m in before if m in ("trigger", "with_skill")]
    assert planned_cells(ef, default_models=["a", "b"]) == [f"{m}/{g}" for m in ("a", "b") for g in gating]
    assert not rubric_optional(ef)


def test_the_shipped_skill_check_covers_every_skill():
    assert len(SHIPPED) == len(list((ROOT / "skills").glob("*/*/SKILL.md"))) > 0


def test_audit_xls_scoped_cases_set_bug_scope():
    import json

    ef = load_evals(ROOT / "skills/common/audit-xls/evals/evals.json")
    bugs = {b["id"] for b in json.loads((ROOT / "fixtures/northwind/model_bugs.json").read_text())["bugs"]}
    by_id = {c.id: c for c in ef.evals}
    assert by_id[101].bug_scope is None
    for i in (102, 103, 104, 105):
        assert by_id[i].bug_scope and set(by_id[i].bug_scope) <= bugs, i
