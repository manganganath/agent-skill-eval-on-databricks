import pytest
from pydantic import ValidationError

from skillhub.evalspec import (
    Criterion,
    EvalCase,
    EvalFile,
    Rubric,
    load_evals,
    load_rubric,
    rubric_hash,
    save_evals,
    save_rubric,
)


def test_resolved_expected_truth_table():
    pos = EvalCase(id=1, prompt="p", should_trigger=True)
    neg = EvalCase(id=2, prompt="p", should_trigger=False)
    near = EvalCase(id=3, prompt="p", should_trigger=False, expected_skill="morning-note")
    assert pos.resolved_expected("earnings-preview") == "earnings-preview"
    assert neg.resolved_expected("earnings-preview") is None
    assert near.resolved_expected("earnings-preview") == "morning-note"


def test_an_assertion_case_is_a_trigger_case_that_should_fire_and_has_assertions():
    asserted = EvalCase(id=1, prompt="p", should_trigger=True, assertions=["In German"])
    assert asserted.is_assertion_case and asserted.run_kinds == ("trigger", "effectiveness")
    others = [
        EvalCase(id=2, prompt="p", should_trigger=True),  # no assertions
        EvalCase(id=3, prompt="p", should_trigger=False, assertions=["In German"]),  # no answer to judge
        EvalCase(id=101, kind="effectiveness", prompt="p", should_trigger=True, assertions=["x"]),
    ]
    assert [(c.is_assertion_case, c.run_kinds) for c in others] == [
        (False, ("trigger",)),
        (False, ("trigger",)),
        (False, ("effectiveness",)),
    ]
    assert "is_assertion_case" not in asserted.model_dump() and "run_kinds" not in asserted.model_dump()


def test_bug_scope_defaults_to_none_and_round_trips(tmp_path):
    assert EvalCase(id=1, prompt="p", should_trigger=True).bug_scope is None
    case = EvalCase(id=1, prompt="p", should_trigger=True, bug_scope=["common", "operations"])
    p = tmp_path / "evals.json"
    save_evals(p, EvalFile(skill_name="x", evals=[case]))
    assert load_evals(p).evals[0].bug_scope == ["common", "operations"]


def test_save_is_byte_stable_and_round_trips(tmp_path):
    ef = EvalFile(skill_name="x", evals=[EvalCase(id=1, prompt="Ünïcode prompt", should_trigger=True)])
    p = tmp_path / "evals.json"
    save_evals(p, ef)
    first = p.read_bytes()
    save_evals(p, load_evals(p))
    assert p.read_bytes() == first
    assert "Ünïcode".encode() in first  # ensure_ascii=False
    assert first.endswith(b"\n")


def _criteria(n: int) -> list[Criterion]:
    return [
        Criterion(id=f"c{i}", text="t", type="task_quality", weight=1, source="SKILL.md")
        for i in range(1, n + 1)
    ]


def test_rubric_needs_five_to_ten_criteria():
    with pytest.raises(ValidationError):
        Rubric(skill_name="x", source_hash="sha256:0", generated_by="m", criteria=_criteria(4))
    Rubric(skill_name="x", source_hash="sha256:0", generated_by="m", criteria=_criteria(5))


def test_rubric_hash_tracks_content_and_round_trips(tmp_path):
    r = Rubric(skill_name="x", source_hash="sha256:0", generated_by="m", criteria=_criteria(5))
    h = rubric_hash(r)
    assert h == rubric_hash(Rubric.model_validate_json(r.model_dump_json()))
    assert rubric_hash(r.model_copy(update={"pass_score": 0.8})) != h
    p = tmp_path / "rubric.json"
    save_rubric(p, r)
    assert rubric_hash(load_rubric(p)) == h


def test_invalid_kind_is_rejected(tmp_path):
    p = tmp_path / "e.json"
    p.write_text(
        '{"skill_name": "x", "evals": [{"id": 1, "kind": "bogus", "prompt": "p", "should_trigger": true}]}'
    )
    with pytest.raises(ValidationError):
        load_evals(p)


# --- per-skill thresholds may only tighten hub.yaml defaults ---


def _defaults(**kw):
    from skillhub.config import Defaults

    return Defaults(models=["gpt"], **kw)


def _rubric(**kw):
    crit = [Criterion(id=f"c{i}", text="t", type="task_quality", weight=1, source="s") for i in range(5)]
    return Rubric(skill_name="x", source_hash="h", generated_by="g", criteria=crit, **kw)


def test_tighter_values_are_used_and_looser_ones_fall_back_to_the_default(caplog):
    from skillhub.evalspec import Criteria, gate_thresholds

    d = _defaults()
    t = gate_thresholds(d, Criteria(fail_rate=0.6, warn_rate=0.9), _rubric(pass_score=0.8), skill="common/x")
    assert (t.fail_rate, t.warn_rate, t.pass_score) == (0.6, 0.9, 0.8)
    assert caplog.text == ""
    with caplog.at_level("WARNING"):
        t = gate_thresholds(
            d, Criteria(fail_rate=0.0, warn_rate=0.0), _rubric(pass_score=0.1), skill="common/x"
        )
    assert (t.fail_rate, t.warn_rate, t.pass_score) == (d.fail_rate, d.warn_rate, d.pass_score)
    assert caplog.text.count("common/x") == 3 and "the default is used for gating" in caplog.text


def test_error_rate_and_reviewed_case_minimum_always_come_from_the_defaults():
    """A higher min_reviewed_cases would turn a gating verdict into a non-blocking advisory (verdict.py)."""
    from skillhub.evalspec import Criteria, gate_thresholds

    d = _defaults(max_error_rate=0.05, min_reviewed_cases=7)
    t = gate_thresholds(d, Criteria(), None)
    assert (t.max_error_rate, t.min_reviewed_cases) == (0.05, 7)


def test_a_rubric_without_pass_score_uses_the_configured_default():
    from skillhub.evalspec import gate_thresholds

    rubric = Rubric.model_validate_json(_rubric().model_dump_json(exclude={"pass_score"}))
    assert gate_thresholds(_defaults(pass_score=0.85), None, rubric).pass_score == 0.85
    assert gate_thresholds(_defaults(pass_score=0.6), None, rubric).pass_score == 0.6  # not the model's 0.7


@pytest.mark.parametrize(
    "criteria", [{"fail_rate": -0.1}, {"warn_rate": 1.5}, {"fail_rate": 0.8, "warn_rate": 0.5}]
)
def test_out_of_range_or_inverted_criteria_are_invalid(criteria):
    with pytest.raises(ValidationError):
        EvalFile.model_validate({"skill_name": "x", "criteria": criteria, "evals": []})


@pytest.mark.parametrize("score", [0, -1, 1.01])
def test_pass_score_must_be_in_0_1(score):
    with pytest.raises(ValidationError):
        _rubric(pass_score=score)


def test_duplicate_case_ids_are_a_load_error(tmp_path):
    p = tmp_path / "evals.json"
    p.write_text(
        '{"skill_name": "x", "evals": [{"id": 3, "prompt": "a", "should_trigger": true},'
        '{"id": 3, "prompt": "b", "should_trigger": false}]}'
    )
    with pytest.raises(ValidationError, match="repeated: 3"):
        load_evals(p)


def test_an_empty_expected_skill_means_none():
    """Dataset rows carry "" because the MLflow dataset API rejects nulls."""
    case = EvalCase.model_validate({"id": 1, "prompt": "p", "should_trigger": False, "expected_skill": ""})
    assert case.expected_skill is None and case.resolved_expected("x") is None
    near = EvalCase(id=2, prompt="p", should_trigger=False, expected_skill="morning-note")
    assert near.expected_skill == "morning-note"


# --- Eval-input digests ---------------------------------------------------------------------------------


def _skill(tmp_path, cases, rubric=None):
    from skillhub.evalspec import EvalFile, save_evals, save_rubric

    root = tmp_path / "skills" / "common" / "s"
    (root / "evals").mkdir(parents=True)
    save_evals(root / "evals" / "evals.json", EvalFile(skill_name="s", evals=cases))
    if rubric is not None:
        save_rubric(root / "evals" / "rubric.json", rubric)
    return root


def _fixtures(tmp_path, files):
    root = tmp_path / "fixtures"
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    return root


def _case(i, fixtures=(), kind="trigger"):
    from skillhub.evalspec import EvalCase

    return EvalCase(id=i, kind=kind, prompt=f"p{i}", should_trigger=True, fixtures=list(fixtures))


def test_fixture_paths_cover_the_cases_fixtures_the_search_indexes_and_the_planted_manifest():
    from skillhub.evalspec import PLANTED_FIXTURE, PLANTED_MANIFEST, EvalFile, fixture_paths

    ef = EvalFile(
        skill_name="s", evals=[_case(1), _case(101, ["a/doc.md", PLANTED_FIXTURE], "effectiveness")]
    )
    assert fixture_paths(ef, ["web_index.json", "a/web_index.json"]) == sorted(
        ["a/doc.md", PLANTED_FIXTURE, PLANTED_MANIFEST, "web_index.json", "a/web_index.json"]
    )
    assert fixture_paths(EvalFile(skill_name="s", evals=[_case(1)]), ["web_index.json"]) == []


def test_dataset_digest_changes_with_evals_json_and_every_fixture_byte(tmp_path):
    from skillhub.evalspec import eval_digests

    skill = _skill(tmp_path, [_case(101, ["n/doc.md"], "effectiveness")])
    fx = _fixtures(tmp_path, {"n/doc.md": b"v1", "n/web_index.json": b"{}", "other.md": b"x"})
    base = eval_digests(skill, fx)
    assert base.dataset_digest.startswith("sha256:") and base == eval_digests(skill, fx)
    (fx / "other.md").write_bytes(b"not used by any case")
    assert eval_digests(skill, fx) == base
    (fx / "n" / "doc.md").write_bytes(b"v2")
    assert eval_digests(skill, fx).dataset_digest != base.dataset_digest
    (fx / "n" / "doc.md").write_bytes(b"v1")
    (fx / "n" / "web_index.json").write_bytes(b'{"keywords": {}}')
    assert eval_digests(skill, fx).dataset_digest != base.dataset_digest
    (fx / "n" / "web_index.json").write_bytes(b"{}")
    (fx / "n" / "doc.md").unlink()
    assert eval_digests(skill, fx).dataset_digest != base.dataset_digest  # a missing fixture counts
    (fx / "n" / "doc.md").write_bytes(b"v1")
    path = skill / "evals" / "evals.json"
    path.write_bytes(path.read_bytes().replace(b'"p101"', b'"another prompt"'))
    assert eval_digests(skill, fx).dataset_digest != base.dataset_digest


def test_a_fixture_outside_the_root_is_digested_as_missing(tmp_path):
    from skillhub.evalspec import read_fixture

    fx = _fixtures(tmp_path, {"in.md": b"x"})
    (tmp_path / "secret.md").write_bytes(b"s")
    assert read_fixture(fx, "in.md") == b"x"
    assert read_fixture(fx, "../secret.md") is None and read_fixture(fx, "nope.md") is None


def test_rubric_hash_is_none_without_rubric_json_and_tracks_the_rubric(tmp_path):
    from skillhub.evalspec import NO_RUBRIC, Criterion, Rubric, eval_digests, rubric_hash

    fx = _fixtures(tmp_path, {})
    assert eval_digests(_skill(tmp_path / "a", [_case(1)]), fx).rubric_hash == NO_RUBRIC
    r = Rubric(
        skill_name="s",
        source_hash="sha256:x",
        generated_by="gpt",
        criteria=[
            Criterion(id=f"c{i}", text="t", type="task_quality", weight=1, source="SKILL.md")
            for i in range(5)
        ],
    )
    assert eval_digests(_skill(tmp_path / "b", [_case(1)], r), fx).rubric_hash == rubric_hash(r)


def test_eval_digests_fail_on_a_broken_evals_json(tmp_path):
    from skillhub.evalspec import eval_digests

    root = tmp_path / "s"
    (root / "evals").mkdir(parents=True)
    (root / "evals" / "evals.json").write_text("{not json")
    with pytest.raises(ValueError):
        eval_digests(root, tmp_path)
    with pytest.raises(OSError):
        eval_digests(tmp_path / "missing", tmp_path)


# --- The planned matrix ---


def test_planned_cells_resolve_models_and_modes_like_the_runner():
    from skillhub.evalspec import EvalFile, planned_cells

    both = EvalFile(skill_name="s", evals=[_case(1), _case(101, kind="effectiveness")])
    trig = EvalFile(skill_name="s", evals=[_case(1)], models=["gpt"])
    assert planned_cells(both, default_models=["a", "b"]) == [
        "a/trigger",
        "a/with_skill",
        "b/trigger",
        "b/with_skill",
    ]
    assert planned_cells(trig, default_models=["a"]) == ["gpt/trigger"]  # evals.json models beat the defaults
    assert planned_cells(trig, request_models=["x"], default_models=["a"]) == [
        "x/trigger"
    ]  # the request wins
    assert planned_cells(both, default_models=["a"], modes=["effectiveness"]) == ["a/with_skill"]
    assert planned_cells(EvalFile(skill_name="s", evals=[]), default_models=["a"]) == []


def _asserted(i, should_trigger=True):
    return EvalCase(id=i, prompt=f"p{i}", should_trigger=should_trigger, assertions=["In German"])


def test_assertion_cases_plan_the_effectiveness_modes_and_their_gating_cell():
    from skillhub.evalspec import REQUEST_MODES, planned_cells, run_modes

    asserted = EvalFile(skill_name="s", evals=[_case(1), _asserted(2)])
    assert run_modes(asserted, REQUEST_MODES) == ["trigger", "without_skill", "with_skill"]
    assert planned_cells(asserted, default_models=["a"]) == ["a/trigger", "a/with_skill"]
    ignored = EvalFile(skill_name="s", evals=[_case(1), _asserted(2, should_trigger=False)])
    assert run_modes(ignored, REQUEST_MODES) == ["trigger"]
    assert planned_cells(ignored, default_models=["a"]) == ["a/trigger"]


def test_only_a_skill_whose_effectiveness_cases_are_all_assertion_cases_needs_no_rubric():
    from skillhub.evalspec import rubric_optional

    assert rubric_optional(EvalFile(skill_name="s", evals=[_case(1), _asserted(2)]))
    assert not rubric_optional(EvalFile(skill_name="s", evals=[_case(1)]))  # no effectiveness to run
    assert not rubric_optional(
        EvalFile(skill_name="s", evals=[_asserted(2), _case(101, kind="effectiveness")])
    )
    assert not rubric_optional(EvalFile(skill_name="s", evals=[_asserted(2, should_trigger=False)]))
