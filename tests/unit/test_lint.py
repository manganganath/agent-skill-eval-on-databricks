import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from skillhub.gateway.skills_mcp import MAX_SKILL_TOOLS
from skillhub.lint import MAX_BUNDLE_BYTES, LintRefused, assert_clean, lint_repo


def _messages(issues):
    return [i.message for i in issues]


def test_valid_tree_has_no_issues(make_bundle, tmp_path):
    make_bundle("common", "audit-xls")
    make_bundle("equity_research", "earnings-preview")
    assert lint_repo(tmp_path / "skills") == []


def test_name_must_match_pattern_and_equal_folder(make_bundle, tmp_path):
    make_bundle("common", "Bad_Name")
    msgs = _messages(lint_repo(tmp_path / "skills"))
    assert any("must match" in m for m in msgs)


def test_frontmatter_name_must_equal_folder(make_bundle, tmp_path):
    b = make_bundle("common", "good-name")
    (b.root / "SKILL.md").write_text("---\nname: other-name\ndescription: d\n---\nbody\n")
    assert any("must equal folder" in m for m in _messages(lint_repo(tmp_path / "skills")))


def test_empty_description_is_an_issue(make_bundle, tmp_path):
    make_bundle("common", "quiet", description="   ")
    assert any("description is empty" in m for m in _messages(lint_repo(tmp_path / "skills")))


def test_names_must_be_unique_across_departments(make_bundle, tmp_path):
    make_bundle("common", "dup-skill")
    make_bundle("operations", "dup-skill")
    msgs = _messages(lint_repo(tmp_path / "skills"))
    assert sum("also used in another department" in m for m in msgs) == 2


def test_a_department_folder_holds_at_most_the_gateway_connection_cap(make_bundle, tmp_path):
    for i in range(MAX_SKILL_TOOLS):
        make_bundle("equity_research", f"skill-{i:02d}")
    assert lint_repo(tmp_path / "skills") == []
    make_bundle("equity_research", "skill-extra")
    msgs = _messages(lint_repo(tmp_path / "skills"))
    assert any("holds 21 skills" in m and "the limit is 20" in m for m in msgs), msgs


def test_bundle_size_limit(make_bundle, tmp_path):
    make_bundle("common", "huge", files={"big.txt": "x" * (MAX_BUNDLE_BYTES + 1)})
    assert any("limit is" in m for m in _messages(lint_repo(tmp_path / "skills")))


def test_invalid_evals_json_is_reported(make_bundle, tmp_path):
    b = make_bundle("common", "bad-evals")
    (b.root / "evals").mkdir()
    (b.root / "evals" / "evals.json").write_text(
        json.dumps({"skill_name": "bad-evals", "evals": [{"id": "x"}]})
    )
    assert any(m.startswith("evals/evals.json") for m in _messages(lint_repo(tmp_path / "skills")))


def test_near_miss_must_point_at_a_real_sibling(make_bundle, tmp_path):
    b = make_bundle("equity_research", "earnings-preview")
    (b.root / "evals").mkdir()
    case = {"id": 1, "prompt": "p", "should_trigger": False, "expected_skill": "no-such-skill"}
    (b.root / "evals" / "evals.json").write_text(
        json.dumps({"skill_name": "earnings-preview", "evals": [case]})
    )
    assert any("not a skill in the repo" in m for m in _messages(lint_repo(tmp_path / "skills")))


def test_paths_filter_limits_reported_bundles(make_bundle, tmp_path):
    make_bundle("common", "Bad_Name")
    make_bundle("common", "fine-name")
    issues = lint_repo(
        tmp_path / "skills", paths=[str(tmp_path / "skills" / "common" / "fine-name" / "SKILL.md")]
    )
    assert issues == []


def test_lint_runs_without_config_or_env(make_bundle, tmp_path, monkeypatch):
    monkeypatch.delenv("DATABRICKS_HOST", raising=False)
    make_bundle("common", "audit-xls")
    assert lint_repo(tmp_path / "skills") == []


def test_cli_lint_exit_codes(make_bundle, tmp_path, monkeypatch):
    from skillhub.cli import app

    monkeypatch.chdir(tmp_path)
    make_bundle("common", "Bad_Name")  # writes under tmp_path/skills
    r = CliRunner().invoke(app, ["lint"])
    assert r.exit_code == 1
    assert "must match" in r.output


def test_malformed_frontmatter_is_reported_not_raised(make_bundle, tmp_path):
    b = make_bundle("common", "broken")
    (b.root / "SKILL.md").write_text("---\nname: broken\ndescription: [unclosed\n---\nbody\n")
    assert any("not valid YAML" in m for m in _messages(lint_repo(tmp_path / "skills")))


def test_cli_lint_uses_skillhub_skills_env(make_bundle, tmp_path, monkeypatch):
    from skillhub.cli import app

    make_bundle("common", "Bad_Name")
    monkeypatch.chdir(tmp_path.parent)
    monkeypatch.setenv("SKILLHUB_SKILLS", str(tmp_path / "skills"))
    assert CliRunner().invoke(app, ["lint"]).exit_code == 1


def _crit(n=5):
    return [
        {"id": f"c{i}", "text": "t", "type": "task_quality", "weight": 1, "source": "s"}
        for i in range(1, n + 1)
    ]


def _rubric(name, criteria=None):
    return json.dumps(
        {"skill_name": name, "source_hash": "h", "generated_by": "m", "criteria": criteria or _crit()}
    )


def _write_evals(b, data, fname="evals.json"):
    (b.root / "evals").mkdir(exist_ok=True)
    (b.root / "evals" / fname).write_text(data if isinstance(data, str) else json.dumps(data))


def test_relative_path_after_chdir_selects_bundle(make_bundle, tmp_path, monkeypatch):
    make_bundle("common", "Bad_Name")
    make_bundle("common", "fine-name")
    monkeypatch.chdir(tmp_path)
    issues = lint_repo(Path("skills"), paths=["skills/common/Bad_Name/SKILL.md"])
    assert any("must match" in m for m in _messages(issues))
    assert lint_repo(Path("skills"), paths=["skills/common/fine-name"]) == []


def test_path_with_dotdot_selects_bundle(make_bundle, tmp_path):
    make_bundle("common", "Bad_Name")
    p = tmp_path / "skills" / "common" / "other" / ".." / "Bad_Name" / "SKILL.md"
    assert any("must match" in m for m in _messages(lint_repo(tmp_path / "skills", paths=[str(p)])))


def test_path_matching_no_bundle_is_an_issue(make_bundle, tmp_path):
    make_bundle("common", "fine-name")
    p = tmp_path / "skills" / "common" / "ghost" / "SKILL.md"
    assert any("unknown_path" in m for m in _messages(lint_repo(tmp_path / "skills", paths=[str(p)])))


def test_path_outside_skills_root_is_an_issue(make_bundle, tmp_path):
    make_bundle("common", "fine-name")
    issues = lint_repo(tmp_path / "skills", paths=[str(tmp_path / "README.md")])
    assert any("outside_root" in m and str((tmp_path / "skills").resolve()) in m for m in _messages(issues))


def test_missing_skills_root_is_an_issue(tmp_path):
    assert any("missing_root" in m for m in _messages(lint_repo(tmp_path / "nope")))


def test_cli_lint_missing_root_exits_1(tmp_path, monkeypatch):
    from skillhub.cli import app

    monkeypatch.setenv("SKILLHUB_SKILLS", str(tmp_path / "nope"))
    r = CliRunner().invoke(app, ["lint"])
    assert r.exit_code == 1
    assert "missing_root" in r.output


def test_load_failures_reported_even_when_paths_filter(make_bundle, tmp_path):
    make_bundle("common", "fine-name")
    (tmp_path / "skills" / "common" / "empty-dir").mkdir()
    msgs = _messages(lint_repo(tmp_path / "skills", paths=[str(tmp_path / "skills/common/fine-name")]))
    assert any("missing SKILL.md" in m for m in msgs)


def test_evals_skill_name_must_equal_folder(make_bundle, tmp_path):
    b = make_bundle("common", "alpha-skill")
    _write_evals(b, {"skill_name": "other", "evals": []})
    assert any(
        "skill_name must equal the folder name" in m for m in _messages(lint_repo(tmp_path / "skills"))
    )


def test_rubric_skill_name_mismatch(make_bundle, tmp_path):
    b = make_bundle("common", "alpha-skill")
    _write_evals(b, _rubric("other"), "rubric.json")
    assert any("rubric skill_name must equal" in m for m in _messages(lint_repo(tmp_path / "skills")))


def test_rubric_duplicate_criterion_ids(make_bundle, tmp_path):
    b = make_bundle("common", "alpha-skill")
    crit = _crit()
    crit[1]["id"] = "c1"
    _write_evals(b, _rubric("alpha-skill", crit), "rubric.json")
    assert any("criterion ids must be unique" in m for m in _messages(lint_repo(tmp_path / "skills")))


def test_rubric_schema_failure(make_bundle, tmp_path):
    b = make_bundle("common", "alpha-skill")
    _write_evals(b, _rubric("alpha-skill", _crit(2)), "rubric.json")
    assert any(m.startswith("evals/rubric.json") for m in _messages(lint_repo(tmp_path / "skills")))


def test_assert_clean_raises_lint_refused(make_bundle):
    b = make_bundle("common", "Bad_Name")
    with pytest.raises(LintRefused, match="must match"):
        assert_clean(b)


def test_assert_clean_passes_on_clean_bundle(make_bundle):
    assert_clean(make_bundle("common", "fine-name"))


def test_negative_case_cannot_expect_its_own_skill(make_bundle, tmp_path):
    b = make_bundle("common", "alpha-skill")
    case = {"id": 1, "prompt": "p", "should_trigger": False, "expected_skill": "alpha-skill"}
    _write_evals(b, {"skill_name": "alpha-skill", "evals": [case]})
    assert any("cannot expect its own skill" in m for m in _messages(lint_repo(tmp_path / "skills")))


def test_cli_lint_exit_zero_on_clean_tree_and_scans_bundles(make_bundle, tmp_path, monkeypatch):
    from skillhub.cli import app

    monkeypatch.chdir(tmp_path.parent)  # prove the env var, not the cwd, locates the tree
    monkeypatch.setenv("SKILLHUB_SKILLS", str(tmp_path / "skills"))
    make_bundle("common", "fine-name")
    r = CliRunner().invoke(app, ["lint"])
    assert r.exit_code == 0
    assert "0 issue(s)" in r.output
    # Same setup with a bad bundle added: the tree really is scanned.
    make_bundle("common", "Bad_Name")
    r = CliRunner().invoke(app, ["lint"])
    assert r.exit_code == 1
    assert "Bad_Name" in r.output


def _evals(b, cases, **extra):
    (b.root / "evals").mkdir(exist_ok=True)
    (b.root / "evals" / "evals.json").write_text(json.dumps({"skill_name": b.name, "evals": cases, **extra}))


def _case(i, kind="trigger"):
    return {"id": i, "kind": kind, "prompt": "p", "should_trigger": True}


def test_duplicate_case_ids_are_reported(make_bundle, tmp_path):
    _evals(make_bundle("common", "dups"), [_case(4), _case(4)])
    assert any("repeated: 4" in m for m in _messages(lint_repo(tmp_path / "skills")))


def test_case_ids_stay_in_their_ranges(make_bundle, tmp_path):
    cases = [_case(101), _case(100, "effectiveness"), _case(1), _case(102, "effectiveness")]
    _evals(make_bundle("common", "ranges"), cases)
    msgs = [m for m in _messages(lint_repo(tmp_path / "skills")) if " case " in m]
    assert msgs == [
        "evals/evals.json: trigger case 101: trigger ids are 1-100",
        "evals/evals.json: effectiveness case 100: ids start at 101",
    ]


def test_department_folder_must_be_a_config_key(make_bundle, tmp_path):
    make_bundle("common", "fine")
    make_bundle("rogue_dept", "sneaky")
    issues = lint_repo(tmp_path / "skills", departments={"common", "operations"})
    assert [i.message for i in issues] == ["department folder 'rogue_dept' is not a key in hub.yaml"]
    assert lint_repo(tmp_path / "skills") == []  # without config the check is skipped


def test_loosening_thresholds_are_reported(make_bundle, tmp_path):
    from skillhub.config import Defaults

    b = make_bundle("common", "loose")
    _evals(b, [_case(1)], criteria={"fail_rate": 0.0, "warn_rate": 0.9})
    msgs = _messages(lint_repo(tmp_path / "skills", defaults=Defaults(models=["gpt"])))
    assert msgs == [
        "criteria.fail_rate 0.0 is below defaults.fail_rate 0.5: a skill may only tighten the gate"
    ]


def test_cli_lint_checks_against_the_repository_config(make_bundle, tmp_path, monkeypatch):
    from skillhub.cli import app

    monkeypatch.setenv("SKILLHUB_SKILLS", str(tmp_path / "skills"))
    b = make_bundle("common", "loose")
    _evals(b, [_case(1)], criteria={"warn_rate": 0.1})
    make_bundle("not_a_dept", "x")
    for k in ("DATABRICKS_HOST", "SQL_WAREHOUSE_ID", "SKILLHUB_CATALOG"):
        monkeypatch.delenv(k, raising=False)
    config = Path(__file__).resolve().parents[2] / "config" / "hub.yaml"
    r = CliRunner().invoke(app, ["--config", str(config), "lint"])
    assert r.exit_code == 1, r.output
    assert "criteria.warn_rate 0.1 is below defaults.warn_rate 0.8" in r.output
    assert "department folder 'not_a_dept' is not a key in hub.yaml" in r.output
    r = CliRunner().invoke(app, ["--config", str(tmp_path / "missing.yaml"), "lint"])
    assert r.exit_code == 1 and "cannot read" in r.output
