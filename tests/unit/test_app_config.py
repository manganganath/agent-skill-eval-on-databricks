from __future__ import annotations

import re
import stat

import yaml

from tests.unit.test_bundle_config import ROOT, load_bundle

REQUIRED_SCOPES = {
    "sql",
    "model-serving",
    "ai-gateway",
    "files",
    "catalog.schemas:read",
    "catalog.tables:read",
}


def app() -> dict:
    return load_bundle()["resources"]["apps"]["skill_hub"]


def test_app_command_and_source():
    a = app()
    assert a["source_code_path"] == "../app"
    app_yaml = yaml.safe_load((ROOT / "app" / "app.yaml").read_text())
    assert app_yaml["command"] == ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]


def test_user_scopes_are_the_documented_names():
    scopes = set(app()["user_api_scopes"])
    assert REQUIRED_SCOPES <= scopes
    deprecated = {"sql.warehouses", "serving.serving-endpoints", "dashboards.genie", "files.files"}
    assert not (scopes & deprecated)


def test_app_resources_bind_the_warehouse_and_jobs_but_no_serving_endpoints():
    """The app calls models with the user's token. Binding an endpoint would need MANAGE on it."""
    resources = {r["name"]: r for r in app()["resources"]}
    assert resources["sql-warehouse"]["sql_warehouse"]["permission"] == "CAN_USE"
    assert resources["skill-eval-job"]["job"]["permission"] == "CAN_MANAGE_RUN"
    assert resources["skill-switch-job"]["job"]["permission"] == "CAN_MANAGE_RUN"
    assert not [r for r in resources.values() if "serving_endpoint" in r]


def test_app_env_comes_from_bundle_variables():
    env = {e["name"]: e.get("value") for e in app()["config"]["env"]}
    assert env["SKILLHUB_CATALOG"] == "${var.catalog}${var.catalog_suffix}"
    assert env["OTEL_TRACES_SAMPLER"] == "always_on"
    assert env["SKILLHUB_JOB_ID_EVAL"] == "${resources.jobs.skill_eval.id}"
    assert env["SKILLHUB_JOB_ID_SWITCH"] == "${resources.jobs.skill_switch.id}"
    assert env["SKILLHUB_WORKSPACE_ROOT"] == "${var.workspace_root}"  # The Jobs use the same folder
    assert not [n for n in env if n.startswith("SKILLHUB_ENDPOINT_")]


def test_app_env_supplies_every_hub_yaml_variable():
    # A bundle config.env replaces app.yaml env, so the bundle must set everything hub.yaml reads.
    entries = app()["config"]["env"]
    names = {e["name"] for e in entries}
    hub = (ROOT / "config" / "hub.yaml").read_text().split("\n")
    used = {m for line in hub for m in re.findall(r"\$\{([A-Z_]+)(?::-[^}]*)?\}", line.split("#")[0])}
    assert "SKILLHUB_WORKSPACE_ROOT" in used  # ${VAR:-default} counts too
    assert used - names == {"DATABRICKS_HOST"}  # the Apps runtime sets DATABRICKS_HOST itself
    bound = {r["name"] for r in app()["resources"]}
    for e in entries:
        assert ("value" in e) != ("value_from" in e), e["name"]
        if "value_from" in e:
            assert e["value_from"] in bound, e["name"]
    warehouse = next(e for e in entries if e["name"] == "SQL_WAREHOUSE_ID")
    assert warehouse["value_from"] == "sql-warehouse"


def test_app_yaml_sets_no_env():
    app_yaml = yaml.safe_load((ROOT / "app" / "app.yaml").read_text())
    assert "env" not in app_yaml


def test_dashboard_is_bound_to_the_catalog_and_no_viewer_credentials():
    d = load_bundle()["resources"]["dashboards"]["skill_eval_dashboard"]
    assert d["file_path"] == "../src/skillhub/results/dashboard/skill_eval.lvdash.json"
    assert d["warehouse_id"] == "${var.warehouse_id}"
    assert d["dataset_catalog"] == "${var.catalog}${var.catalog_suffix}"
    assert d["embed_credentials"] is False


def test_build_script_is_executable_and_stages_the_app():
    script = ROOT / "scripts" / "build_app.sh"
    assert script.stat().st_mode & stat.S_IXUSR
    text = script.read_text()
    assert "npm run build" in text and "app/skillhub" in text and "app/config" in text
    assert "uv export" in text and "--no-dev" in text and "app/requirements.txt" in text
    assert "package.json" in text  # tolerates a missing frontend
    ignored = (ROOT / ".gitignore").read_text().splitlines()
    assert "app/skillhub/" in ignored and "app/config/" in ignored


def test_sync_includes_the_git_ignored_build_outputs():
    include = set(load_bundle()["sync"]["include"])
    assert {"app/skillhub/**", "app/config/**", "app/requirements.txt", "app/backend/static/**"} <= include
