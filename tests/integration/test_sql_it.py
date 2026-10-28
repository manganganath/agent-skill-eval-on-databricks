import os

import pytest

from skillhub.auth import workspace_client
from skillhub.sql import SqlClient

pytestmark = pytest.mark.integration


def test_parameterised_select_round_trips_types():
    sql = SqlClient(workspace_client(), os.environ["SQL_WAREHOUSE_ID"])
    assert sql.execute("SELECT :n AS n, :s AS s", {"n": 7, "s": "x"}) == [{"n": 7, "s": "x"}]


def test_journal_round_trip_with_null_version():
    from skillhub.config import load_config
    from skillhub.naming import evals_schema
    from skillhub.registry.journal import Journal, new_row

    cfg = load_config("config/hub.yaml")
    sql = SqlClient(workspace_client(), os.environ["SQL_WAREHOUSE_ID"])
    sql.execute(
        f"CREATE TABLE IF NOT EXISTS {evals_schema(cfg, 'common')}.skill_versions (dept STRING, skill STRING, "
        "action STRING, version INT, from_version INT, to_version INT, versioned_fqn STRING, "
        "content_hash STRING, git_sha STRING, eval_run_id STRING, actor STRING, job_run_id STRING, "
        "ts TIMESTAMP) TBLPROPERTIES ('delta.appendOnly' = 'true')"
    )
    j = Journal(cfg, sql)
    skill = f"it-journal-{os.urandom(4).hex()}"
    j.append(new_row("common", skill, "drift", actor="it", content_hash="sha256:x"))  # version is NULL
    hist = j.history("common", skill)
    assert len(hist) == 1 and hist[0].version is None and hist[0].action == "drift"
