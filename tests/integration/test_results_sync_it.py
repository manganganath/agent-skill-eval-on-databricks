import os

import pytest

from skillhub.auth import workspace_client
from skillhub.config import load_config
from skillhub.results import sync as sync_mod
from skillhub.sql import SqlClient

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("SKILLHUB_IT") != "1", reason="set SKILLHUB_IT=1"),
    pytest.mark.skipif(not os.environ.get("SKILLHUB_IT_RUN_KEY"), reason="set SKILLHUB_IT_RUN_KEY"),
]


def _setup():
    cfg = load_config()
    return (
        os.environ["SKILLHUB_IT_RUN_KEY"],
        cfg,
        SqlClient(workspace_client(), cfg.workspace.sql_warehouse_id),
    )


def test_sync_is_idempotent_for_a_finished_run():
    """Needs a finished run: SKILLHUB_IT_RUN_KEY is the run_key of a 1-skill x 1-model trigger eval."""
    key, cfg, sql = _setup()
    sync_mod.sync_run(cfg, sql, key, timeout_s=600)
    second = sync_mod.sync_run(cfg, sql, key, timeout_s=600)
    assert second.runs_inserted == 0 and second.cases_inserted == 0


def test_rebuild_adds_nothing_twice():
    key, cfg, sql = _setup()
    sync_mod.sync_run(cfg, sql, key, timeout_s=600)
    again = sync_mod.sync_run(cfg, sql, key, timeout_s=600, rebuild=True)
    assert again.runs_inserted == 0 and again.cases_inserted == 0
