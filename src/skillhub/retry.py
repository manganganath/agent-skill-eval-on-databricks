"""Which errors are worth another attempt, and a jittered exponential backoff to make it.

A rate limit or a briefly unavailable service (HTTP 429, 503, 504) is transient. So is a Delta write conflict
between two writers of one table, for a write that checks again what is there (insert unless it exists).
A library often wraps the error it got, so the whole cause and context chain is checked.
"""

from __future__ import annotations

import logging
import random
import re
import time
from collections.abc import Callable, Iterator
from typing import TypeVar

from databricks.sdk.errors import DeadlineExceeded, TemporarilyUnavailable, TooManyRequests
from mlflow.exceptions import MlflowException

log = logging.getLogger(__name__)

T = TypeVar("T")

TRANSIENT_STATUSES = frozenset({429, 503, 504})
TRANSIENT_CODES = frozenset({"REQUEST_LIMIT_EXCEEDED", "TEMPORARILY_UNAVAILABLE"})
# "429" as a word, so a count such as 1429 is not a status
_TRANSIENT_TEXT = re.compile(r"\b429\b|Too Many Requests|TEMPORARILY_UNAVAILABLE|REQUEST_LIMIT_EXCEEDED")
_CONFLICT_TEXT = re.compile(r"DELTA_CONCURRENT_APPEND|ConcurrentAppendException|Transaction conflict")


def _chain(exc: BaseException) -> Iterator[BaseException]:
    """The error, then everything in its cause and context chain, each once."""
    seen: set[int] = set()
    todo = [exc]
    while todo:
        e = todo.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        yield e
        todo += [e.__context__, e.__cause__]


def _status(e: BaseException) -> int | None:
    if isinstance(e, MlflowException):
        return e.get_http_status_code()
    for owner in (e, getattr(e, "response", None)):  # requests and httpx errors carry their response
        status = getattr(owner, "status_code", None)
        if isinstance(status, int):
            return status
    return None


def _transient(e: BaseException) -> bool:
    return (
        isinstance(e, (TooManyRequests, TemporarilyUnavailable, DeadlineExceeded))
        or getattr(e, "error_code", None) in TRANSIENT_CODES
        or _status(e) in TRANSIENT_STATUSES
        or bool(_TRANSIENT_TEXT.search(str(e)))
    )


def is_transient(exc: BaseException) -> bool:
    """A rate limit or an unavailable service anywhere in the error's chain."""
    return any(_transient(e) for e in _chain(exc))


def is_write_conflict(exc: BaseException) -> bool:
    """A Delta transaction conflict (a concurrent append) anywhere in the error's chain."""
    return any(_CONFLICT_TEXT.search(str(e)) for e in _chain(exc))


def backoff_s(attempt: int, *, base_s: float, cap_s: float) -> float:
    """The wait after failed attempt `attempt` (1-based): base * 2^(attempt-1), capped, then 50-100% of it."""
    return min(cap_s, base_s * 2 ** (attempt - 1)) * (0.5 + random.random() / 2)


def retrying(
    fn: Callable[[], T],
    *,
    attempts: int,
    base_s: float,
    cap_s: float,
    retry_on: Callable[[BaseException], bool],
    what: str,
    sleep: Callable[[float], object] = time.sleep,
) -> T:
    """`fn()`, called again after a backoff while it raises an error `retry_on` accepts, up to `attempts` calls.

    Each retry logs one warning. The last attempt's error, or one `retry_on` refuses, is raised as it is."""
    for attempt in range(1, attempts):
        try:
            return fn()
        except Exception as e:
            if not retry_on(e):
                raise
            delay = backoff_s(attempt, base_s=base_s, cap_s=cap_s)
            first_line = next(iter(str(e).splitlines()), "")[:200]  # a response body can follow the status
            log.warning(
                "%s: attempt %d of %d failed (%s: %s); retrying in %.1fs",
                what,
                attempt,
                attempts,
                type(e).__name__,
                first_line,
                delay,
            )
            sleep(delay)
    return fn()
