"""A release reuses the pull request gate's eval only under the release rule (skillhub.ci.reuse)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from skillhub.ci.reuse import MergeFacts, decide, gate_run_keys, merge_facts
from skillhub.naming import make_run_key
from tests.unit.eval_rows import CALLER, DATASET, RUBRIC, TRUSTED, add_rows, ensure_eval_runs, trust_ci_eval

ROOT = Path(__file__).resolve().parents[2]
HEAD = "c" * 40
MAIN = "b" * 40
SKILLS_ONLY = MergeFacts((MAIN, HEAD), same_tree=True, changed=("skills/common/alpha/SKILL.md",))


def _never(*_):
    raise AssertionError("the workspace must not be read before the git and config rules pass")


def _decide(facts=SKILLS_ONLY, modes=("trigger",), keys=(), failures=None):
    seen = {"keys": [], "checked": []}

    def run_keys(head):
        seen["keys"].append(head)
        return list(keys)

    def check(key):
        seen["checked"].append(key)
        return (failures or {}).get(key, [])

    return decide(facts, modes, run_keys, check), seen


# --- The decision, rule by rule ---


@pytest.mark.parametrize("parents", [(MAIN,), (MAIN, HEAD, "d" * 40), ()])
def test_only_a_two_parent_merge_can_reuse(parents):
    facts = MergeFacts(parents, same_tree=True, changed=SKILLS_ONLY.changed)
    res = decide(facts, ["trigger"], _never, _never)
    assert not res.reuse and res.run_key == "" and f"{len(parents)} parent(s)" in res.reason


def test_a_merge_that_brought_more_than_the_head_cannot_reuse():
    facts = MergeFacts((MAIN, HEAD), same_tree=False, changed=SKILLS_ONLY.changed)
    res = decide(facts, ["trigger"], _never, _never)
    assert not res.reuse and "trees differ" in res.reason


@pytest.mark.parametrize(
    "changed",
    [
        ("skills/common/alpha/SKILL.md", "src/skillhub/eval/runner.py"),
        ("config/hub.yaml",),
        ("skills/common/alpha/SKILL.md", ".github/workflows/skills-publish.yml"),
        ("skillsx/a.md",),  # a sibling folder, not under skills/
        ("fixtures/northwind/orders.csv",),  # cases read fixtures, but they live outside skills/
    ],
)
def test_a_pull_request_that_changed_anything_outside_skills_cannot_reuse(changed):
    res = decide(MergeFacts((MAIN, HEAD), True, changed), ["trigger"], _never, _never)
    outside = [p for p in changed if not p.startswith("skills/")]
    assert not res.reuse and "outside skills/" in res.reason
    assert all(repr(p) in res.reason for p in outside)


def test_the_reason_names_at_most_five_paths_and_escapes_newlines():
    changed = tuple(f"src/{i}.py" for i in range(7)) + ("a\n::error::x",)
    res = decide(MergeFacts((MAIN, HEAD), True, changed), ["trigger"], _never, _never)
    assert "and 3 more" in res.reason and "\n" not in res.reason


def test_a_pull_request_that_changed_nothing_cannot_reuse():
    res = decide(MergeFacts((MAIN, HEAD), True, ()), ["trigger"], _never, _never)
    assert not res.reuse and "changed no files" in res.reason


@pytest.mark.parametrize("modes", [["trigger", "effectiveness"], ["effectiveness"], ["trigger", "trigger"]])
def test_reuse_needs_trigger_as_the_only_gating_mode(modes):
    """Pull request runs use one judge, merge runs the panel: an effectiveness gate would read other evidence."""
    res = decide(SKILLS_ONLY, modes, _never, _never)
    assert not res.reuse and "defaults.gating_modes" in res.reason


def test_no_pull_request_gate_run_for_the_head_means_re_evaluation():
    res, seen = _decide(keys=())
    assert not res.reuse and f"no trusted pull request gate run for {HEAD[:12]}" in res.reason
    assert seen == {"keys": [HEAD], "checked": []}


def test_a_gate_run_that_fails_the_merge_gate_check_is_not_reused():
    key = "pr12_ccccccc_a1"
    res, seen = _decide(keys=[key], failures={key: ["wrong content: common/alpha gpt trigger: ...", "x"]})
    assert not res.reuse and res.run_key == ""
    assert f"{key}: wrong content: common/alpha" in res.reason and seen["checked"] == [key]


def test_a_passing_gate_run_is_reused():
    res, seen = _decide(keys=["pr12_ccccccc_a1"])
    assert res.reuse and res.run_key == "pr12_ccccccc_a1" and res.reason == ""
    assert seen == {"keys": [HEAD], "checked": ["pr12_ccccccc_a1"]}


def test_the_newest_gate_run_that_passes_is_reused():
    keys = ["pr12_ccccccc_a3", "pr12_ccccccc_a2", "pr12_ccccccc_a1"]
    res, seen = _decide(keys=keys, failures={"pr12_ccccccc_a3": ["no result for planned cells: x"]})
    assert res.run_key == "pr12_ccccccc_a2" and seen["checked"] == keys[:2]
    res, _ = _decide(keys=keys, failures=dict.fromkeys(keys, ["blocking verdicts: fail"]))
    assert not res.reuse and "pr12_ccccccc_a3: blocking verdicts: fail" in res.reason


# --- What git says about the pushed commit ---


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip


def write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def commit(repo: Path, msg: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


def merge(repo: Path, branch: str) -> str:
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", f"Merge {branch}", branch)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture(scope="module")
def history(tmp_path_factory) -> dict:
    """One repository with every kind of pushed commit (commits are slow where global git hooks run)."""
    repo = tmp_path_factory.mktemp("repo")
    h: dict = {"repo": repo}
    git(repo, "init", "-q", "-b", "main")
    write(repo, "skills/common/alpha/SKILL.md", "alpha")
    write(repo, "src/code.py", "code")
    h["base"] = commit(repo, "base")
    # An up-to-date pull request that changes two skills.
    git(repo, "checkout", "-q", "-b", "skills")
    write(repo, "skills/common/alpha/SKILL.md", "alpha v2")
    write(repo, "skills/common/beta/SKILL.md", "beta")
    h["skills_head"] = commit(repo, "change skills")
    h["skills_merge"] = merge(repo, "skills")
    # A pull request that moves code into a skill folder.
    git(repo, "checkout", "-q", "-b", "move")
    git(repo, "mv", "src/code.py", "skills/common/alpha/code.py")
    commit(repo, "move code into a skill")
    h["move_merge"] = merge(repo, "move")
    # A pull request branched before main moved on, merged without updating it.
    git(repo, "checkout", "-q", "-b", "behind")
    write(repo, "skills/common/alpha/SKILL.md", "alpha v3")
    commit(repo, "change a skill")
    git(repo, "checkout", "-q", "main")
    write(repo, "README.md", "readme")
    commit(repo, "main moved on")
    h["behind_merge"] = merge(repo, "behind")
    # A commit pushed straight to main (or a squash merge).
    write(repo, "skills/common/alpha/SKILL.md", "alpha v4")
    h["direct"] = commit(repo, "pushed straight to main")
    return h


def test_merge_facts_of_an_up_to_date_skills_only_merge(history):
    facts = merge_facts(history["skills_merge"], history["repo"])
    assert facts.parents == (history["base"], history["skills_head"]) and facts.head == history["skills_head"]
    assert facts.same_tree
    assert facts.changed == ("skills/common/alpha/SKILL.md", "skills/common/beta/SKILL.md")


def test_merge_facts_list_both_sides_of_a_rename_into_skills(history):
    facts = merge_facts(history["move_merge"], history["repo"])
    assert facts.same_tree and set(facts.changed) == {"src/code.py", "skills/common/alpha/code.py"}
    assert not decide(facts, ["trigger"], _never, _never).reuse


def test_merge_facts_see_a_merge_of_a_branch_behind_main(history):
    facts = merge_facts(history["behind_merge"], history["repo"])
    assert facts.head is not None and not facts.same_tree
    # Main's change shows too: from the first parent, the branch undoes it.
    assert facts.changed == ("README.md", "skills/common/alpha/SKILL.md")


def test_merge_facts_of_a_squash_or_direct_commit(history):
    facts = merge_facts(history["direct"], history["repo"])
    assert len(facts.parents) == 1 and facts.head is None and facts.changed == ()


def test_merge_facts_fail_on_an_unknown_commit(history):
    from skillhub.ci.changes import GitRefError

    with pytest.raises(GitRefError):
        merge_facts("f" * 40, history["repo"])


# --- Finding the pull request gate's runs for the head ---


def _rows(reg, key, *, dept="common", skill="alpha", **kw):
    kw.setdefault("trigger_source", "pr")
    kw.setdefault("git_sha", HEAD)
    add_rows(reg.sql, reg.cfg, dept, skill, key, "sha256:" + "a" * 64, (("trigger", "pass"),), **kw)


def test_gate_run_keys_are_the_trusted_pull_request_runs_of_the_head_newest_first(reg):
    ensure_eval_runs(reg.sql, reg.cfg)
    _rows(reg, "pr12_ccccccc_a1", started_at="2026-10-28T09:00:00")
    _rows(reg, "pr12_ccccccc_a2", dept="equity_research", skill="comps", started_at="2026-10-28T11:00:00")
    _rows(reg, "pr12_ccccccc_a1", skill="beta", started_at="2026-10-28T12:00:00")  # a1's newest row
    _rows(reg, "pr12_ccccccc_a3", author="someone@example.com", started_at="2026-10-29T00:00:00")
    _rows(reg, "pr12_ccccccc_a4", trigger_source="manual", started_at="2026-10-29T00:00:00")
    _rows(reg, "merge_ccccccc_a1", trigger_source="merge", started_at="2026-10-29T00:00:00")
    _rows(reg, "pr12_ddddddd_a1", git_sha="d" * 40, started_at="2026-10-29T00:00:00")
    _rows(reg, "pr12_ccccccc_a1_h0123abcd", started_at="2026-10-29T00:00:00")  # not the gate's key
    keys = gate_run_keys(reg.sql.execute, reg.cfg, HEAD, trusted={TRUSTED, CALLER})
    assert keys == ["pr12_ccccccc_a1", "pr12_ccccccc_a2"]
    [(statement, params)] = [c for c in reg.sql.calls if "GROUP BY run_key" in c[0]]
    assert params == {"trigger_source": "pr", "git_sha": HEAD, "author_0": TRUSTED, "author_1": CALLER}
    assert statement.count("parent_run_id IS NOT NULL") == len(reg.cfg.departments)


def test_gate_run_keys_match_the_label_skills_pr_forms(cfg):
    text = (ROOT / ".github" / "workflows" / "skills-pr.yml").read_text()
    assert 'echo "run_key=pr${PR_NUMBER}-${HEAD_SHA:0:7}-a${RUN_ATTEMPT}"' in text
    rows = [{"run_key": make_run_key(f"pr12-{HEAD[:7]}-a3")}]
    assert gate_run_keys(lambda sql, params: rows, cfg, HEAD, trusted={"x"}) == ["pr12_ccccccc_a3"]
    # The reuse step accepts the same form.
    publish = (ROOT / ".github" / "workflows" / "skills-publish.yml").read_text()
    assert 'test("^pr[0-9]+_[0-9a-f]{7}_a[0-9]+$")' in publish


@pytest.mark.parametrize(("sha", "trusted"), [(HEAD, ()), (HEAD[:7], {"x"}), ("git:" + HEAD, {"x"})])
def test_gate_run_keys_fail_closed(sha, trusted):
    with pytest.raises(ValueError):
        gate_run_keys(_never, None, sha, trusted=trusted)


# --- The command CI runs ---


def _plan(cells=("gpt/trigger",)):
    return [
        {"dept": "common", "skill": "alpha", "hash": "sha256:" + "a" * 64, "rubric_hash": RUBRIC,
         "dataset_digest": DATASET, "cells": list(cells)},
    ]  # fmt: skip


@pytest.fixture
def merged(history, monkeypatch):
    """A skills-only merge of an up-to-date branch: (merge, head). The command runs in the checkout."""
    monkeypatch.chdir(history["repo"])
    return history["skills_merge"], history["skills_head"]


@pytest.fixture
def cli_db(reg, monkeypatch):
    import skillhub.sql

    ensure_eval_runs(reg.sql, reg.cfg)
    trust_ci_eval(monkeypatch)
    monkeypatch.setattr(skillhub.sql, "SqlClient", lambda w, warehouse: reg.sql)
    return reg


def _reuse(tmp_path, merge_sha, plan, skills="common/alpha"):
    path = tmp_path / "expect.json"
    path.write_text(json.dumps(plan))
    from skillhub.cli import app

    args = ["--config", str(ROOT / "config" / "hub.yaml"), "reuse-pr-gate", "--merge-sha", merge_sha]
    return CliRunner().invoke(app, [*args, "--skills", skills, "--expect-file", str(path)])


def _gate_rows(reg, head, **kw):
    add_rows(reg.sql, reg.cfg, "common", "alpha", f"pr7_{head[:7]}_a1", "sha256:" + "a" * 64,
             (("trigger", "pass"),), trigger_source="pr", git_sha=head, **kw)  # fmt: skip


def test_cli_reuses_a_passing_gate_run_of_the_merged_head(cli_db, merged, tmp_path):
    merge_sha, head = merged
    _gate_rows(cli_db, head)
    res = _reuse(tmp_path, merge_sha, _plan())
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout) == {"reuse": True, "run_key": f"pr7_{head[:7]}_a1", "reason": ""}
    # The check reads only the gate's own rows: trigger source pr, under that key.
    assert [p for s, p in cli_db.sql.calls if "ROW_NUMBER()" in s][-1]["trigger_source"] == "pr"


def test_cli_does_not_reuse_a_gate_run_bound_to_other_eval_inputs(cli_db, merged, tmp_path):
    merge_sha, head = merged
    _gate_rows(cli_db, head, rubric="sha256:" + "e" * 64)
    res = _reuse(tmp_path, merge_sha, _plan())
    out = json.loads(res.stdout)
    assert res.exit_code == 0 and not out["reuse"] and "wrong content" in out["reason"]


def test_cli_does_not_reuse_a_gate_run_without_every_planned_cell(cli_db, merged, tmp_path):
    merge_sha, head = merged
    _gate_rows(cli_db, head)
    res = _reuse(tmp_path, merge_sha, _plan(cells=("gpt/trigger", "claude-sonnet/trigger")))
    assert "no result for planned cells" in json.loads(res.stdout)["reason"]


def test_cli_decides_without_the_workspace_when_a_git_rule_fails(cli_db, history, monkeypatch, tmp_path):
    import skillhub.auth

    monkeypatch.chdir(history["repo"])
    monkeypatch.setattr(skillhub.auth, "workspace_client", _never)
    res = _reuse(tmp_path, history["direct"], _plan())
    assert res.exit_code == 0 and json.loads(res.stdout)["reason"].endswith("it is not a merge")


def test_cli_exits_1_without_details_when_the_workspace_fails(cli_db, merged, monkeypatch, tmp_path):
    def down(statement, params=None):
        raise RuntimeError("https://secret-host.example warehouse wh-1 is down")

    monkeypatch.setattr(cli_db.sql, "execute", down)
    res = _reuse(tmp_path, merged[0], _plan())
    assert res.exit_code == 1 and "cannot check the pull request gate (RuntimeError)" in res.output
    assert "secret-host" not in res.output


@pytest.mark.parametrize(
    ("plan", "skills"),
    [
        ([], "common/alpha"),
        (_plan(), "common/alpha,common/beta"),
        (_plan(cells=()), "common/alpha"),
        (_plan(), ""),
    ],
)
def test_cli_refuses_a_plan_that_does_not_cover_the_skills(cli_db, merged, tmp_path, plan, skills):
    assert _reuse(tmp_path, merged[0], plan, skills=skills).exit_code == 2
