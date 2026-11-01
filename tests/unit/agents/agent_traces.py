"""Agent-shaped traces in the test MLflow store, built from recorded span trees. Not a test module.

A recorded tree is {"name", "attributes", "span_type"?, "children"?}: the span names and attributes that an agent's
OpenTelemetry export carries, as MLflow reads them back from the trace table.
"""

from __future__ import annotations

import json
from pathlib import Path

import mlflow

RECORDED = Path(__file__).with_name("recorded")


def recorded(name: str) -> dict:
    return json.loads((RECORDED / name).read_text(encoding="utf-8"))


def _span(node: dict) -> None:
    with mlflow.start_span(name=node["name"], span_type=node.get("span_type", "UNKNOWN")) as span:
        span.set_attributes(node.get("attributes") or {})
        for child in node.get("children") or []:
            _span(child)


def build_trace(tree: dict) -> str:
    """Write the tree as one trace and return its ID."""
    _span(tree)
    trace_id = mlflow.get_last_active_trace_id()
    mlflow.flush_trace_async_logging()
    return trace_id


def load(tree: dict):
    return mlflow.get_trace(build_trace(tree))
