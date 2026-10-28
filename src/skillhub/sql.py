from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol

from databricks.sdk.service.sql import (
    ExecuteStatementRequestOnWaitTimeout,
    StatementParameterListItem,
    StatementState,
)

from skillhub import SkillHubError


class SqlError(SkillHubError):
    """A SQL statement failed or did not finish in time."""


class SqlExecutor(Protocol):
    def execute(self, statement: str, params: Mapping[str, object] | None = None) -> list[dict[str, Any]]: ...


def _param(name: str, v: object) -> StatementParameterListItem:
    if v is None:
        return StatementParameterListItem(
            name=name, value=None
        )  # untyped NULL; CAST it in the SQL for typed columns
    if isinstance(v, bool):
        return StatementParameterListItem(name=name, value="true" if v else "false", type="BOOLEAN")
    if isinstance(v, int):
        t = "INT" if -(2**31) <= v < 2**31 else "BIGINT"
        return StatementParameterListItem(name=name, value=str(v), type=t)
    if isinstance(v, float):
        return StatementParameterListItem(name=name, value=repr(v), type="DOUBLE")
    if isinstance(v, datetime):
        return StatementParameterListItem(name=name, value=v.isoformat(), type="TIMESTAMP")
    return StatementParameterListItem(name=name, value=str(v), type="STRING")


def _coerce(type_name: object, value: str | None) -> object:
    if value is None:
        return None
    t = str(getattr(type_name, "value", type_name)).upper()
    if t in ("INT", "LONG", "SHORT", "BYTE"):
        return int(value)
    if t in ("DOUBLE", "FLOAT"):
        return float(value)
    if t == "BOOLEAN":
        return value.lower() == "true"
    return value


class SqlClient:
    """Runs statements on a SQL warehouse through the statement execution API.

    Parameters use :name markers and are typed from their Python type. Waits for the statement to
    finish (polling every poll_s seconds) and raises SqlError on failure or timeout.
    """

    def __init__(self, w, warehouse_id: str, *, timeout_s: float = 900, poll_s: float = 1.0) -> None:
        self._w = w
        self._wh = warehouse_id
        self._timeout = timeout_s
        self._poll = poll_s

    def _cancel(self, statement_id: str | None) -> None:
        try:
            if statement_id:
                self._w.statement_execution.cancel_execution(statement_id)
        except Exception:  # best effort: the timeout error is what matters
            pass

    def execute(self, statement: str, params: Mapping[str, object] | None = None) -> list[dict[str, Any]]:
        plist = [_param(k, v) for k, v in (params or {}).items()] or None
        deadline = time.monotonic() + self._timeout
        first_wait = max(5, min(50, int(self._timeout)))
        resp = self._w.statement_execution.execute_statement(
            statement=statement,
            warehouse_id=self._wh,
            parameters=plist,
            wait_timeout=f"{first_wait}s",
            on_wait_timeout=ExecuteStatementRequestOnWaitTimeout.CONTINUE,
        )
        while resp.status is not None and resp.status.state in (
            StatementState.PENDING,
            StatementState.RUNNING,
        ):
            if time.monotonic() >= deadline:
                self._cancel(resp.statement_id)
                raise SqlError(f"statement {resp.statement_id} did not finish within {self._timeout}s")
            time.sleep(self._poll)
            resp = self._w.statement_execution.get_statement(resp.statement_id)
        if resp.status is None:
            raise SqlError(f"statement {resp.statement_id} returned no status")
        if resp.status.state != StatementState.SUCCEEDED:
            err = resp.status.error
            msg = (err.message if err else None) or str(
                getattr(resp.status.state, "value", resp.status.state)
            )
            raise SqlError(f"statement failed: {msg}")
        if resp.manifest is None or resp.result is None:
            return []
        if getattr(resp.manifest, "truncated", False):
            raise SqlError("result was truncated by the server; narrow the query")
        data = list(resp.result.data_array or [])
        chunk = resp.result
        while chunk.next_chunk_index is not None:
            chunk = self._w.statement_execution.get_statement_result_chunk_n(
                resp.statement_id, chunk.next_chunk_index
            )
            data.extend(chunk.data_array or [])
        cols = resp.manifest.schema.columns
        return [{c.name: _coerce(c.type_name, v) for c, v in zip(cols, row, strict=True)} for row in data]
