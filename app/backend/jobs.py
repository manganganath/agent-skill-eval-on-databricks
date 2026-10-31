"""Job lookup and start. Always called with the app service principal's client: users never start Jobs."""

from __future__ import annotations

import os
import threading

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import InvalidParameterValue, NotFound

JOB_NAMES = ("skill-eval", "skill-switch", "skillhub-probe")
# The env var wins when it is set and non-empty. Otherwise the job is found by exact name.
_ENV = {
    "skill-eval": "SKILLHUB_JOB_ID_EVAL",
    "skill-switch": "SKILLHUB_JOB_ID_SWITCH",
    "skillhub-probe": "SKILLHUB_JOB_ID_PROBE",
}
_ids: dict[str, int] = {}
_lock = threading.Lock()


class JobNotFound(Exception):
    pass


def clear_cache() -> None:
    with _lock:
        _ids.clear()


def _resolve(w: WorkspaceClient, name: str) -> tuple[int, bool]:
    """(Job ID, whether it came from the name cache)."""
    if name not in JOB_NAMES:
        raise ValueError(f"unknown job {name}")
    configured = os.environ.get(_ENV[name], "").strip()
    if configured:
        try:
            return int(configured), False
        except ValueError:
            raise JobNotFound(name) from None  # a malformed id is a configuration fault: refuse, do not guess
    with _lock:
        cached = _ids.get(name)
    if cached is not None:
        return cached, True
    matches = [j for j in w.jobs.list(name=name) if j.settings and j.settings.name == name]
    if len(matches) != 1:
        raise JobNotFound(name)
    found = int(matches[0].job_id)
    with _lock:
        _ids[name] = found
    return found, False


def job_id(w: WorkspaceClient, name: str) -> int:
    return _resolve(w, name)[0]


def _forget(name: str) -> None:
    with _lock:
        _ids.pop(name, None)


def start_job(w: WorkspaceClient, name: str, params: dict[str, str]) -> int:
    """Start the Job. A cached id can go stale (the Job was recreated), and the Jobs API answers a missing Job
    with NOT_FOUND or INVALID_PARAMETER_VALUE. For a cached id, either error clears the cache entry and the
    lookup runs once more. A configured or freshly looked-up id is not retried."""
    jid, cached = _resolve(w, name)
    try:
        waiter = w.jobs.run_now(job_id=jid, job_parameters=params)
    except (NotFound, InvalidParameterValue):
        if not cached:
            raise
        _forget(name)
        waiter = w.jobs.run_now(job_id=job_id(w, name), job_parameters=params)
    return int(waiter.response.run_id)


def active_runs(w: WorkspaceClient, name: str) -> list[dict[str, str]]:
    """Job parameters of each run of the Job that has not finished (queued, pending or running)."""
    runs = w.jobs.list_runs(job_id=job_id(w, name), active_only=True)
    return [{p.name: p.value for p in (r.job_parameters or [])} for r in runs]
