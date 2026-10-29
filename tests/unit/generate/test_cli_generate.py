from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from skillhub import cli
from skillhub.evalspec import (
    Criterion,
    EvalCase,
    EvalFile,
    Rubric,
    load_evals,
    load_rubric,
    save_evals,
    save_rubric,
)
from skillhub.generate.tests import GenerationError

runner = CliRunner()
FM = "---\nname: {name}\ndescription: {desc}\n---\nbody"


def _rubric_obj(name, source_hash="sha256:old"):
    return Rubric(
        skill_name=name,
        source_hash=source_hash,
        generated_by="claude-opus",
        pass_score=0.7,
        reviewed=True,
        criteria=[
            Criterion(id=f"c{i}", text="t", type="task_quality", weight=1, source="SKILL.md")
            for i in range(1, 6)
        ],
    )


def _skill(root, name, evals=None, rubric=False, dept="common"):
    d = root / "skills" / dept / name
    (d / "evals").mkdir(parents=True)
    (d / "SKILL.md").write_text(FM.format(name=name, desc=f"{name} skill"))
    if evals is not None:
        save_evals(d / "evals" / "evals.json", EvalFile(skill_name=name, evals=evals))
    if rubric:
        save_rubric(d / "evals" / "rubric.json", _rubric_obj(name))
    return d


def _trigger(i, origin="human"):
    return EvalCase(
        id=i, prompt=f"{origin} {i}", should_trigger=True, origin=origin, reviewed=origin == "human"
    )


def _effectiveness(i):
    return EvalCase(id=i, kind="effectiveness", prompt=f"eff {i}", should_trigger=True, origin="human")


@pytest.fixture
def calls():
    return SimpleNamespace(cases=[], rubrics=[])


def _invoke(monkeypatch, tmp_path, calls, args, fail=(), n_cases=2):
    def _cases(cfg, b, sib, model, start_id=1):
        if b.name in fail:
            raise GenerationError("bad output")
        calls.cases.append((b.name, start_id))
        return [
            EvalCase(
                id=start_id + i, prompt=f"gen {i}", should_trigger=True, origin="generated", reviewed=False
            )
            for i in range(n_cases)
        ]

    def _rubric(cfg, b, model):
        calls.rubrics.append(b.name)
        return _rubric_obj(b.name, source_hash="sha256:x")

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SKILLHUB_SKILLS", raising=False)
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: SimpleNamespace(generator="claude-opus"))
    monkeypatch.setattr(cli, "_generation_model", lambda cfg: None)
    monkeypatch.setattr("skillhub.generate.tests.generate_trigger_cases", _cases)
    monkeypatch.setattr("skillhub.generate.rubric.generate_rubric", _rubric)
    return runner.invoke(cli.app, ["generate", *args])


def _ids(d):
    return [c.id for c in load_evals(d / "evals" / "evals.json").evals]


def test_missing_selects_skills_without_trigger_cases_or_rubric(tmp_path, monkeypatch, calls):
    fresh = _skill(tmp_path, "audit-xls")
    eff_only = _skill(tmp_path, "morning-note", evals=[_effectiveness(101)], rubric=True)
    no_rubric = _skill(tmp_path, "sales-memo", evals=[_trigger(1), _trigger(2), _trigger(3)])
    complete = _skill(tmp_path, "xlsx-author", evals=[_trigger(1)], rubric=True)
    result = _invoke(monkeypatch, tmp_path, calls, ["--missing"])
    assert result.exit_code == 0, result.output
    assert "xlsx-author" not in result.output
    assert _ids(fresh) == [1, 2]
    assert _ids(eff_only) == [101, 1, 2]
    assert _ids(no_rubric) == [1, 2, 3]
    assert load_rubric(no_rubric / "evals" / "rubric.json").source_hash == "sha256:x"
    assert load_rubric(eff_only / "evals" / "rubric.json").source_hash == "sha256:old"
    assert _ids(complete) == [1]


def test_existing_rubric_is_not_overwritten_without_force(tmp_path, monkeypatch, calls):
    d = _skill(tmp_path, "xlsx-author", evals=[_trigger(1)], rubric=True)
    assert _invoke(monkeypatch, tmp_path, calls, ["--skills", "xlsx-author"]).exit_code == 0
    assert load_rubric(d / "evals" / "rubric.json").source_hash == "sha256:old"
    assert _ids(d) == [1, 2, 3]


def test_skills_merge_keeps_human_and_effectiveness_cases(tmp_path, monkeypatch, calls):
    d = _skill(tmp_path, "xlsx-author", evals=[_trigger(1), _effectiveness(101)], rubric=True)
    assert _invoke(monkeypatch, tmp_path, calls, ["--skills", "xlsx-author"]).exit_code == 0
    cases = load_evals(d / "evals" / "evals.json").evals
    assert [c.id for c in cases] == [1, 101, 2, 3]
    assert cases[0].origin == "human" and cases[1].kind == "effectiveness"


def test_force_replaces_generated_cases_and_rubric_but_keeps_human(tmp_path, monkeypatch, calls):
    d = _skill(
        tmp_path,
        "xlsx-author",
        evals=[_trigger(1), _trigger(2, "generated"), _trigger(3, "generated"), _effectiveness(101)],
        rubric=True,
    )
    assert _invoke(monkeypatch, tmp_path, calls, ["--skills", "xlsx-author", "--force"]).exit_code == 0
    cases = load_evals(d / "evals" / "evals.json").evals
    assert [(c.id, c.origin) for c in cases] == [
        (1, "human"),
        (101, "human"),
        (2, "generated"),
        (3, "generated"),
    ]
    assert [c.prompt for c in cases[:2]] == ["human 1", "eff 101"]
    assert load_rubric(d / "evals" / "rubric.json").source_hash == "sha256:x"


def test_rubric_only_never_touches_evals_and_writes_missing_rubric(tmp_path, monkeypatch, calls):
    d = _skill(tmp_path, "sales-memo", evals=[_trigger(1)])
    before = (d / "evals" / "evals.json").read_bytes()
    assert _invoke(monkeypatch, tmp_path, calls, ["--rubric-only", "--skills", "sales-memo"]).exit_code == 0
    assert (d / "evals" / "evals.json").read_bytes() == before
    assert calls.cases == [] and calls.rubrics == ["sales-memo"]
    assert load_rubric(d / "evals" / "rubric.json").source_hash == "sha256:x"


def test_rubric_only_without_evals_file_does_not_create_one(tmp_path, monkeypatch, calls):
    d = _skill(tmp_path, "sales-memo")
    assert _invoke(monkeypatch, tmp_path, calls, ["--rubric-only", "--skills", "sales-memo"]).exit_code == 0
    assert not (d / "evals" / "evals.json").exists()


def test_rubric_only_keeps_existing_rubric_unless_forced(tmp_path, monkeypatch, calls):
    d = _skill(tmp_path, "sales-memo", evals=[_trigger(1)], rubric=True)
    assert _invoke(monkeypatch, tmp_path, calls, ["--rubric-only", "--skills", "sales-memo"]).exit_code == 0
    assert load_rubric(d / "evals" / "rubric.json").source_hash == "sha256:old"
    assert (
        _invoke(
            monkeypatch, tmp_path, calls, ["--rubric-only", "--force", "--skills", "sales-memo"]
        ).exit_code
        == 0
    )
    assert load_rubric(d / "evals" / "rubric.json").source_hash == "sha256:x"
    assert _ids(d) == [1]


def test_rubric_only_with_missing_selects_only_skills_without_rubric(tmp_path, monkeypatch, calls):
    a = _skill(tmp_path, "a-skill")
    b = _skill(tmp_path, "b-skill", evals=[_trigger(1)], rubric=True)
    assert _invoke(monkeypatch, tmp_path, calls, ["--rubric-only", "--missing"]).exit_code == 0
    assert calls.rubrics == ["a-skill"] and calls.cases == []
    assert not (a / "evals" / "evals.json").exists() and _ids(b) == [1]


def test_dept_name_and_bare_name_are_accepted(tmp_path, monkeypatch, calls):
    _skill(tmp_path, "morning-note", dept="equity_research")
    _skill(tmp_path, "audit-xls")
    r = _invoke(monkeypatch, tmp_path, calls, ["--skills", "equity_research/morning-note,audit-xls"])
    assert r.exit_code == 0, r.output
    assert sorted(n for n, _ in calls.cases) == ["audit-xls", "morning-note"]


def test_bare_name_in_two_departments_exits_2_and_dept_name_disambiguates(tmp_path, monkeypatch, calls):
    _skill(tmp_path, "memo", dept="equity_research")
    other = _skill(tmp_path, "memo", dept="investment_banking")
    r = _invoke(monkeypatch, tmp_path, calls, ["--skills", "memo"])
    assert r.exit_code == 2 and calls.cases == []
    assert _invoke(monkeypatch, tmp_path, calls, ["--skills", "investment_banking/memo"]).exit_code == 0
    assert calls.cases == [("memo", 1)] and _ids(other) == [1, 2]


def test_unknown_skill_exits_2(tmp_path, monkeypatch, calls):
    _skill(tmp_path, "audit-xls")
    assert _invoke(monkeypatch, tmp_path, calls, ["--skills", "nope"]).exit_code == 2


def test_empty_skills_string_means_unset(tmp_path, monkeypatch, calls):
    _skill(tmp_path, "xlsx-author", evals=[_trigger(1)], rubric=True)
    assert _invoke(monkeypatch, tmp_path, calls, ["--skills", "", "--missing"]).exit_code == 0
    assert calls.cases == []


def test_missing_and_skills_are_mutually_exclusive(tmp_path, monkeypatch, calls):
    _skill(tmp_path, "xlsx-author", evals=[_trigger(1)], rubric=True)
    assert _invoke(monkeypatch, tmp_path, calls, ["--skills", "xlsx-author", "--missing"]).exit_code != 0


def test_generation_failure_writes_nothing_for_that_skill_and_continues(tmp_path, monkeypatch, calls):
    bad = _skill(tmp_path, "bad-skill", evals=[_trigger(1)])
    good = _skill(tmp_path, "good-skill")
    r = _invoke(monkeypatch, tmp_path, calls, ["--skills", "bad-skill,good-skill"], fail=("bad-skill",))
    assert r.exit_code == 1
    assert _ids(bad) == [1] and not (bad / "evals" / "rubric.json").exists()
    assert _ids(good) == [1, 2]


def test_trigger_ids_reaching_the_effectiveness_range_fail_that_skill(tmp_path, monkeypatch, calls):
    d = _skill(tmp_path, "xlsx-author", evals=[_trigger(95)])  # next ids 96, 97 are fine
    assert _invoke(monkeypatch, tmp_path, calls, ["--skills", "xlsx-author"]).exit_code == 0
    assert _ids(d) == [95, 96, 97]
    d2 = _skill(tmp_path, "tight", evals=[_trigger(100)])  # next id 101 is the effectiveness range
    r = _invoke(monkeypatch, tmp_path, calls, ["--skills", "tight"])
    assert r.exit_code == 1 and _ids(d2) == [100]


def test_a_full_batch_of_15_fits_only_below_the_effectiveness_range(tmp_path, monkeypatch, calls):
    d = _skill(tmp_path, "xlsx-author", evals=[_trigger(85)])  # 86..100 is the last batch that fits
    assert _invoke(monkeypatch, tmp_path, calls, ["--skills", "xlsx-author"], n_cases=15).exit_code == 0
    assert _ids(d) == list(range(85, 101))
    d2 = _skill(tmp_path, "tight", evals=[_trigger(86)])  # 87..101 would reach the effectiveness range
    r = _invoke(monkeypatch, tmp_path, calls, ["--skills", "tight"], n_cases=15)
    assert r.exit_code == 1 and _ids(d2) == [86]


def test_id_collision_with_kept_case_fails_that_skill(tmp_path, monkeypatch, calls):
    d = _skill(tmp_path, "xlsx-author", evals=[_trigger(1)])
    monkeypatch.setattr(
        "skillhub.generate.tests.generate_trigger_cases",
        lambda *a, **k: [EvalCase(id=1, prompt="x", should_trigger=True, origin="generated", reviewed=False)],
    )
    # _invoke re-patches, so call the CLI directly
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: SimpleNamespace(generator="g"))
    monkeypatch.setattr(cli, "_generation_model", lambda cfg: None)
    r = runner.invoke(cli.app, ["generate", "--skills", "xlsx-author"])
    assert r.exit_code == 1 and "collide" in r.output and _ids(d) == [1]


def test_malformed_existing_evals_json_fails_that_skill_only(tmp_path, monkeypatch, calls):
    bad = _skill(tmp_path, "bad-skill")
    (bad / "evals" / "evals.json").write_text("{not json")
    good = _skill(tmp_path, "good-skill")
    r = _invoke(monkeypatch, tmp_path, calls, ["--skills", "bad-skill,good-skill"])
    assert r.exit_code == 1 and _ids(good) == [1, 2]
    assert (bad / "evals" / "evals.json").read_text() == "{not json"


def test_model_transport_error_fails_that_skill_only(tmp_path, monkeypatch, calls):
    a = _skill(tmp_path, "a-skill")
    b = _skill(tmp_path, "b-skill")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: SimpleNamespace(generator="g"))
    monkeypatch.setattr(cli, "_generation_model", lambda cfg: None)

    def gen(cfg, bundle, sib, model, start_id=1):
        if bundle.name == "a-skill":
            raise ConnectionError("endpoint down")
        return [EvalCase(id=start_id, prompt="x", should_trigger=True, origin="generated", reviewed=False)]

    monkeypatch.setattr("skillhub.generate.tests.generate_trigger_cases", gen)
    monkeypatch.setattr("skillhub.generate.rubric.generate_rubric", lambda c, bb, m: _rubric_obj(bb.name))
    r = runner.invoke(cli.app, ["generate", "--skills", "a-skill,b-skill"])
    assert r.exit_code == 1 and "endpoint down" in r.output
    assert not (a / "evals" / "evals.json").exists() and _ids(b) == [1]


@pytest.mark.parametrize("value", [",", " , ", "  "])
def test_skills_naming_nothing_exits_2(tmp_path, monkeypatch, calls, value):
    _skill(tmp_path, "audit-xls")
    r = _invoke(monkeypatch, tmp_path, calls, ["--skills", value])
    assert r.exit_code == 2
