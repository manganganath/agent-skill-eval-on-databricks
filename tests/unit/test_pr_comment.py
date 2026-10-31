# tests/unit/test_pr_comment.py
from __future__ import annotations

import httpx
import pytest
import respx

from skillhub.ci.pr_comment import (
    COMMENT_MAX,
    MARKER,
    exit_code,
    missing_skills,
    render_matrix_markdown,
    upsert_pr_comment,
)

API = "https://api.github.com"
COMMENTS = f"{API}/repos/o/r/issues/7/comments"


def row(**over) -> dict:
    base = {
        "dept": "common",
        "skill": "xlsx-author",
        "model_profile": "claude-sonnet",
        "model_class": "frontier",
        "mode": "trigger",
        "verdict": "pass",
        "accuracy": 0.875,
        "spread": 0.04,
        "uplift_task_quality": None,
        "uplift_skill_format": None,
        "n_cases": 12,
        "n_reviewed": 9,
        "content_hash": HASH,
        "rubric_hash": RUBRIC,
        "dataset_digest": DATASET,
        "run_id": "abc",
        "experiment_id": "1",
        "run_url": "https://ws/ml/experiments/1/runs/abc",
    }
    base.update(over)
    return base


HASH = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64
RUBRIC = "sha256:" + "c" * 64
DATASET = "sha256:" + "d" * 64


def test_render_contains_marker_matrix_and_unreviewed_count():
    md = render_matrix_markdown([row()], run_key="pr12_ab12cd3")
    assert md.startswith(MARKER)
    assert "pr12_ab12cd3" in md
    assert "| xlsx-author | claude-sonnet | frontier | trigger | pass | 88% |" in md
    assert md.rstrip().endswith("| 3 |\n\nDetails in Skill Hub.")  # 12 cases, 9 reviewed => 3 unreviewed


def test_render_has_no_links_unless_asked():
    """The repository can be public, so no host, run or experiment ID and no Skill Hub URL by default."""
    md = render_matrix_markdown([row()], run_key="k", hub_url="https://hub.example")
    for leak in ("https://ws", "/ml/experiments/", "abc", "MLflow", "| Run |", "https://hub.example"):
        assert leak not in md, leak
    assert "Details in Skill Hub." in md
    linked = render_matrix_markdown([row()], run_key="k", links=True, hub_url="https://hub.example")
    assert "| Run |" in linked and "[MLflow](https://ws/ml/experiments/1/runs/abc)" in linked
    assert "Skill Hub: https://hub.example" in linked


def test_with_skill_rows_show_the_pass_rates_with_and_without_the_skill_and_the_uplift_in_points():
    rows = [
        row(mode="trigger", pass_rate=0.875),
        row(mode="without_skill", accuracy=None, pass_rate=0.25),
        row(mode="with_skill", accuracy=None, pass_rate=0.75, pass_rate_without=0.25),
        row(skill="no-baseline", mode="with_skill", accuracy=None, pass_rate=0.5, pass_rate_without=None),
    ]
    md = render_matrix_markdown(rows, run_key="k")
    header = "| Trigger accuracy | Pass rate | Pass rate (without) | Uplift (pass) | Uplift (task) |"
    assert header in md
    assert "| trigger | pass | 88% | 88% | - | - | - |" in md
    assert "| without_skill | pass | - | 25% | - | - | - |" in md
    assert "| with_skill | pass | - | 75% | 25% | +50 pp | - |" in md
    assert "| no-baseline | claude-sonnet | frontier | with_skill | pass | - | 50% | - | - | - |" in md
    lower = render_matrix_markdown(
        [row(mode="with_skill", pass_rate=0.2, pass_rate_without=0.6)], run_key="k"
    )
    assert "| 20% | 60% | -40 pp |" in lower


def test_render_escapes_pipes_in_cells():
    md = render_matrix_markdown([row(skill="a|b")], run_key="k")
    assert "a\\|b" in md


def test_render_no_rows_says_so():
    assert "No evaluation results" in render_matrix_markdown([], run_key="k")


def test_render_fits_one_github_comment_with_the_blocking_rows_first():
    """GitHub refuses a comment body over 65,536 characters (422), so the PR would get no comment. A large PR, such
    as an import of every skill, shows the blocking rows first and says how many rows are left out."""
    rows = [
        row(skill=f"skill-{i:02d}-with-a-long-name", model_profile=m, mode=mode, run_url=f"https://ws/{i}")
        for i in range(63)
        for m in ("claude-sonnet", "gpt", "gemini-flash")
        for mode in ("trigger", "without_skill", "with_skill")
    ]
    rows[-1]["verdict"] = "fail"
    md = render_matrix_markdown(rows, run_key="k", links=True, hub_url="https://hub.example")
    assert len(md) <= COMMENT_MAX
    table = [ln for ln in md.splitlines() if ln.startswith("| skill-")]
    assert table[0].startswith("| skill-62-with-a-long-name | gemini-flash |") and "| fail |" in table[0]
    assert f"{len(rows) - len(table)} of {len(rows)} rows are not shown" in md
    assert md.endswith("Skill Hub: https://hub.example")
    small = render_matrix_markdown(rows[-9:], run_key="k")  # a body that fits keeps its order and has no note
    assert "not shown" not in small and small.index("| fail |") > small.index("| trigger |")


@pytest.mark.parametrize(
    ("verdicts", "code"),
    [
        (["pass"], 0),
        (["warn", "advisory"], 0),
        (["pass", "fail"], 1),
        (["inconclusive"], 1),
        ([], 1),
    ],
)
def test_exit_codes(verdicts, code):
    assert exit_code(verdicts) == code


@respx.mock
def test_creates_comment_when_no_bot_comment_has_the_marker():
    respx.get(COMMENTS, params={"per_page": "100"}).mock(return_value=httpx.Response(200, json=[]))
    post = respx.post(COMMENTS).mock(return_value=httpx.Response(201, json={"id": 1}))
    assert upsert_pr_comment(repo="o/r", pr=7, body=f"{MARKER}\nhi", token="t") == "created"
    assert post.called


@respx.mock
def test_upsert_updates_existing_bot_comment_and_ignores_user_comments():
    respx.get(COMMENTS, params={"per_page": "100"}).mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": 5, "body": f"{MARKER} copied by a user", "user": {"type": "User"}},
                {"id": 42, "body": f"{MARKER} old", "user": {"type": "Bot"}},
            ],
        )
    )
    patch = respx.patch(f"{API}/repos/o/r/issues/comments/42").mock(
        return_value=httpx.Response(200, json={"id": 42})
    )
    post = respx.post(COMMENTS)
    assert upsert_pr_comment(repo="o/r", pr=7, body=f"{MARKER} new", token="t") == "updated"
    assert patch.called
    assert not post.called


@respx.mock
def test_upsert_follows_pagination_to_find_the_marker():
    page2 = f"{COMMENTS}?per_page=100&page=2"
    # Register page 2 first: the per_page-only route would also match the page 2 request.
    respx.get(page2).mock(
        return_value=httpx.Response(200, json=[{"id": 9, "body": MARKER, "user": {"type": "Bot"}}])
    )
    respx.get(COMMENTS, params={"per_page": "100"}).mock(
        return_value=httpx.Response(
            200,
            json=[{"id": 1, "body": "other", "user": {"type": "Bot"}}],
            headers={"Link": f'<{page2}>; rel="next"'},
        )
    )
    patch = respx.patch(f"{API}/repos/o/r/issues/comments/9").mock(
        return_value=httpx.Response(200, json={"id": 9})
    )
    assert upsert_pr_comment(repo="o/r", pr=7, body=f"{MARKER} x", token="t") == "updated"
    assert patch.called


def test_blocking_verdicts_ignore_without_skill_rows():
    from skillhub.ci.pr_comment import blocking_verdicts

    rows = [row(), row(mode="without_skill", verdict="fail")]
    assert blocking_verdicts(rows) == ["pass"]


def test_load_matrix_rows_queries_every_department_for_trusted_authors_only(cfg):
    from skillhub.ci.pr_comment import load_matrix_rows

    seen = {}

    def fake_execute(sql, params):
        seen["sql"], seen["params"] = sql, params
        return [{"run_id": "abc", "experiment_id": "9", "verdict": "pass"}]

    rows = load_matrix_rows(fake_execute, cfg, "pr1_abc", trusted={"sp-b", "sp-a"})
    assert seen["params"] == {"run_key": "pr1_abc", "author_0": "sp-a", "author_1": "sp-b"}
    assert seen["sql"].count("UNION ALL") == len(cfg.departments) - 1
    assert seen["sql"].count("run_author IN (:author_0, :author_1)") == len(cfg.departments)
    assert seen["sql"].count("parent_run_id IS NOT NULL") == len(cfg.departments)
    assert "content_hash" in seen["sql"]
    assert "run_url" not in rows[0]


def test_load_matrix_rows_without_trusted_authors_fails_closed(cfg):
    from skillhub.ci.pr_comment import load_matrix_rows

    with pytest.raises(ValueError, match="no trusted run authors"):
        load_matrix_rows(lambda sql, params: [row()], cfg, "k", trusted=set())


def test_add_run_links_builds_mlflow_urls(cfg):
    from skillhub.ci.pr_comment import add_run_links

    (r,) = add_run_links([{"run_id": "abc", "experiment_id": "9"}], cfg.workspace.host)
    assert r["run_url"] == f"{cfg.workspace.host.rstrip('/')}/ml/experiments/9/runs/abc"


@respx.mock
def test_http_error_does_not_leak_token():
    respx.get(COMMENTS, params={"per_page": "100"}).mock(return_value=httpx.Response(403))
    with pytest.raises(httpx.HTTPStatusError) as ei:
        upsert_pr_comment(repo="o/r", pr=7, body=MARKER, token="sekret-token")
    assert "sekret-token" not in str(ei.value)


@pytest.mark.parametrize("verdict", [None, "", "weird", "FAIL"])
def test_unknown_or_null_verdicts_block(verdict):
    from skillhub.ci.pr_comment import blocking_verdicts

    assert exit_code(blocking_verdicts([row(verdict=verdict)])) == 1


def test_only_without_skill_rows_means_no_results_and_blocks():
    from skillhub.ci.pr_comment import blocking_verdicts

    assert exit_code(blocking_verdicts([row(mode="without_skill")])) == 1


def test_matrix_sql_selects_latest_row_per_key(cfg):
    from skillhub.ci.pr_comment import load_matrix_rows

    seen = {}
    load_matrix_rows(lambda sql, params: seen.update(sql=sql, params=params) or [], cfg, "k", trusted={"t"})
    sql = seen["sql"]
    assert sql.count(
        "ROW_NUMBER() OVER (PARTITION BY skill, skill_version, model_profile, "
        "COALESCE(harness, 'reference'), mode"
    ) == len(cfg.departments)
    assert sql.count("ORDER BY started_at DESC, synced_at DESC") == len(cfg.departments)
    assert sql.count("WHERE rn = 1") == len(cfg.departments)
    assert sql.count("run_key = :run_key") == len(cfg.departments)
    assert "mode IS NOT NULL" not in sql  # a row without a mode stays in and blocks
    assert sql.count("accuracy, pass_rate, pass_rate_without, ") == len(cfg.departments)
    assert seen["params"] == {"run_key": "k", "author_0": "t"}


def test_cell_escapes_backslash_pipe_backtick_mention_and_html():
    md = render_matrix_markdown([row(skill="a\\|b")], run_key="k")
    assert "a\\\\\\|b" in md  # backslash escaped first, then the pipe
    assert "`" not in render_matrix_markdown([row(skill="x`y")], run_key="k").split("\n", 2)[2]
    md = render_matrix_markdown([row(skill="@team <b>")], run_key="k")
    assert "@team" not in md
    assert "@​team &lt;b>" in md


def test_missing_skills_blocks_on_a_skill_with_no_result():
    rows = [row(), row(dept="common", skill="other", mode="without_skill")]
    assert missing_skills(rows, "common/xlsx-author") == []
    assert missing_skills(rows, "common/xlsx-author,common/other,sales/new") == ["common/other", "sales/new"]
    assert missing_skills(rows, "") == []  # empty means no check


def test_null_mode_row_blocks():
    from skillhub.ci.pr_comment import blocking_verdicts

    assert exit_code(blocking_verdicts([row(), row(mode=None, verdict="pass")])) == 1


def test_parse_expect_hashes():
    from skillhub.ci.pr_comment import parse_expect_hashes

    assert parse_expect_hashes([f"common/xlsx-author={HASH}", f" common/xlsx-author={HASH} "]) == {
        "common/xlsx-author": HASH
    }
    for bad in ("common/xlsx-author", f"xlsx-author={HASH}", "common/x=sha256:ABC", f"a/b={HASH};id"):
        with pytest.raises(ValueError):
            parse_expect_hashes([bad])
    with pytest.raises(ValueError, match="two hashes"):
        parse_expect_hashes([f"common/x={HASH}", f"common/x={OTHER}"])


def test_hash_mismatches_check_every_gating_row_of_an_expected_skill():
    from skillhub.ci.pr_comment import hash_mismatches

    rows = [
        row(),
        row(model_profile="gpt", content_hash=OTHER),
        row(mode="without_skill", content_hash=None),  # the baseline does not gate
        row(skill="other", content_hash=OTHER),  # not expected
    ]
    (msg,) = hash_mismatches(rows, {"common/xlsx-author": HASH})
    assert msg.startswith("common/xlsx-author gpt trigger:") and OTHER in msg


@pytest.fixture
def cli_env(monkeypatch):
    from pathlib import Path

    import skillhub.auth
    import skillhub.ci.pr_comment as pc
    import skillhub.results.experiments as experiments
    import skillhub.sql

    cfg_path = Path(__file__).resolve().parents[2] / "config" / "hub.yaml"
    monkeypatch.setenv("DATABRICKS_HOST", "https://secret-host.example")
    monkeypatch.setenv("SKILLHUB_CATALOG", "cat")
    monkeypatch.setenv("SQL_WAREHOUSE_ID", "wh")
    monkeypatch.setattr(skillhub.auth, "workspace_client", lambda: object())
    monkeypatch.setattr(skillhub.sql, "SqlClient", lambda *a, **k: type("S", (), {"execute": None})())
    monkeypatch.setattr(experiments, "trusted_run_authors", lambda w=None: frozenset({"ci-eval"}))
    state = {"rows": [row()], "trusted": None}

    def load(fetch, cfg, key, *, trusted):
        state["trusted"] = trusted
        return [dict(r) for r in state["rows"]]

    monkeypatch.setattr(pc, "load_matrix_rows", load)
    return cfg_path, state


def _invoke(cfg_path, *args):
    from typer.testing import CliRunner

    from skillhub.cli import app

    return CliRunner().invoke(app, ["--config", str(cfg_path), *args])


def test_check_verdict_skills_option(cli_env):
    cfg_path, state = cli_env

    def run(skills: str):
        return _invoke(cfg_path, "check-verdict", "--run-key", "merge-abc1234-a1", "--skills", skills)

    assert run("").exit_code == 0
    assert run("common/xlsx-author").exit_code == 0
    assert run("common/xlsx-author,sales/new").exit_code == 1
    assert state["trusted"] == {"ci-eval"}


def test_check_verdict_binds_the_evaluated_content_hash(cli_env):
    """A passing row for other content (a swapped candidate, a forged row) fails the gate."""
    cfg_path, state = cli_env

    def run(*expect):
        args = [a for h in expect for a in ("--expect-hash", h)]
        return _invoke(cfg_path, "check-verdict", "--run-key", "merge-abc1234-a1", *args)

    assert run(f"common/xlsx-author={HASH}").exit_code == 0
    res = run(f"common/xlsx-author={OTHER}")
    assert res.exit_code == 1 and "wrong content: common/xlsx-author" in res.output
    res = run(f"common/xlsx-author={HASH}", f"sales/new={HASH}")  # an expected skill needs a result
    assert res.exit_code == 1 and "no result for: sales/new" in res.output
    state["rows"] = [row(), row(model_profile="gpt", content_hash=OTHER)]  # every gating row must match
    assert run(f"common/xlsx-author={HASH}").exit_code == 1
    state["rows"] = [row(), row(mode="without_skill", content_hash=OTHER)]  # the baseline doesn't gate
    assert run(f"common/xlsx-author={HASH}").exit_code == 0
    assert run("common/xlsx-author=sha256:nothex").exit_code == 2


def test_check_verdict_still_fails_on_a_failing_verdict_with_the_right_hash(cli_env):
    cfg_path, state = cli_env
    state["rows"] = [row(verdict="fail")]
    res = _invoke(cfg_path, "check-verdict", "--run-key", "k", "--expect-hash", f"common/xlsx-author={HASH}")
    assert res.exit_code == 1


@pytest.mark.parametrize("links", ["", "false", "TRUE", "yes", "true"])
def test_pr_comment_body_carries_no_host_or_ids_by_default(cli_env, monkeypatch, links):
    import skillhub.ci.pr_comment as pc

    cfg_path, _ = cli_env
    posted = {}
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("SKILL_HUB_URL", "https://hub.example")
    monkeypatch.setenv("SKILLHUB_PR_LINKS", links)
    monkeypatch.setattr(pc, "upsert_pr_comment", lambda **kw: posted.update(kw) or "created")
    res = _invoke(cfg_path, "pr-comment", "--run-key", "pr12-ab12cd3-a1", "--pr", "7")
    assert res.exit_code == 0, res.output
    body = posted["body"]
    if links == "true":  # the explicit opt-in for private repositories; nothing else turns links on
        assert "https://secret-host.example/ml/experiments/1/runs/abc" in body
        assert "https://hub.example" in body
        return
    for leak in ("secret-host", "/ml/experiments/", "abc", "https://hub.example"):
        assert leak not in body, leak
    assert "pr12_ab12cd3_a1" in body and "Details in Skill Hub." in body


# --- The gates bind the eval inputs ---


def _expect(**over):
    item = {
        "dept": "common",
        "skill": "xlsx-author",
        "hash": HASH,
        "rubric_hash": RUBRIC,
        "dataset_digest": DATASET,
        "cells": ["claude-sonnet/trigger"],
    }
    item.update(over)
    return item


def test_binding_mismatches_name_every_eval_input_that_differs():
    from skillhub.ci.pr_comment import Expected, binding_mismatches

    want = {"common/xlsx-author": Expected(HASH, RUBRIC, DATASET)}
    assert binding_mismatches([row(), row(mode="without_skill", dataset_digest=None)], want) == []
    (msg,) = binding_mismatches([row(dataset_digest=OTHER)], want)
    assert "cases and fixtures dataset_digest=" + OTHER in msg
    (msg,) = binding_mismatches([row(rubric_hash=None)], want)
    assert "rubric rubric_hash=None" in msg
    assert binding_mismatches([row(rubric_hash=None)], {"common/xlsx-author": Expected(HASH)}) == []


def test_parse_expectations_reads_eval_digests_output_and_refuses_bad_entries():
    import json

    from skillhub.ci.pr_comment import Expected, parse_expectations

    assert parse_expectations(json.dumps([_expect(), _expect(skill="b", rubric_hash="none", cells=[])])) == {
        "common/xlsx-author": Expected(HASH, RUBRIC, DATASET, ("claude-sonnet/trigger",)),
        "common/b": Expected(HASH, "none", DATASET, ()),
    }
    for bad in (
        "{",
        "{}",
        json.dumps([_expect(), _expect()]),
        json.dumps([_expect(dept="Bad")]),
        json.dumps([_expect(hash="x")]),
        json.dumps([_expect(rubric_hash="")]),
        json.dumps([_expect(dataset_digest="none")]),
        json.dumps([_expect(cells=None)]),
        json.dumps([_expect(cells=["gpt/without_skill"])]),
        json.dumps([_expect(cells="gpt/trigger")]),
        json.dumps(["x"]),
    ):
        with pytest.raises(ValueError):
            parse_expectations(bad)


def test_check_verdict_binds_the_eval_inputs_from_the_expect_file(cli_env, tmp_path):
    """Rows that evaluated a swapped evals.json, rubric or fixture fail the gate."""
    import json

    cfg_path, state = cli_env
    path = tmp_path / "expect.json"

    def run(*items, extra=()):
        path.write_text(json.dumps(list(items)))
        return _invoke(cfg_path, "check-verdict", "--run-key", "k", "--expect-file", str(path), *extra)

    assert run(_expect()).exit_code == 0
    res = run(_expect(dataset_digest=OTHER))
    assert (
        res.exit_code == 1
        and "wrong content: common/xlsx-author" in res.output
        and "dataset_digest" in res.output
    )
    assert run(_expect(rubric_hash="none")).exit_code == 1
    res = run(_expect(), _expect(skill="other"))
    assert res.exit_code == 1 and "no result for: common/other" in res.output
    assert run(_expect(), extra=("--expect-hash", f"common/xlsx-author={OTHER}")).exit_code == 2
    path.write_text("not json")
    assert _invoke(cfg_path, "check-verdict", "--run-key", "k", "--expect-file", str(path)).exit_code == 2


# --- The gates need every planned cell ---


def test_missing_cells_name_every_planned_gating_cell_without_a_row():
    from skillhub.ci.pr_comment import Expected, missing_cells

    rows = [row(), row(mode="without_skill", model_profile="gpt")]
    want = {
        "common/xlsx-author": Expected(HASH, cells=("claude-sonnet/trigger", "gpt/trigger", "gpt/with_skill"))
    }
    assert missing_cells(rows, want) == [
        "common/xlsx-author gpt/trigger",
        "common/xlsx-author gpt/with_skill",
    ]
    assert missing_cells(rows, {"common/xlsx-author": Expected(HASH)}) == []  # --expect-hash: no plan


def test_check_verdict_fails_when_a_planned_cell_has_no_row(cli_env, tmp_path):
    """A failing child run deleted before sync leaves its cell empty; a passing row elsewhere is not enough."""
    import json

    cfg_path, state = cli_env
    path = tmp_path / "expect.json"
    path.write_text(json.dumps([_expect(cells=["claude-sonnet/trigger", "claude-sonnet/with_skill"])]))
    res = _invoke(cfg_path, "check-verdict", "--run-key", "k", "--expect-file", str(path))
    assert (
        res.exit_code == 1
        and "no result for planned cells: common/xlsx-author claude-sonnet/with_skill" in res.output
    )
    state["rows"] = [row(), row(mode="with_skill")]
    assert _invoke(cfg_path, "check-verdict", "--run-key", "k", "--expect-file", str(path)).exit_code == 0


def test_blocking_verdicts_skip_modes_outside_gating_modes_but_never_unknown_ones():
    from skillhub.ci.pr_comment import blocking_verdicts, exit_code

    rows = [row(), row(mode="with_skill", verdict="fail"), row(mode="without_skill", verdict="fail")]
    assert blocking_verdicts(rows, ["trigger"]) == ["pass"]
    assert blocking_verdicts(rows, ["trigger", "effectiveness"]) == ["pass", "fail"]
    assert blocking_verdicts([row(mode="mystery", verdict="pass")], ["trigger"]) == ["pass"]  # fail closed
    assert (
        exit_code(blocking_verdicts([row(mode="with_skill", verdict="pass")], ["trigger"])) == 1
    )  # nothing gated
