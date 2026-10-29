from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from skillhub.ci.changes import ChangedSkill, GitRefError, changed_skills
from skillhub.cli import app


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
    )


def write(repo: Path, rel: str, text: str = "x") -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def commit_all(repo: Path, msg: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    git(tmp_path, "init", "-q", "-b", "main")
    write(tmp_path, "skills/common/alpha/SKILL.md", "alpha")
    write(tmp_path, "skills/common/beta/SKILL.md", "beta")
    write(tmp_path, "skills/common/beta/references/g.md", "g")
    write(tmp_path, "README.md", "readme")
    base = commit_all(tmp_path, "base")
    return tmp_path, base


def test_added_skill(repo):
    path, base = repo
    write(path, "skills/common/gamma/SKILL.md", "gamma")
    head = commit_all(path, "add gamma")
    assert changed_skills(base, head, repo=path) == [ChangedSkill("common", "gamma", "added", True, True)]


def test_evals_only_change_is_a_modification(repo):
    path, base = repo
    write(path, "skills/common/alpha/evals/evals.json", "{}")
    head = commit_all(path, "evals")
    assert changed_skills(base, head, repo=path) == [ChangedSkill("common", "alpha", "modified", True, True)]


def test_deleted_skill_folder(repo):
    path, base = repo
    git(path, "rm", "-r", "-q", "skills/common/beta")
    head = commit_all(path, "delete beta")
    assert changed_skills(base, head, repo=path) == [ChangedSkill("common", "beta", "deleted")]


def test_rename_is_deleted_plus_renamed(repo):
    path, base = repo
    git(path, "mv", "skills/common/alpha", "skills/common/alpha2")
    head = commit_all(path, "rename alpha")
    result = changed_skills(base, head, repo=path)
    assert result == [
        ChangedSkill("common", "alpha", "deleted"),
        ChangedSkill("common", "alpha2", "renamed", True, True),
    ]


def test_non_skill_files_are_ignored(repo):
    path, base = repo
    write(path, "README.md", "changed")
    head = commit_all(path, "readme")
    assert changed_skills(base, head, repo=path) == []


def test_as_dict_is_the_cli_shape(repo):
    assert ChangedSkill("common", "alpha", "deleted").as_dict() == {
        "dept": "common",
        "skill": "alpha",
        "status": "deleted",
        "needs_cases": False,
        "needs_rubric": False,
        "moved_from_quarantine": False,
        "needs_generation": False,
    }


def test_cli_prints_json_rows(repo, monkeypatch):
    path, base = repo
    write(path, "skills/common/gamma/SKILL.md", "gamma")
    head = commit_all(path, "add gamma")
    monkeypatch.chdir(path)
    result = CliRunner().invoke(app, ["changed-skills", "--base", base, "--head", head])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [
        {
            "dept": "common",
            "skill": "gamma",
            "status": "added",
            "needs_cases": True,
            "needs_rubric": True,
            "moved_from_quarantine": False,
            "needs_generation": True,
        }
    ]


def test_an_empty_base_adds_every_skill_at_head(repo, monkeypatch):
    """Before the first release nothing is released: every skill at head is added, and none is deleted."""
    path, base = repo
    git(path, "rm", "-r", "-q", "skills/common/beta")
    head = commit_all(path, "delete beta")
    assert changed_skills(None, head, repo=path) == [ChangedSkill("common", "alpha", "added", True, True)]
    monkeypatch.chdir(path)
    result = CliRunner().invoke(app, ["changed-skills", "--empty-base", "--head", base])
    assert result.exit_code == 0, result.output
    rows = [(r["skill"], r["status"]) for r in json.loads(result.stdout)]
    assert rows == [("alpha", "added"), ("beta", "added")]
    for args in ([], ["--base", base, "--empty-base"], ["--base", ""]):
        assert CliRunner().invoke(app, ["changed-skills", *args, "--head", head]).exit_code == 2


def test_option_like_ref_is_rejected(repo):
    path, base = repo
    with pytest.raises(GitRefError, match="must not start with"):
        changed_skills("--output=/tmp/x", base, repo=path)


def test_unknown_sha_gives_one_line_error(repo):
    path, base = repo
    with pytest.raises(GitRefError, match="rev-parse failed: .*"):
        changed_skills("1" * 40, base, repo=path)


def test_all_zeros_sha_is_a_clear_error(repo, monkeypatch):
    path, base = repo
    monkeypatch.chdir(path)
    result = CliRunner().invoke(app, ["changed-skills", "--base", "0" * 40, "--head", base])
    assert result.exit_code == 1
    assert result.output.startswith("error: ")
    assert "Traceback" not in result.output
    assert len(result.output.strip().splitlines()) == 1


def test_merge_base_used_for_existence(repo):
    path, base = repo
    git(path, "checkout", "-q", "-b", "pr")
    write(path, "skills/common/alpha/SKILL.md", "alpha edited")
    pr_head = commit_all(path, "edit alpha")
    git(path, "checkout", "-q", "main")
    git(path, "rm", "-r", "-q", "skills/common/alpha")
    main_head = commit_all(path, "delete alpha on main")
    assert changed_skills(main_head, pr_head, repo=path) == [
        ChangedSkill("common", "alpha", "modified", True, True)
    ]


def test_cross_department_rename(repo):
    path, base = repo
    (path / "skills/legal").mkdir()
    git(path, "mv", "skills/common/alpha", "skills/legal/alpha")
    head = commit_all(path, "move")
    assert changed_skills(base, head, repo=path) == [
        ChangedSkill("common", "alpha", "deleted"),
        ChangedSkill("legal", "alpha", "renamed", True, True),
    ]


def test_deleted_file_only_stays_modified(repo):
    path, base = repo
    git(path, "rm", "-q", "skills/common/beta/references/g.md")
    head = commit_all(path, "drop ref")
    assert changed_skills(base, head, repo=path) == [ChangedSkill("common", "beta", "modified", True, True)]


def test_deleted_evals_file_is_modified(repo):
    path, base = repo
    write(path, "skills/common/alpha/evals/evals.json", "{}")
    mid = commit_all(path, "add evals")
    git(path, "rm", "-q", "skills/common/alpha/evals/evals.json")
    head = commit_all(path, "drop evals")
    assert changed_skills(mid, head, repo=path) == [ChangedSkill("common", "alpha", "modified", True, True)]


def test_non_ascii_skill_name(repo):
    path, base = repo
    write(path, "skills/common/café/SKILL.md", "c")
    head = commit_all(path, "unicode")
    assert changed_skills(base, head, repo=path) == [ChangedSkill("common", "café", "added", True, True)]


RUBRIC = "skills/common/alpha/evals/rubric.json"
EVALS = "skills/common/alpha/evals/evals.json"


def _needs(path: Path, base: str, head: str) -> bool:
    (row,) = changed_skills(base, head, repo=path)
    return row.needs_generation


def test_needs_generation_false_when_trigger_cases_and_rubric_exist(repo):
    path, base = repo
    write(path, EVALS, json.dumps({"evals": [{"id": 1, "kind": "trigger"}]}))
    write(path, RUBRIC, "{}")
    assert _needs(path, base, commit_all(path, "complete")) is False


def test_needs_generation_true_without_rubric(repo):
    path, base = repo
    write(path, EVALS, json.dumps({"evals": [{"id": 1}]}))  # kind defaults to trigger
    assert _needs(path, base, commit_all(path, "no rubric")) is True


def test_needs_generation_true_without_trigger_cases(repo):
    path, base = repo
    write(path, EVALS, json.dumps({"evals": [{"id": 1, "kind": "effectiveness"}]}))
    write(path, RUBRIC, "{}")
    assert _needs(path, base, commit_all(path, "effectiveness only")) is True


def test_needs_generation_false_for_unparseable_evals(repo):
    path, base = repo
    write(path, EVALS, "{not json")
    write(path, RUBRIC, "{}")
    assert _needs(path, base, commit_all(path, "broken")) is False


def test_deleted_skill_never_needs_generation(repo):
    path, base = repo
    git(path, "rm", "-r", "-q", "skills/common/beta")
    assert _needs(path, base, commit_all(path, "delete")) is False


def _row(path: Path, base: str, head: str) -> ChangedSkill:
    (row,) = changed_skills(base, head, repo=path)
    return row


def test_rubric_missing_but_cases_present_needs_only_rubric(repo):
    path, base = repo
    write(path, EVALS, json.dumps({"evals": [{"id": 1}]}))
    row = _row(path, base, commit_all(path, "cases only"))
    assert (row.needs_cases, row.needs_rubric, row.needs_generation) == (False, True, True)


def test_rubric_present_but_no_cases_needs_only_cases(repo):
    path, base = repo
    write(path, RUBRIC, "{}")
    row = _row(path, base, commit_all(path, "rubric only"))
    assert (row.needs_cases, row.needs_rubric) == (True, False)


def test_non_utf8_evals_file_needs_nothing(repo):
    path, base = repo
    (path / EVALS).parent.mkdir(parents=True, exist_ok=True)
    (path / EVALS).write_bytes(b"\xff\xfe\x00bad")
    write(path, RUBRIC, "{}")
    assert _row(path, base, commit_all(path, "binary")).needs_generation is False


# --- Moving a skill out of a quarantined department ---

HUB = "departments:\n  common: {readers: [u]}\n  external: {readers: [r], quarantine: %s}\n"


@pytest.fixture
def qrepo(tmp_path: Path) -> tuple[Path, str]:
    git(tmp_path, "init", "-q", "-b", "main")
    write(tmp_path, "config/hub.yaml", HUB % "true")
    write(tmp_path, "skills/external/vendor/SKILL.md", "vendor skill\n" * 20)
    write(tmp_path, "skills/external/vendor/references/r.md", "ref\n" * 20)
    write(tmp_path, "skills/common/alpha/SKILL.md", "alpha")
    return tmp_path, commit_all(tmp_path, "base")


def _moved(result) -> dict[str, bool]:
    return {f"{c.dept}/{c.skill}": c.moved_from_quarantine for c in result}


def test_rename_out_of_quarantine_is_marked(qrepo):
    path, base = qrepo
    git(path, "mv", "skills/external/vendor", "skills/common/vendor")
    head = commit_all(path, "promote vendor")
    assert _moved(changed_skills(base, head, repo=path)) == {"common/vendor": True, "external/vendor": False}


def test_rename_to_a_new_name_out_of_quarantine_is_marked(qrepo):
    path, base = qrepo
    git(path, "mv", "skills/external/vendor", "skills/common/house-style")
    head = commit_all(path, "promote under a new name")
    assert _moved(changed_skills(base, head, repo=path))["common/house-style"] is True


def test_delete_plus_add_under_the_same_name_is_marked(qrepo):
    """git can't see a rename when the content is rewritten; the same name still gives it away."""
    path, base = qrepo
    git(path, "rm", "-r", "-q", "skills/external/vendor")
    write(path, "skills/common/vendor/SKILL.md", "completely different text")
    head = commit_all(path, "rewrite and move")
    assert _moved(changed_skills(base, head, repo=path))["common/vendor"] is True


def test_clearing_the_quarantine_flag_in_the_same_change_is_still_marked(qrepo):
    path, base = qrepo
    write(path, "config/hub.yaml", HUB % "false")
    git(path, "mv", "skills/external/vendor", "skills/common/vendor")
    head = commit_all(path, "unquarantine and move")
    assert _moved(changed_skills(base, head, repo=path))["common/vendor"] is True


def test_moves_inside_or_into_quarantine_are_not_marked(qrepo):
    path, base = qrepo
    git(path, "mv", "skills/external/vendor", "skills/external/vendor2")
    git(path, "mv", "skills/common/alpha", "skills/external/alpha")
    write(path, "skills/common/vendorish/SKILL.md", "new")  # a new skill with an unrelated name
    head = commit_all(path, "shuffle")
    assert not any(_moved(changed_skills(base, head, repo=path)).values())


def test_editing_a_quarantined_skill_in_place_is_not_marked(qrepo):
    path, base = qrepo
    write(path, "skills/external/vendor/SKILL.md", "edited")
    head = commit_all(path, "edit")
    assert _moved(changed_skills(base, head, repo=path)) == {"external/vendor": False}


def test_string_quarantine_values_count(qrepo):
    path, _ = qrepo
    write(path, "config/hub.yaml", HUB % '"yes"')
    base = commit_all(path, "string flag")
    git(path, "mv", "skills/external/vendor", "skills/common/vendor")
    head = commit_all(path, "move")
    assert _moved(changed_skills(base, head, repo=path))["common/vendor"] is True


def test_unparseable_config_is_an_error(qrepo):
    path, base = qrepo
    write(path, "config/hub.yaml", "departments: [unclosed")
    head = commit_all(path, "break config")
    with pytest.raises(GitRefError, match="config/hub.yaml"):
        changed_skills(base, head, repo=path)


def test_repo_config_marks_external_as_quarantined():
    """The real config keeps `external` quarantined, so the CI check has something to protect."""
    from skillhub.ci.changes import _quarantined

    root = Path(__file__).resolve().parents[2]
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True)
    assert "external" in _quarantined(root, head.stdout.strip())
