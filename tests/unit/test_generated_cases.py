"""Validation of generated trigger cases and rubrics. Runs after `skillhub generate --missing`.

A skill whose trigger cases are all its author's own (a test-case file brought as it is) is not held to the generated
mix, and needs no rubric when its effectiveness cases are all assertion cases."""

from pathlib import Path

import pytest

from skillhub.bundle import content_hash, discover_bundles
from skillhub.evalspec import load_evals, load_rubric, rubric_optional

BUNDLES = discover_bundles(Path("skills"))


def _own_cases(ef) -> bool:
    """Every trigger case is the author's (origin human): none came from the generator."""
    trig = [c for c in ef.evals if c.kind == "trigger"]
    return bool(trig) and all(c.origin == "human" for c in trig)


def _scope(bundle, all_bundles):
    """Skills a department sees: its own plus common. common sees only common."""
    allowed = {"common"} if bundle.dept == "common" else {bundle.dept, "common"}
    return [b for b in all_bundles if b.dept in allowed and b.name != bundle.name]


def _id(b):
    return f"{b.dept}/{b.name}"


@pytest.mark.parametrize("b", BUNDLES, ids=_id)
def test_trigger_case_mix(b):
    ef = load_evals(b.root / "evals" / "evals.json")
    if _own_cases(ef):
        pytest.skip("the author's own trigger cases: lint and review check them")
    trig = [c for c in ef.evals if c.kind == "trigger"]
    positives = [c for c in trig if c.should_trigger]
    near = [c for c in trig if not c.should_trigger and c.expected_skill]
    unrelated = [c for c in trig if not c.should_trigger and not c.expected_skill]
    assert 6 <= len(positives) <= 10, len(positives)
    assert 3 <= len(near) <= 5, len(near)
    assert 2 <= len(unrelated) <= 4, len(unrelated)
    siblings = {s.name for s in _scope(b, BUNDLES)}
    for c in near:
        assert c.expected_skill in siblings, f"case {c.id}: {c.expected_skill} is out of scope"
        assert c.expected_skill != b.name, f"case {c.id}: near-miss names the skill itself"
    for c in trig:
        assert c.origin == "generated", c.id
    assert len({c.id for c in ef.evals}) == len(ef.evals)


@pytest.mark.parametrize("b", BUNDLES, ids=_id)
def test_rubric_shape_and_pointers(b):
    path = b.root / "evals" / "rubric.json"
    if not path.exists() and rubric_optional(load_evals(b.root / "evals" / "evals.json")):
        pytest.skip("no rubric: its effectiveness cases are assertion cases, judged on their assertions")
    r = load_rubric(path)
    assert r.skill_name == b.name
    assert 5 <= len(r.criteria) <= 10, len(r.criteria)
    assert len({c.id for c in r.criteria}) == len(r.criteria)
    for c in r.criteria:
        assert c.source.strip(), c.id
        assert 1 <= c.weight <= 5, c.id
        assert c.type in ("task_quality", "skill_format"), c.id
    assert r.source_hash == content_hash(b.files)


def test_six_effectiveness_files_survived_generation():
    six = {
        "earnings-analysis",
        "earnings-preview",
        "model-update",
        "morning-note",
        "audit-xls",
        "xlsx-author",
    }
    for b in BUNDLES:
        if b.name not in six:
            continue
        ef = load_evals(b.root / "evals" / "evals.json")
        eff_ids = sorted(c.id for c in ef.evals if c.kind == "effectiveness")
        assert eff_ids == [101, 102, 103, 104, 105], b.name


def test_a_test_case_file_with_assertions_on_its_trigger_cases_is_not_held_to_the_generated_mix():
    ef = load_evals(Path(__file__).parent / "data" / "trigger_assertions" / "evals.json")
    assert _own_cases(ef) and rubric_optional(ef)
    assert not any(_own_cases(load_evals(b.root / "evals" / "evals.json")) for b in BUNDLES)  # none shipped
