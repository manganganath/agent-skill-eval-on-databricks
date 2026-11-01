import json
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from skillhub import cli
from skillhub.eval.runner import RunSummary
from skillhub.model_catalog import store
from skillhub.model_catalog.entry import CatalogEntry
from skillhub.model_catalog.merge import CatalogError, load_requested, resolve, with_catalog
from tests.fakes import FakeSql, make_cfg

NOW = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)


def row(name, kind="external_model", enabled=True, **over):
    base = {"ug_model": {"route": "serving", "endpoint": "databricks-llama-4-maverick"},
            "external_model": {"route": "bedrock", "model_id": "amazon.nova-lite-v1:0", "region": "us-east-1"},
            "external_agent": {"harness": "agentcore"}}[kind]  # fmt: skip
    return CatalogEntry(name=name, kind=kind, model_class="fast", enabled=enabled, **{**base, **over})


ROWS = [
    row("nova-lite"),
    row("llama", "ug_model"),
    row("old-nova", enabled=False),
    row("agent", "external_agent"),
]


def test_only_enabled_model_rows_are_merged_and_hub_yaml_wins_a_clash():
    cfg = make_cfg()
    merged = with_catalog(cfg, [*ROWS, row("gpt", model_id="amazon.titan")], 4)
    assert set(merged.models) - set(cfg.models) == {"nova-lite", "llama"}
    assert merged.models["gpt"] == cfg.models["gpt"]
    assert merged.models["nova-lite"].catalog_version == 4
    assert set(cfg.models) == set(make_cfg().models)  # the loaded config is not changed


def test_an_enabled_external_agent_is_merged_with_its_harness_and_settings():
    agent = CatalogEntry(
        name="cc-sonnet", kind="external_agent", harness="claude-code", route="ug-anthropic", enabled=True,
        endpoint="databricks-claude-sonnet-5-5", model_class="frontier", agent={"cli_version": "2.1.314"},
    )  # fmt: skip
    merged = resolve(make_cfg(), [*ROWS, agent], 11, ["cc-sonnet"])
    profile = merged.models["cc-sonnet"]
    assert (profile.kind, profile.harness, profile.agent) == (
        "external_agent",
        "claude-code",
        {"cli_version": "2.1.314"},
    )


def test_a_connection_test_may_merge_a_disabled_row():
    merged = with_catalog(make_cfg(), ROWS, 4, only=["old-nova"], include_disabled=True)
    assert "old-nova" in merged.models and "nova-lite" not in merged.models


def test_a_request_merges_only_the_catalog_profiles_it_names():
    cfg = make_cfg()
    merged = resolve(cfg, ROWS, 9, ["gpt", "nova-lite"])
    assert set(merged.models) - set(cfg.models) == {"nova-lite"}


@pytest.mark.parametrize(
    ("names", "says"),
    [
        (["nope"], "unknown model profile(s): nope"),
        (["old-nova"], "disabled in the model catalog: old-nova"),
        (["agent"], "not a model this release can run: agent"),
    ],
)
def test_a_request_naming_a_profile_that_cannot_run_is_refused(names, says):
    with pytest.raises(CatalogError, match=says.replace("(", r"\(").replace(")", r"\)")):
        resolve(make_cfg(), ROWS, 9, names)


def test_hub_yaml_names_never_read_the_catalog():
    class NoSql:
        def execute(self, *a, **k):
            raise AssertionError("the catalog was read")

    cfg = make_cfg()
    assert load_requested(cfg, NoSql, ["gpt", "claude-sonnet"]) is cfg
    assert load_requested(cfg, NoSql, []) is cfg


def test_an_unreadable_catalog_or_workspace_refuses_a_catalog_model():
    class Broken:
        def execute(self, *a, **k):
            raise RuntimeError("TABLE_OR_VIEW_NOT_FOUND")

    def no_workspace():
        raise ValueError("default auth: cannot configure default credentials")

    for connect in (Broken, no_workspace):
        with pytest.raises(CatalogError, match="the model catalog cannot be read"):
            load_requested(make_cfg(), connect, ["nova-lite"])


@pytest.fixture
def cli_run(monkeypatch, make_bundle):
    """The eval command with the catalog in a fake table and run_eval replaced: what reaches the runner."""
    cfg, sql = make_cfg(), FakeSql()
    store.ensure_table(cfg, sql)
    for r in ROWS:
        store.insert_entry(sql, cfg, r, "rita@example.com", NOW)
    seen = {}

    def fake_run(c, req):
        seen.update(cfg=c, req=req)
        return RunSummary(run_key=req.run_key, parent_run_ids={}, children=[])

    bundle = make_bundle("common", "audit-xls")
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(cli, "workspace_client", lambda: object())
    monkeypatch.setattr("skillhub.sql.SqlClient", lambda *a, **k: sql)
    monkeypatch.setattr("skillhub.bundle.discover_bundles", lambda root=None: [bundle])
    monkeypatch.setattr("skillhub.eval.runner.run_eval", fake_run)

    def invoke(models):
        args = ["eval", "--run-key", "hub_0123abcd", "--skills", "common/audit-xls", "--models", models,
                "--modes", "trigger", "--source", "live", "--trigger-source", "hub"]  # fmt: skip
        return CliRunner().invoke(cli.app, args)

    return invoke, seen, sql


def test_the_eval_command_runs_an_enabled_catalog_model(cli_run):
    invoke, seen, _ = cli_run
    result = invoke("gpt,nova-lite")
    assert result.exit_code == 0, result.output
    assert seen["req"].models == ["gpt", "nova-lite"]
    assert seen["cfg"].models["nova-lite"].route == "bedrock"
    assert json.loads(result.output)["run_key"] == "hub_0123abcd"


def test_the_eval_command_refuses_a_disabled_catalog_model_before_anything_runs(cli_run):
    invoke, seen, _ = cli_run
    result = invoke("old-nova")
    assert result.exit_code == 2
    assert "disabled in the model catalog: old-nova" in result.output
    assert seen == {}


def test_a_run_on_hub_yaml_models_never_touches_the_catalog(cli_run):
    invoke, seen, sql = cli_run
    before = len(sql.calls)
    assert invoke("default").exit_code == 0
    assert invoke("gpt").exit_code == 0
    assert len(sql.calls) == before
    assert seen["cfg"] is cli.load_config()
