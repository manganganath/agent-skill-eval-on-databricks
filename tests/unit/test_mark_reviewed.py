# tests/unit/test_mark_reviewed.py
import importlib.util
from pathlib import Path

import pytest

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

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("mark_reviewed", ROOT / "scripts" / "mark_reviewed.py")
mark = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mark)


def _case(i: int, should: bool = True) -> EvalCase:
    return EvalCase(id=i, prompt=f"prompt {i}", should_trigger=should, origin="generated", reviewed=False)


def test_marks_only_listed_cases(tmp_path):
    path = tmp_path / "evals.json"
    save_evals(path, EvalFile(skill_name="demo", evals=[_case(1), _case(2), _case(3, should=False)]))
    changed = mark.mark_cases(path, [1, 3])
    assert changed == [1, 3]
    assert {c.id: c.reviewed for c in load_evals(path).evals} == {1: True, 2: False, 3: True}


def test_unknown_id_is_rejected_and_file_is_untouched(tmp_path):
    path = tmp_path / "evals.json"
    save_evals(path, EvalFile(skill_name="demo", evals=[_case(1)]))
    before = path.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="unknown case ids"):
        mark.mark_cases(path, [1, 9])
    assert path.read_text(encoding="utf-8") == before


def test_already_reviewed_case_is_not_reported_as_changed(tmp_path):
    path = tmp_path / "evals.json"
    reviewed = _case(1)
    reviewed.reviewed = True
    save_evals(path, EvalFile(skill_name="demo", evals=[reviewed]))
    assert mark.mark_cases(path, [1]) == []


def test_marks_rubric_reviewed_once(tmp_path):
    path = tmp_path / "rubric.json"
    crit = [
        Criterion(id=f"c{i}", text="t", type="task_quality", weight=1, source="SKILL.md › Step 1")
        for i in range(1, 6)
    ]
    save_rubric(path, Rubric(skill_name="demo", source_hash="sha256:x", generated_by="m", criteria=crit))
    assert mark.mark_rubric(path) is True
    assert load_rubric(path).reviewed is True
    assert mark.mark_rubric(path) is False


def _skills_tree(tmp_path, monkeypatch, with_rubric=True):
    sdir = tmp_path / "skills" / "dept" / "demo"
    (sdir / "evals").mkdir(parents=True)
    (sdir / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
    save_evals(sdir / "evals" / "evals.json", EvalFile(skill_name="demo", evals=[_case(1), _case(2)]))
    if with_rubric:
        crit = [
            Criterion(id=f"c{i}", text="t", type="task_quality", weight=1, source="SKILL.md")
            for i in range(1, 6)
        ]
        save_rubric(
            sdir / "evals" / "rubric.json",
            Rubric(skill_name="demo", source_hash="sha256:x", generated_by="m", criteria=crit),
        )
    monkeypatch.chdir(tmp_path)
    return sdir / "evals"


def test_cli_happy_path(tmp_path, monkeypatch):
    ev = _skills_tree(tmp_path, monkeypatch)
    assert mark.main(["--skill", "demo", "--ids", "1,2", "--rubric"]) == 0
    assert all(c.reviewed for c in load_evals(ev / "evals.json").evals)
    assert load_rubric(ev / "rubric.json").reviewed is True


def test_cli_unknown_id_returns_2_and_leaves_file(tmp_path, monkeypatch):
    ev = _skills_tree(tmp_path, monkeypatch)
    before = (ev / "evals.json").read_bytes()
    assert mark.main(["--skill", "demo", "--ids", "1,9"]) == 2
    assert (ev / "evals.json").read_bytes() == before


def test_cli_malformed_ids_exits_2(tmp_path, monkeypatch):
    _skills_tree(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        mark.main(["--skill", "demo", "--ids", "1,a"])
    assert exc.value.code == 2


def test_cli_requires_ids_or_rubric(tmp_path, monkeypatch):
    _skills_tree(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        mark.main(["--skill", "demo"])
    assert exc.value.code == 2


def test_cli_missing_files_return_2(tmp_path, monkeypatch):
    ev = _skills_tree(tmp_path, monkeypatch, with_rubric=False)
    assert mark.main(["--skill", "demo", "--rubric"]) == 2
    (ev / "evals.json").unlink()
    assert mark.main(["--skill", "demo", "--ids", "1"]) == 2


def test_skill_dir_zero_or_many_matches_exit(tmp_path):
    root = tmp_path / "skills"
    (root / "a" / "demo").mkdir(parents=True)
    (root / "a" / "demo" / "SKILL.md").write_text("x", encoding="utf-8")
    with pytest.raises(SystemExit):
        mark.skill_dir("nope", root)
    (root / "b" / "demo").mkdir(parents=True)
    (root / "b" / "demo" / "SKILL.md").write_text("x", encoding="utf-8")
    with pytest.raises(SystemExit):
        mark.skill_dir("demo", root)
