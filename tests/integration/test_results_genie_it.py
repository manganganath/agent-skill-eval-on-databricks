import os

import pytest

from skillhub.auth import workspace_client
from skillhub.config import load_config
from skillhub.naming import eval_schema
from skillhub.results import genie
from skillhub.sql import SqlClient

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("SKILLHUB_IT") != "1", reason="set SKILLHUB_IT=1"),
]


def test_genie_space_is_idempotent_and_sample_sql_runs():
    cfg = load_config()
    w = workspace_client()
    first = genie.ensure_genie_space(cfg, w)
    assert genie.ensure_genie_space(cfg, w) == first
    sql = SqlClient(w, cfg.workspace.sql_warehouse_id)
    for _, text in genie.SAMPLE_QUESTIONS:
        sql.execute(text.replace("{ev}", eval_schema(cfg)))
