import os

import pytest

from skillhub.auth import workspace_client
from skillhub.config import load_config
from skillhub.sql import SqlClient

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("SKILLHUB_IT") != "1", reason="set SKILLHUB_IT=1"),
]


def test_usage_table_has_the_columns_the_dashboard_token_section_reads():
    cfg = load_config()
    sql = SqlClient(workspace_client(), cfg.workspace.sql_warehouse_id)
    cols = {r["col_name"] for r in sql.execute("DESCRIBE TABLE system.serving.endpoint_usage")}
    assert {"served_entity_id", "request_time", "input_token_count", "output_token_count"} <= cols
