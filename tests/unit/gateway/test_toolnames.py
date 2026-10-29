import re

import pytest

from skillhub.gateway.toolnames import ToolNameMap

PROVIDER_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


def test_dots_become_double_underscore_and_round_trip():
    m = ToolNameMap()
    s = m.register("skill_cat.dept_x.earnings-preview")
    assert s == "skill_cat__dept_x__earnings-preview"
    assert PROVIDER_RE.match(s)
    assert m.original(s) == "skill_cat.dept_x.earnings-preview"


def test_long_names_are_truncated_with_hash_and_fit_128():
    m = ToolNameMap()
    long_name = "skill_" + "a" * 200
    s = m.register(long_name)
    assert len(s) == 128
    assert PROVIDER_RE.match(s)
    assert m.original(s) == long_name


def test_collision_after_sanitising_raises():
    m = ToolNameMap()
    m.register("skill_a.b")
    with pytest.raises(ValueError):
        m.register("skill_a__b")  # sanitises to the same name, different original


def test_reregistering_same_original_is_idempotent():
    m = ToolNameMap()
    assert m.register("skill_a.b") == m.register("skill_a.b")


def test_original_unknown_name_raises_keyerror():
    with pytest.raises(KeyError):
        ToolNameMap().original("nope")


def test_known_lists_sanitised_names():
    m = ToolNameMap()
    s = m.register("skill_a.b")
    m.register("load_skill")
    assert m.known() == {s, "load_skill"}


def test_model_sees_production_fqn_only():
    m = ToolNameMap()
    m.set_fqn_rewrite({"run_pr12_ab12cd3_dept_x.earnings_preview": "skill_cat.dept_x.earnings_preview"})
    text = "Use run_pr12_ab12cd3_dept_x.earnings_preview to answer"
    out = m.to_production(text)
    assert out == "Use skill_cat.dept_x.earnings_preview to answer"
    assert "run_pr12" not in out


def test_rewrite_replaces_every_occurrence_and_longest_first():
    m = ToolNameMap()
    m.set_fqn_rewrite({"run_k_d.s": "cat.d.s", "run_k_d.s2": "cat.d.s2"})
    assert m.to_production("run_k_d.s2 and run_k_d.s") == "cat.d.s2 and cat.d.s"


def test_to_staged_reverses_rewrite_recursively_longest_first():
    m = ToolNameMap()
    m.set_fqn_rewrite({"run_k_d.s": "cat.d.s", "run_k_d.s2": "cat.d.s2"})
    args = {
        "full_name": "cat.d.s2",
        "extra": ["cat.d.s", {"n": "see cat.d.s2 and cat.d.s"}],
        "n": 3,
        "ok": True,
    }
    assert m.to_staged(args) == {
        "full_name": "run_k_d.s2",
        "extra": ["run_k_d.s", {"n": "see run_k_d.s2 and run_k_d.s"}],
        "n": 3,
        "ok": True,
    }


def test_to_staged_load_skill_call_round_trips():
    m = ToolNameMap()
    m.set_fqn_rewrite({"run_pr12_ab12cd3_dept_x.earnings_preview": "cat.dept_x.earnings_preview"})
    call = {"full_name": "cat.dept_x.earnings_preview"}
    staged = m.to_staged(call)
    assert staged == {"full_name": "run_pr12_ab12cd3_dept_x.earnings_preview"}
    assert m.to_production(staged["full_name"]) == call["full_name"]
