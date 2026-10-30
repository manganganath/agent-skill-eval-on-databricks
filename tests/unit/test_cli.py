import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from skillhub.cli import app

ROOT = Path(__file__).resolve().parents[2]

SRC = Path(__file__).resolve().parents[2] / "config" / "hub.yaml"


def test_help_shows_config_option():
    r = CliRunner().invoke(app, ["--help"])
    assert r.exit_code == 0
    assert "--config" in r.output


def test_config_option_is_honoured(tmp_path, cfg):
    p = tmp_path / "other.yaml"
    p.write_text(
        SRC.read_text(encoding="utf-8").replace("catalog: ${SKILLHUB_CATALOG}", "catalog: other_cat")
    )
    r = CliRunner().invoke(app, ["--config", str(p), "config-show"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["catalog"] == "other_cat"


def test_bad_config_exits_1_with_one_line(tmp_path, monkeypatch):
    # DATABRICKS_HOST may be unset (the SDK's host is used), so drop a variable that has no fallback.
    monkeypatch.delenv("SQL_WAREHOUSE_ID", raising=False)
    r = CliRunner().invoke(app, ["--config", str(SRC), "config-show"])
    assert r.exit_code == 1
    assert "SQL_WAREHOUSE_ID" in r.output and len(r.output.strip().splitlines()) == 1


def test_tracebacks_never_print_locals():
    """pr-comment holds GITHUB_TOKEN in a local; older Typer releases printed locals by default."""
    from skillhub.cli import app

    assert app.pretty_exceptions_show_locals is False


def test_eval_digests_prints_what_the_gates_expect_without_a_workspace(tmp_path, make_bundle, monkeypatch):
    """Plan and the PR step run this on the checkout; it needs no Databricks settings."""
    import json

    from typer.testing import CliRunner

    from skillhub.bundle import content_hash
    from skillhub.cli import app
    from skillhub.evalspec import eval_digests

    for k in ("DATABRICKS_HOST", "SQL_WAREHOUSE_ID", "SKILLHUB_CATALOG"):
        monkeypatch.delenv(k, raising=False)
    evals = '{"skill_name": "a", "evals": [{"id": 101, "kind": "effectiveness", "prompt": "p", "should_trigger": true, "fixtures": ["n/doc.md"]}]}'
    b = make_bundle("common", "a", files={"evals/evals.json": evals})
    fx = tmp_path / "fx"
    (fx / "n").mkdir(parents=True)
    (fx / "n" / "doc.md").write_text("doc")
    args = [
        "eval-digests",
        "--skills",
        "common/a",
        "--skills-root",
        str(tmp_path / "skills"),
        "--fixtures-root",
        str(fx),
    ]
    r = CliRunner().invoke(app, args)
    assert r.exit_code == 0, r.output
    d = eval_digests(b.root, fx)
    assert json.loads(r.output) == [
        {
            "dept": "common",
            "skill": "a",
            "hash": content_hash(b.files),
            "rubric_hash": "none",
            "dataset_digest": d.dataset_digest,
            "cells": [
                "claude-sonnet/with_skill",
                "gpt/with_skill",
                "gemini-flash/with_skill",
            ],  # defaults.models
        }
    ]
    one = CliRunner().invoke(app, [*args, "--models", "gpt", "--modes", "trigger"])
    assert json.loads(one.output)[0]["cells"] == []  # no trigger cases: nothing to plan
    assert CliRunner().invoke(app, [*args[:2], "a", *args[3:]]).exit_code == 2  # dept/skill only
    (b.root / "evals" / "evals.json").unlink()
    r = CliRunner().invoke(app, args)
    assert r.exit_code == 1 and "error: common/a:" in r.output


# --- The Databricks Jobs entry point ---------------------------------------------------------------------------


def test_job_main_returns_normally_on_success(monkeypatch):
    """Serverless wheel tasks run under IPython and report SystemExit(0) as a failed task, so the Jobs entry
    point must return normally when the command succeeds."""
    from skillhub import cli

    monkeypatch.setattr(sys, "argv", ["skillhub-job", "--help"])
    assert cli.job_main() is None


def test_job_main_still_fails_the_task_on_errors(monkeypatch):
    from skillhub import cli

    monkeypatch.setattr(sys, "argv", ["skillhub-job", "no-such-command"])
    with pytest.raises(SystemExit) as e:
        cli.job_main()
    assert e.value.code not in (0, None)


def test_every_job_task_uses_the_jobs_entry_point():
    import yaml

    jobs = yaml.safe_load((ROOT / "resources" / "jobs.yml").read_text())["resources"]["jobs"]
    points = [
        t["python_wheel_task"]["entry_point"]
        for j in jobs.values()
        for t in j["tasks"]
        if "python_wheel_task" in t
    ]
    assert points and set(points) == {"skillhub-job"}
