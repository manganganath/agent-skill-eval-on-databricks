"""The model catalog as the Skill Hub serves it. The rules and the SQL are in skillhub.model_catalog.

Reads and writes run as the app principal (deps.get_app_sql): users have no grant on the table, and every signed-in
user may read the list. The API writes only after its reviewer check. The app never calls a catalog model itself: a
connection test is the `skillhub-probe` Job, which runs as skillhub-ci-eval, and the app only reads its result.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any, Literal, TypeVar

from databricks.sdk.errors import DatabricksError

from backend.authz import Reason
from backend.deps import problem
from backend.schemas import ModelOut, ModelTestOut
from skillhub.config import HubConfig
from skillhub.model_catalog import store
from skillhub.model_catalog.entry import CatalogEntry
from skillhub.probe import MESSAGE_MAX, parse_result
from skillhub.sql import SqlExecutor

log = logging.getLogger("skillhub.app.models")
T = TypeVar("T")  # a TypeVar, not PEP 695 syntax: the Databricks Apps runtime is Python 3.11
TERMINAL = frozenset({"TERMINATED", "SKIPPED", "INTERNAL_ERROR"})
NO_RESULT = "The test ended before it reported a result. Open the skillhub-probe run in Jobs for its log."


class CatalogUnavailable(Exception):
    """The catalog table cannot be read: it is not bootstrapped yet, or the warehouse failed."""


def read(sql: SqlExecutor, cfg: HubConfig) -> list[CatalogEntry]:
    try:
        return store.read_entries(sql, cfg)[0]
    except Exception as exc:  # the type only: a warehouse message could quote the statement
        log.warning("model catalog read failed: %s", type(exc).__name__)
        raise CatalogUnavailable() from None


def read_or_503(sql: SqlExecutor, cfg: HubConfig) -> list[CatalogEntry]:
    try:
        return read(sql, cfg)
    except CatalogUnavailable:
        raise problem(503, Reason.CATALOG_UNAVAILABLE) from None


def write_or_503(write: Callable[[], T]) -> T:
    """Run one catalog write. A failure is 503 catalog_unavailable; its message is not kept."""
    try:
        return write()
    except Exception as exc:
        log.warning("model catalog write failed: %s", type(exc).__name__)
        raise problem(503, Reason.CATALOG_UNAVAILABLE) from None


def find(entries: Iterable[CatalogEntry], name: str) -> CatalogEntry | None:
    return next((e for e in entries if e.name == name), None)


def require_runnable(sql: SqlExecutor, cfg: HubConfig, names: list[str]) -> None:
    """Refuse a model that is not a hub.yaml profile or an enabled catalog model: 404 model_not_found. The catalog is
    read only when a name is not in hub.yaml, so a run on hub.yaml models never depends on it."""
    extra = [n for n in names if n not in cfg.models]
    if not extra:
        return
    known = {e.name: e for e in read_or_503(sql, cfg)}
    if any(n not in known or not known[n].enabled or not known[n].runnable for n in extra):
        raise problem(404, Reason.MODEL_NOT_FOUND)


def hub_rows(cfg: HubConfig) -> list[ModelOut]:
    """The hub.yaml profiles: the release-gate models, listed first and changed only in hub.yaml."""
    return [
        ModelOut(
            name=name,
            kind="hub_yaml",
            route=p.route,
            endpoint=p.endpoint or p.model,
            model_id=None,
            region=None,
            model_class=p.class_,
            harness="reference",
            enabled=True,
            release_gate=True,
        )
        for name, p in cfg.models.items()
    ]


def entry_out(e: CatalogEntry) -> ModelOut:
    test = e.last_test
    return ModelOut(
        name=e.name,
        kind=e.kind,
        route=e.route,
        endpoint=e.endpoint,
        model_id=e.model_id,
        region=e.region,
        model_class=e.model_class,
        harness=e.harness,
        enabled=e.enabled,
        release_gate=False,
        price_in=e.price_in,
        price_out=e.price_out,
        tested_at=e.tested_at,
        test=ModelTestOut(status=test.status, message=test.message, job_run_id=test.job_run_id)
        if test
        else None,
        updated_by=e.updated_by,
        updated_at=e.updated_at,
    )


_ORDER = {"ug_model": 0, "external_model": 1, "external_agent": 2}


def catalog_rows(entries: Iterable[CatalogEntry]) -> list[ModelOut]:
    """Grouped as the Models page shows them: on Unity Gateway, external models, external agents; by name."""
    return [entry_out(e) for e in sorted(entries, key=lambda e: (_ORDER.get(e.kind, 9), e.name))]


def is_finished(run: Any) -> bool:
    state = getattr(run, "state", None)
    life = getattr(getattr(state, "life_cycle_state", None), "value", None)
    return getattr(state, "result_state", None) is not None or life in TERMINAL


def outcome(app_w: Any, run: Any) -> tuple[Literal["passed", "failed"], str]:
    """The finished probe run's result: from its result line when the output can be read, else from its state."""
    passed = getattr(getattr(run.state, "result_state", None), "value", None) == "SUCCESS"
    tasks = getattr(run, "tasks", None) or []
    task_run = getattr(tasks[0], "run_id", None) if tasks else None
    try:
        logs = (
            app_w.jobs.get_run_output(run_id=task_run or run.run_id).logs
            if (task_run or run.run_id)
            else None
        )
    except DatabricksError as exc:
        log.warning("probe output unavailable: %s", type(exc).__name__)
        logs = None
    result = parse_result(logs)
    if result is not None and result.status == ("passed" if passed else "failed"):
        return result.status, result.message[:MESSAGE_MAX]
    return ("passed", "The test passed.") if passed else ("failed", NO_RESULT)


def finished_at(run: Any) -> datetime:
    end = getattr(run, "end_time", None)
    return datetime.fromtimestamp(end / 1000, tz=UTC) if end else datetime.now(UTC)
