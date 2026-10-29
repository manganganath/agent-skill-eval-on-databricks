import os

import pytest

from skillhub.auth import workspace_client
from skillhub.config import load_config
from skillhub.results import alerts

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("SKILLHUB_IT") != "1", reason="set SKILLHUB_IT=1"),
]


def test_alerts_are_idempotent_and_queries_run():
    cfg = load_config()
    w = workspace_client()
    first = alerts.ensure_alerts(cfg, w)
    second = alerts.ensure_alerts(cfg, w)
    assert first == second and len(first) == 3
    # the queries must run on the warehouse (they read the views from Task 1)
    from skillhub.sql import SqlClient

    sql = SqlClient(workspace_client(), cfg.workspace.sql_warehouse_id)
    for key, (text, col) in alerts.queries(cfg).items():
        rows = sql.execute(text)
        assert col in rows[0], key
