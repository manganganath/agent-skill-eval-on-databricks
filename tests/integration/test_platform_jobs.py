"""Dev integration checks for the platform Jobs. Run after `databricks bundle deploy -t dev`.

Everything comes from the shell, nothing is committed:
  SKILLHUB_IT=1, DATABRICKS_CONFIG_PROFILE (or DATABRICKS_HOST + OAuth M2M variables),
  SKILLHUB_JOB_ID_EVAL / SKILLHUB_JOB_ID_SWITCH (dev Job IDs from the deploy),
  CI_PRINCIPAL_NAME, CI_EVAL_PRINCIPAL_NAME, APP_PRINCIPAL_NAME (application IDs, as the ACL returns them),
  and for the run check: SQL_WAREHOUSE_ID, SKILLHUB_CATALOG (dev catalog), SKILLHUB_IT_SKILL (dept/name).
"""

from __future__ import annotations

import os
import uuid
from datetime import timedelta

import pytest

from skillhub.auth import workspace_client

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("SKILLHUB_IT") != "1", reason="set SKILLHUB_IT=1 to run"),
]


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} is not set")
    return value


def _runners(job_id: str) -> set[str]:
    """Principals holding CAN_MANAGE_RUN (as granted by the bundle) on a Job."""
    perms = workspace_client().permissions.get("jobs", job_id)
    return {
        acl.service_principal_name or acl.user_name or acl.group_name
        for acl in perms.access_control_list or []
        for p in acl.all_permissions or []
        if str(p.permission_level.value if p.permission_level else "") == "CAN_MANAGE_RUN" and not p.inherited
    }


def test_skill_eval_is_runnable_only_by_the_eval_and_app_principals():
    """Spec 11: a principal outside this set cannot start skill-eval."""
    expected = {_env("CI_EVAL_PRINCIPAL_NAME"), _env("APP_PRINCIPAL_NAME")}
    assert _runners(_env("SKILLHUB_JOB_ID_EVAL")) == expected


def test_skill_switch_is_runnable_only_by_the_ci_and_app_principals():
    """Spec 11: the eval principal cannot start skill-switch."""
    expected = {_env("CI_PRINCIPAL_NAME"), _env("APP_PRINCIPAL_NAME")}
    assert _runners(_env("SKILLHUB_JOB_ID_SWITCH")) == expected


def test_skill_eval_run_succeeds_and_writes_a_dev_eval_runs_row():
    """Start skill-eval for one skill and the default model, then find its row in the dev catalog."""
    from skillhub.config import load_config
    from skillhub.naming import evals_schema, make_run_key
    from skillhub.sql import SqlClient

    _env("SKILLHUB_CATALOG")  # hub.yaml reads the catalog from it
    w = workspace_client()
    dept, skill = _env("SKILLHUB_IT_SKILL").split("/", 1)
    label = f"manual-{uuid.uuid4().hex[:8]}"  # the Job applies make_run_key to the label
    run = w.jobs.run_now(
        int(_env("SKILLHUB_JOB_ID_EVAL")),
        job_parameters={
            "run_key": label,
            "skills": f"{dept}/{skill}",
            "models": "default",
            "modes": "trigger",
            "source": "live",
            "trigger_source": "manual",
            "requested_by": "integration-test",
        },
    ).result(timeout=timedelta(minutes=30))
    assert run.state.result_state.value == "SUCCESS", run.run_page_url

    cfg = load_config("config/hub.yaml")
    assert cfg.catalog.endswith("_dev"), "run this check against the dev target only"
    sql = SqlClient(w, _env("SQL_WAREHOUSE_ID"))
    rows = sql.execute(
        f"SELECT run_key FROM {evals_schema(cfg, dept)}.eval_runs WHERE run_key = :run_key",
        {"run_key": make_run_key(label)},
    )
    assert rows, f"no eval_runs row for {make_run_key(label)} in {cfg.catalog}"
