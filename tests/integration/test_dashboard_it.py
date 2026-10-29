import json
import os

import pytest

from skillhub.auth import workspace_client
from skillhub.config import load_config
from skillhub.results.dashboard import build
from skillhub.sql import SqlClient

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("SKILLHUB_IT") != "1", reason="set SKILLHUB_IT=1"),
]


def test_usage_table_has_the_columns_the_cost_page_reads():
    cfg = load_config()
    sql = SqlClient(workspace_client(), cfg.workspace.sql_warehouse_id)
    cols = {r["col_name"] for r in sql.execute("DESCRIBE TABLE system.serving.endpoint_usage")}
    assert {"endpoint_name", "request_time", "input_token_count", "output_token_count"} <= cols


def test_workspace_accepts_the_dashboard_json():
    cfg = load_config()
    w = workspace_client()
    from databricks.sdk.service.dashboards import Dashboard

    created = w.lakeview.create(
        Dashboard(
            display_name="skillhub-it-probe",
            serialized_dashboard=json.dumps(build.build()),
            warehouse_id=cfg.workspace.sql_warehouse_id,
        ),
        dataset_catalog=cfg.catalog,
    )
    try:
        back = json.loads(w.lakeview.get(created.dashboard_id).serialized_dashboard)
        assert [p["name"] for p in back["pages"]] == [p["name"] for p in build.build()["pages"]]
    finally:
        w.workspace.delete(created.path)
