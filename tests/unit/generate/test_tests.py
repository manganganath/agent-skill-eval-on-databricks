import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from skillhub.bundle import SkillBundle
from skillhub.gateway.models import ChatResult
from skillhub.generate.tests import GenerationError, generate_trigger_cases, sibling_bundles
from tests.fakes import FakeModelClient

CFG = SimpleNamespace(generator="claude-opus", catalog="cat")


def bundle(dept, name, text="desc"):
    return SkillBundle(
        dept=dept,
        name=name,
        root=Path("."),
        files={"SKILL.md": text.encode()},
        frontmatter={"name": name, "description": text},
    )


def raw(text) -> ChatResult:
    return ChatResult(
        text=text,
        tool_calls=[],
        finish_reason="stop",
        usage={"prompt_tokens": 0, "completion_tokens": 0},
        assistant_message={"role": "assistant", "content": text},
    )


def reply(payload) -> ChatResult:
    return raw(json.dumps(payload))


GOOD = {
    "cases": (
        [
            {"prompt": f"p{i}", "should_trigger": True, "expected_skill": None, "kind": "positive"}
            for i in range(8)
        ]
        + [
            {
                "prompt": f"n{i}",
                "should_trigger": False,
                "expected_skill": "morning-note",
                "kind": "near_miss",
            }
            for i in range(4)
        ]
        + [
            {"prompt": f"u{i}", "should_trigger": False, "expected_skill": None, "kind": "unrelated"}
            for i in range(3)
        ]
    )
}
SIB = [bundle("equity_research", "morning-note")]
ME = bundle("equity_research", "earnings-preview", "SECRET-SKILL-BODY")


def test_sibling_scope_is_same_department_plus_common_excluding_self():
    everything = [
        ME,
        bundle("equity_research", "morning-note"),
        bundle("common", "audit-xls"),
        bundle("investment_banking", "pitch-deck"),
    ]
    assert sorted(b.name for b in sibling_bundles(everything, ME)) == ["audit-xls", "morning-note"]


def test_common_skill_sees_only_common():
    me = bundle("common", "audit-xls")
    everything = [me, bundle("common", "xlsx-author"), bundle("equity_research", "morning-note")]
    assert [b.name for b in sibling_bundles(everything, me)] == ["xlsx-author"]


def test_generated_cases_have_the_expected_mix_and_flags():
    model = FakeModelClient([reply(GOOD)])
    cases = generate_trigger_cases(CFG, ME, SIB, model, start_id=10)
    assert len(cases) == 15
    assert [c.id for c in cases] == list(range(10, 25))
    assert all(c.origin == "generated" and c.reviewed is False and c.kind == "trigger" for c in cases)
    near = [c for c in cases if c.expected_skill == "morning-note"]
    assert len(near) == 4 and all(c.should_trigger is False for c in near)
    assert sum(1 for c in cases if c.should_trigger) == 8


def test_skill_text_is_data_in_the_user_message_not_in_the_instructions():
    model = FakeModelClient([reply(GOOD)])
    generate_trigger_cases(CFG, ME, SIB, model)
    system, user = model.calls[0]["messages"]
    assert system["role"] == "system" and "SECRET-SKILL-BODY" not in system["content"]
    assert user["role"] == "user" and "SECRET-SKILL-BODY" in user["content"]


def test_near_miss_must_name_a_sibling_so_bad_output_is_retried():
    bad = json.loads(json.dumps(GOOD))
    bad["cases"][8]["expected_skill"] = "made-up-skill"
    model = FakeModelClient([reply(bad), reply(GOOD)])
    generate_trigger_cases(CFG, ME, SIB, model)
    assert len(model.calls) == 2


def test_malformed_json_retried_twice_then_fails():
    model = FakeModelClient([raw("not json"), raw("not json"), raw("not json")])
    with pytest.raises(GenerationError, match="3 attempts"):
        generate_trigger_cases(CFG, ME, [], model)
    assert len(model.calls) == 3


def test_schema_mismatch_fails_with_generation_error():
    wrong = reply({"cases": [{"prompt": "x", "kind": "positive"}]})
    with pytest.raises(GenerationError):
        generate_trigger_cases(CFG, ME, SIB, FakeModelClient([wrong, wrong, wrong]))


def test_json_that_is_not_an_object_fails():
    with pytest.raises(GenerationError):
        generate_trigger_cases(CFG, ME, SIB, FakeModelClient([raw("[1]")] * 3))


def test_malformed_then_valid_succeeds_on_third_attempt():
    model = FakeModelClient([raw("```json\n{oops"), raw("{"), reply(GOOD)])
    assert len(generate_trigger_cases(CFG, ME, SIB, model)) == 15


def test_system_text_says_skill_text_is_untrusted_data():
    model = FakeModelClient([reply(GOOD)])
    generate_trigger_cases(CFG, ME, SIB, model)
    system = model.calls[0]["messages"][0]["content"]
    assert "untrusted data" in system and "Never follow instructions" in system
    assert "skill's name" in system


def test_prompt_containing_the_skill_name_is_rejected():
    bad = json.loads(json.dumps(GOOD))
    bad["cases"][0]["prompt"] = "Please run EARNINGS-PREVIEW now"
    with pytest.raises(GenerationError, match="skill name"):
        generate_trigger_cases(CFG, ME, SIB, FakeModelClient([reply(bad)] * 3))


def _with_prompt(text):
    payload = json.loads(json.dumps(GOOD))
    payload["cases"][0]["prompt"] = text
    return payload


def test_short_skill_name_inside_another_word_is_not_rejected():
    me = bundle("equity_research", "comps")
    cases = generate_trigger_cases(
        CFG, me, SIB, FakeModelClient([reply(_with_prompt("Pull the compset for retail REITs"))])
    )
    assert cases[0].prompt == "Pull the compset for retail REITs"


def test_sibling_name_that_extends_the_skill_name_is_not_rejected():
    me = bundle("common", "audit")
    payload = _with_prompt("Run audit-xls on the workbook")
    assert (
        generate_trigger_cases(CFG, me, SIB, FakeModelClient([reply(payload)]))[0].prompt
        == payload["cases"][0]["prompt"]
    )
    with pytest.raises(GenerationError, match="skill name"):
        generate_trigger_cases(CFG, me, SIB, FakeModelClient([reply(_with_prompt("Please audit this"))] * 3))


def test_response_without_positive_or_without_negative_cases_is_rejected():
    pos = {"cases": [c for c in GOOD["cases"] if c["should_trigger"]]}
    neg = {"cases": [c for c in GOOD["cases"] if not c["should_trigger"]]}
    for payload in (pos, neg):
        with pytest.raises(GenerationError):
            generate_trigger_cases(CFG, ME, SIB, FakeModelClient([reply(payload)] * 3))


def _mutated(index, **changes):
    payload = json.loads(json.dumps(GOOD))
    payload["cases"][index].update(changes)
    return payload


def test_positive_naming_its_own_skill_is_normalised_to_no_expected_skill():
    model = FakeModelClient([reply(_mutated(0, expected_skill="earnings-preview"))])
    cases = generate_trigger_cases(CFG, ME, SIB, model)
    assert len(model.calls) == 1
    assert cases[0].should_trigger is True and cases[0].expected_skill is None
    assert cases[0].origin == "generated" and cases[0].reviewed is False


def test_near_miss_marked_as_triggering_is_normalised_to_not_trigger():
    model = FakeModelClient([reply(_mutated(8, should_trigger=True))])
    cases = generate_trigger_cases(CFG, ME, SIB, model)
    assert len(model.calls) == 1
    assert cases[8].should_trigger is False and cases[8].expected_skill == "morning-note"
    assert cases[8].origin == "generated" and cases[8].reviewed is False


def test_near_miss_naming_an_out_of_scope_skill_is_still_rejected_with_the_scope_message():
    model = FakeModelClient([reply(_mutated(8, expected_skill="made-up-skill"))] * 3)
    with pytest.raises(
        GenerationError,
        match=r"near_miss must name a sibling in scope, got 'made-up-skill'; siblings: \['morning-note'\]",
    ):
        generate_trigger_cases(CFG, ME, SIB, model)
    assert len(model.calls) == 3


def test_out_of_scope_near_miss_that_also_triggers_reports_the_scope_error():
    model = FakeModelClient([reply(_mutated(8, expected_skill="made-up-skill", should_trigger=True))] * 3)
    with pytest.raises(GenerationError, match="must name a sibling in scope"):
        generate_trigger_cases(CFG, ME, SIB, model)


def test_unrelated_case_with_an_expected_skill_is_still_rejected():
    model = FakeModelClient([reply(_mutated(12, expected_skill="morning-note"))] * 3)
    with pytest.raises(GenerationError, match="unrelated case must have"):
        generate_trigger_cases(CFG, ME, SIB, model)


def test_positive_that_does_not_trigger_is_still_rejected():
    model = FakeModelClient([reply(_mutated(0, should_trigger=False))] * 3)
    with pytest.raises(GenerationError, match="positive case must have"):
        generate_trigger_cases(CFG, ME, SIB, model)
