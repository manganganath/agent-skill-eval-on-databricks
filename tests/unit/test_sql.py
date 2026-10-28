from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from databricks.sdk.service.sql import StatementState

from skillhub.sql import SqlClient, SqlError


def _resp(state, cols=None, rows=None):
    manifest = None
    if cols is not None:
        manifest = SimpleNamespace(
            schema=SimpleNamespace(columns=[SimpleNamespace(name=c, type_name=t) for c, t in cols])
        )
    result = SimpleNamespace(data_array=rows, next_chunk_index=None) if rows is not None else None
    return SimpleNamespace(
        statement_id="s1",
        status=SimpleNamespace(state=state, error=None),
        manifest=manifest,
        result=result,
    )


def test_params_are_typed_and_pending_statements_are_polled():
    w = MagicMock()
    w.statement_execution.execute_statement.return_value = _resp(StatementState.PENDING)
    w.statement_execution.get_statement.return_value = _resp(
        StatementState.SUCCEEDED, cols=[("v", "INT")], rows=[["7"]]
    )
    rows = SqlClient(w, "wh", poll_s=0).execute("SELECT :v AS v", {"v": 7})
    assert rows == [{"v": 7}]
    kw = w.statement_execution.execute_statement.call_args.kwargs
    assert kw["warehouse_id"] == "wh"
    p = kw["parameters"][0]
    assert (p.name, p.value, p.type) == ("v", "7", "INT")
    w.statement_execution.get_statement.assert_called_once()


def test_failed_statement_raises():
    w = MagicMock()
    failed = _resp(StatementState.FAILED)
    failed.status.error = SimpleNamespace(message="table not found")
    w.statement_execution.execute_statement.return_value = failed
    with pytest.raises(SqlError, match="table not found"):
        SqlClient(w, "wh").execute("SELECT 1")


def test_multiple_chunks_are_all_read():
    w = MagicMock()
    first = _resp(StatementState.SUCCEEDED, cols=[("v", "INT")], rows=[["1"]])
    first.result.next_chunk_index = 1
    second = SimpleNamespace(data_array=[["2"]], next_chunk_index=2)
    third = SimpleNamespace(data_array=[["3"]], next_chunk_index=None)
    w.statement_execution.execute_statement.return_value = first
    w.statement_execution.get_statement_result_chunk_n.side_effect = [second, third]
    rows = SqlClient(w, "wh").execute("SELECT v")
    assert rows == [{"v": 1}, {"v": 2}, {"v": 3}]
    calls = w.statement_execution.get_statement_result_chunk_n.call_args_list
    assert [c.args for c in calls] == [("s1", 1), ("s1", 2)]


def test_truncated_result_raises():
    w = MagicMock()
    r = _resp(StatementState.SUCCEEDED, cols=[("v", "INT")], rows=[["1"]])
    r.manifest.truncated = True
    w.statement_execution.execute_statement.return_value = r
    with pytest.raises(SqlError, match="truncated"):
        SqlClient(w, "wh").execute("SELECT v")


def test_timeout_cancels_the_statement(monkeypatch):
    import skillhub.sql as mod

    now = [0.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(mod.time, "sleep", lambda s: now.__setitem__(0, now[0] + s))
    w = MagicMock()
    w.statement_execution.execute_statement.return_value = _resp(StatementState.RUNNING)
    w.statement_execution.get_statement.return_value = _resp(StatementState.RUNNING)
    with pytest.raises(SqlError, match="did not finish"):
        SqlClient(w, "wh", timeout_s=3, poll_s=1).execute("SELECT 1")
    w.statement_execution.cancel_execution.assert_called_once_with("s1")
    assert w.statement_execution.execute_statement.call_args.kwargs["wait_timeout"] == "5s"


@pytest.mark.parametrize("state", [StatementState.CANCELED, StatementState.CLOSED])
def test_canceled_and_closed_raise(state):
    w = MagicMock()
    w.statement_execution.execute_statement.return_value = _resp(state)
    with pytest.raises(SqlError, match=state.value):
        SqlClient(w, "wh").execute("SELECT 1")


def test_error_with_no_message_still_raises():
    w = MagicMock()
    r = _resp(StatementState.FAILED)
    r.status.error = SimpleNamespace(message=None)
    w.statement_execution.execute_statement.return_value = r
    with pytest.raises(SqlError, match="FAILED"):
        SqlClient(w, "wh").execute("SELECT 1")


def test_missing_status_raises():
    w = MagicMock()
    r = _resp(StatementState.SUCCEEDED)
    r.status = None
    w.statement_execution.execute_statement.return_value = r
    with pytest.raises(SqlError, match="no status"):
        SqlClient(w, "wh").execute("SELECT 1")


def test_parameter_types():
    from datetime import datetime

    from skillhub.sql import _param

    assert _param("a", None).type is None and _param("a", None).value is None
    assert (_param("a", True).type, _param("a", True).value) == ("BOOLEAN", "true")
    assert (_param("a", 1.5).type, _param("a", 1.5).value) == ("DOUBLE", "1.5")
    assert _param("a", 2**31).type == "BIGINT" and _param("a", -(2**31)).type == "INT"
    d = datetime(2026, 1, 2, 3, 4, 5)
    assert (_param("a", d).type, _param("a", d).value) == ("TIMESTAMP", d.isoformat())
    assert _param("a", "x").type == "STRING"
