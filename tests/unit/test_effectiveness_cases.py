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


def test_audit_xls_scoped_cases_set_bug_scope():
    import json

    ef = load_evals(ROOT / "skills/common/audit-xls/evals/evals.json")
    bugs = {b["id"] for b in json.loads((ROOT / "fixtures/northwind/model_bugs.json").read_text())["bugs"]}
    by_id = {c.id: c for c in ef.evals}
    assert by_id[101].bug_scope is None
    for i in (102, 103, 104, 105):
        assert by_id[i].bug_scope and set(by_id[i].bug_scope) <= bugs, i
