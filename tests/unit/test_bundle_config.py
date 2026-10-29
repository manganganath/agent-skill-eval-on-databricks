from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def load_bundle() -> dict:
    """databricks.yml plus every file under resources/, merged the way `include` merges them."""
    bundle = yaml.safe_load((ROOT / "databricks.yml").read_text())
    resources: dict[str, dict] = {}
    for path in sorted((ROOT / "resources").glob("*.yml")):
        doc = yaml.safe_load(path.read_text()) or {}
        for kind, items in (doc.get("resources") or {}).items():
            resources.setdefault(kind, {}).update(items or {})
    bundle["resources"] = resources
    return bundle


def test_targets_are_dev_and_prod_with_catalog_suffix():
    b = load_bundle()
    assert set(b["targets"]) == {"dev", "prod"}
    assert b["targets"]["dev"]["default"] is True
    assert b["targets"]["dev"]["variables"]["catalog_suffix"] == "_dev"
    assert b["targets"]["prod"]["variables"]["catalog_suffix"] == ""
    assert b["targets"]["dev"]["mode"] == "development"
    assert b["targets"]["prod"]["mode"] == "production"


def test_required_variables_exist_and_warehouse_is_not_hardcoded():
    variables = load_bundle()["variables"]
    for name in (
        "catalog",
        "catalog_suffix",
        "app_name",
        "warehouse_id",
        "ci_principal",
        "ci_eval_principal",
        "app_principal",
        "alert_destination_id",
    ):
        assert name in variables, name
    for name in ("warehouse_id", "ci_principal", "ci_eval_principal", "app_principal"):
        assert "default" not in variables[name], name


def test_endpoint_variables_are_gone():
    # Endpoint names live in config/hub.yaml, not in bundle variables.
    assert not [v for v in load_bundle()["variables"] if v.startswith("endpoint_")]


def test_prod_meets_production_mode_prerequisites():
    prod = load_bundle()["targets"]["prod"]
    assert prod["workspace"]["root_path"] == "~/.bundle/${bundle.name}/${bundle.target}"
    assert prod["run_as"]["service_principal_name"] == "${var.ci_principal}"
    assert {"service_principal_name": "${var.ci_principal}", "level": "CAN_MANAGE"} in prod["permissions"]


def test_generated_files_are_gitignored():
    ignored = (ROOT / ".gitignore").read_text().splitlines()
    for entry in ("build/", "dist/", "app/requirements.txt", "app/backend/static/"):
        assert entry in ignored, entry


def test_wheel_artifact_is_built_with_uv():
    artifact = load_bundle()["artifacts"]["skillhub_wheel"]
    assert artifact["type"] == "whl"
    assert artifact["build"] == "uv build --wheel"


def test_resources_are_included():
    assert "resources/*.yml" in load_bundle()["include"]


EVAL_PARAMS = [
    "run_key",
    "skills",
    "models",
    "modes",
    "source",
    "trigger_source",
    "requested_by",
    "versions",
    "skills_root",
    "fixtures_root",
]
SWITCH_PARAMS = [
    "action",
    "dept",
    "skill",
    "version",
    "git_sha",
    "eval_run_id",
    "actor",
    "skills_root",
    "expect_hash",
    "run_key",
    "expect_rubric_hash",
    "expect_dataset_digest",
    "expect_cells",
]


def jobs_by_name() -> dict[str, dict]:
    return {job["name"]: job for job in load_bundle()["resources"]["jobs"].values()}


def test_the_bundle_defines_the_four_jobs():
    assert set(jobs_by_name()) == {"skillhub-bootstrap", "skill-eval", "skill-switch", "skillhub-nightly"}


def test_job_resource_keys_are_referenced_by_other_tasks():
    assert set(load_bundle()["resources"]["jobs"]) == {
        "skillhub_bootstrap",
        "skill_eval",
        "skill_switch",
        "skillhub_nightly",
    }


def test_skill_eval_parameters_and_task_chain():
    job = jobs_by_name()["skill-eval"]
    assert [p["name"] for p in job["parameters"]] == EVAL_PARAMS
    assert {p["name"]: p["default"] for p in job["parameters"]}["skills_root"] == ""
    assert {p["name"]: p["default"] for p in job["parameters"]}["fixtures_root"] == ""
    tasks = {t["task_key"]: t for t in job["tasks"]}
    assert set(tasks) == {"eval", "sync"}
    assert tasks["sync"]["depends_on"] == [{"task_key": "eval"}]
    assert tasks["eval"]["python_wheel_task"]["entry_point"] == "skillhub"
    assert tasks["sync"]["python_wheel_task"]["parameters"][:2] == ["sync", "--run-key"]


def test_skill_eval_passes_roots_and_versions_to_the_cli():
    params = jobs_by_name()["skill-eval"]["tasks"][0]["python_wheel_task"]["parameters"]
    for flag, name in (
        ("--versions", "versions"),
        ("--skills-root", "skills_root"),
        ("--fixtures-root", "fixtures_root"),
    ):
        assert params[params.index(flag) + 1] == "{{job.parameters." + name + "}}", flag


def test_skill_switch_parameters_and_serialisation():
    job = jobs_by_name()["skill-switch"]
    assert [p["name"] for p in job["parameters"]] == SWITCH_PARAMS
    assert job["max_concurrent_runs"] == 1
    assert job["queue"] == {"enabled": True}
    assert len(job["tasks"]) == 1
    assert job["tasks"][0]["python_wheel_task"]["parameters"][0] == "job-switch"
    assert "--skills-root" in job["tasks"][0]["python_wheel_task"]["parameters"]
    assert "--expect-hash" in job["tasks"][0]["python_wheel_task"]["parameters"]


def test_bootstrap_has_a_single_python_task():
    """The register_scorers notebook is gone, so the bootstrap Job is one Python task."""
    job = jobs_by_name()["skillhub-bootstrap"]
    assert [t["task_key"] for t in job["tasks"]] == ["bootstrap"]
    assert "python_wheel_task" in job["tasks"][0]
    assert "notebook_task" not in job["tasks"][0]


def grants_of(name: str) -> dict[str, str]:
    return {p["service_principal_name"]: p["level"] for p in jobs_by_name()[name]["permissions"]}


def test_run_permissions_follow_the_two_ci_identities():
    """The ci_eval principal runs evals, ci switches; the app principal starts both from the Skill Hub."""
    assert grants_of("skill-eval") == {
        "${var.ci_eval_principal}": "CAN_MANAGE_RUN",
        "${var.app_principal}": "CAN_MANAGE_RUN",
    }
    assert grants_of("skill-switch") == {
        "${var.ci_principal}": "CAN_MANAGE_RUN",
        "${var.app_principal}": "CAN_MANAGE_RUN",
    }


def test_jobs_do_not_set_run_as_in_dev():
    """Dev deploys run every Job as the deployer, so a dev deploy needs no role on a CI principal."""
    for name, job in jobs_by_name().items():
        assert "run_as" not in job, name
    assert "resources" not in load_bundle()["targets"]["dev"]


def test_prod_run_as_is_set_per_job():
    """Eval and nightly run as ci_eval, switch as ci. Bootstrap inherits ci."""
    prod = load_bundle()["targets"]["prod"]
    assert prod["run_as"]["service_principal_name"] == "${var.ci_principal}"
    jobs = prod["resources"]["jobs"]
    assert set(jobs) == {"skill_eval", "skill_switch", "skillhub_nightly"}
    ci_eval = {"service_principal_name": "${var.ci_eval_principal}"}
    assert jobs["skill_eval"]["run_as"] == ci_eval
    assert jobs["skillhub_nightly"]["run_as"] == ci_eval
    assert jobs["skill_switch"]["run_as"] == {"service_principal_name": "${var.ci_principal}"}
    assert {"service_principal_name": "${var.ci_eval_principal}", "level": "CAN_VIEW"} in prod["permissions"]


def test_nightly_schedule_is_daily_0200_utc():
    job = jobs_by_name()["skillhub-nightly"]
    assert job["schedule"]["quartz_cron_expression"] == "0 0 2 * * ?"
    assert job["schedule"]["timezone_id"] == "UTC"
    # Unset means UNPAUSED in prod; development mode pauses it in dev (D1).
    assert job["schedule"].get("pause_status", "UNPAUSED") == "UNPAUSED"
    assert load_bundle()["targets"]["dev"]["mode"] == "development"
    names = [t["task_key"] for t in job["tasks"]]
    assert names == ["eval_live", "sync_live", "reconcile", "check_permissions", "cleanup"]
    assert "nightly-{{job.run_id}}" in str(job["tasks"][0]["python_wheel_task"]["parameters"])


def test_every_job_sets_skills_and_config_env():
    """Every Job sets SKILLHUB_SKILLS; SKILLHUB_CONFIG always comes from the deployed bundle."""
    for job in jobs_by_name().values():
        variables = {k: v for e in job["environment_variables"] for k, v in e["spec"]["variables"].items()}
        assert variables["SKILLHUB_SKILLS"] == "${workspace.file_path}/skills", job["name"]
        assert variables["SKILLHUB_CONFIG"] == "${workspace.file_path}/config/hub.yaml", job["name"]
        for task in job["tasks"]:
            assert task["environment_variables_key"] in {
                e["environment_variables_key"] for e in job["environment_variables"]
            }


def test_every_task_environment_key_is_defined_on_its_job():
    for job in jobs_by_name().values():
        keys = {e["environment_key"] for e in job["environments"]}
        for task in job["tasks"]:
            assert task["environment_key"] in keys, (job["name"], task["task_key"])
        assert all(e["spec"]["environment_version"] == "5" for e in job["environments"])


def test_every_job_names_the_trusted_run_authors():
    """Sync trusts runs by ci_eval and the deployer (not the app); the Jobs pass the IDs. The app
    principal is still passed: the experiment folder ACL gives it CAN_EDIT for Try traces."""
    for job in jobs_by_name().values():
        variables = {k: v for e in job["environment_variables"] for k, v in e["spec"]["variables"].items()}
        assert variables["SKILLHUB_CI_PRINCIPAL"] == "${var.ci_principal}", job["name"]
        assert variables["SKILLHUB_CI_EVAL_PRINCIPAL"] == "${var.ci_eval_principal}", job["name"]
        assert variables["SKILLHUB_APP_PRINCIPAL"] == "${var.app_principal}", job["name"]
        assert variables["SKILLHUB_DEPLOYER"] == "${workspace.current_user.userName}", job["name"]


def test_switch_and_nightly_failures_notify_the_alert_destination():
    """The notifications come from a complex variable that is empty unless the prod deploy sets it."""
    variables = load_bundle()["variables"]
    assert variables["job_failure_notifications"]["type"] == "complex"
    assert variables["job_failure_notifications"]["default"] == {}
    jobs = jobs_by_name()
    for name in ("skill-switch", "skillhub-nightly"):
        assert jobs[name]["webhook_notifications"] == "${var.job_failure_notifications}", name
    for name in ("skill-eval", "skillhub-bootstrap"):  # eval failures reach whoever started them
        assert "webhook_notifications" not in jobs[name], name


def test_app_and_dashboard_grant_access_to_their_audiences():
    """Without these, prod users get 'no access', and grants made by hand are reset on every deploy."""
    b = load_bundle()
    hub = yaml.safe_load((ROOT / "config" / "hub.yaml").read_text())
    assert b["variables"]["app_users_group"]["default"] == "users"
    assert b["variables"]["reviewers_group"]["default"] == hub["roles"]["reviewers"]
    app = b["resources"]["apps"]["skill_hub"]
    assert app["permissions"] == [{"group_name": "${var.app_users_group}", "level": "CAN_USE"}]
    dashboard = b["resources"]["dashboards"]["skill_eval_dashboard"]
    assert dashboard["permissions"] == [{"group_name": "${var.reviewers_group}", "level": "CAN_RUN"}]
    assert dashboard["embed_credentials"] is False  # viewers still read with their own UC permissions


def test_every_job_task_passes_only_options_its_cli_command_has():
    """A Job that passes a flag the CLI lacks fails every run with "No such option" (for example
    skill-switch's --run-key, which the publish gate added). Each wheel task names a CLI command, and every
    --flag it passes must be an option of that command."""
    import typer

    from skillhub import cli

    commands = typer.main.get_command(cli.app).commands
    seen = set()
    for job in jobs_by_name().values():
        for task in job["tasks"]:
            command, *args = task["python_wheel_task"]["parameters"]
            options = {o for p in commands[command].params for o in (*p.opts, *p.secondary_opts)}
            flags = [a for a in args if a.startswith("--")]
            assert set(flags) <= options, (job["name"], task["task_key"], sorted(set(flags) - options))
            seen |= {(command, f) for f in flags}
    assert ("job-switch", "--run-key") in seen and ("eval", "--versions") in seen


def test_every_job_and_the_app_use_the_workspace_root_variable():
    """Experiments and CI candidates live under var.workspace_root, by default the ci principal's home
    folder, which `users` cannot reach."""
    assert load_bundle()["variables"]["workspace_root"]["default"] == "/Users/${var.ci_principal}/skill-hub"
    for job in jobs_by_name().values():
        variables = {k: v for e in job["environment_variables"] for k, v in e["spec"]["variables"].items()}
        assert variables["SKILLHUB_WORKSPACE_ROOT"] == "${var.workspace_root}", job["name"]
