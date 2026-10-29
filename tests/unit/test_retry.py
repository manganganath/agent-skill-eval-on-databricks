import logging

import pytest
import requests
from databricks.sdk.errors import NotFound, TemporarilyUnavailable, TooManyRequests
from mlflow.exceptions import MlflowException, RestException
from mlflow.protos.databricks_pb2 import (
    INVALID_PARAMETER_VALUE,
    REQUEST_LIMIT_EXCEEDED,
    TEMPORARILY_UNAVAILABLE,
)

from skillhub import retry
from skillhub.sql import SqlError


def http_error(status: int) -> requests.HTTPError:
    resp = requests.Response()
    resp.status_code = status
    return requests.HTTPError(f"{status} Error", response=resp)


def chained(outer: Exception, inner: Exception, *, cause: bool = True) -> Exception:
    """`outer` raised while handling `inner`: `raise outer from inner` (cause) or inside `except` (context)."""
    try:
        try:
            raise inner
        except Exception as e:
            if cause:
                raise outer from e
            raise outer  # noqa: B904 - the implicit context is the point
    except Exception as got:
        return got


@pytest.mark.parametrize(
    "exc",
    [
        http_error(429),
        http_error(503),
        http_error(504),
        requests.HTTPError("429 Client Error: Too Many Requests\n{}. "),  # the managed-evals client's text
        TooManyRequests("slow down"),
        TemporarilyUnavailable("busy"),
        MlflowException("limited", error_code=REQUEST_LIMIT_EXCEEDED),
        MlflowException("busy", error_code=TEMPORARILY_UNAVAILABLE),
        RestException({"error_code": "REQUEST_LIMIT_EXCEEDED", "message": "slow down"}),
        RestException({"error_code": "DEADLINE_EXCEEDED", "message": "gateway timeout"}),
        RuntimeError("API request failed with status 429"),
        RuntimeError("Too Many Requests"),
        RuntimeError("TEMPORARILY_UNAVAILABLE: try later"),
        SqlError("statement failed: REQUEST_LIMIT_EXCEEDED"),
    ],
    ids=repr,
)
def test_rate_limits_and_unavailable_services_are_transient(exc):
    assert retry.is_transient(exc)


@pytest.mark.parametrize(
    "exc",
    [
        http_error(400),
        http_error(500),
        requests.HTTPError("404 Client Error: Not Found"),
        NotFound("Table cat.s.t does not exist"),
        MlflowException("bad", error_code=INVALID_PARAMETER_VALUE),
        MlflowException("an internal error"),  # INTERNAL_ERROR, 500
        RestException({"error_code": "RESOURCE_DOES_NOT_EXIST", "message": "gone"}),
        ValueError("rubric.json is for skill 'x'"),
        RuntimeError("processed 1429 rows"),  # 429 inside a number is not a status
        FileNotFoundError("fixtures/northwind/model_bugs.json"),
    ],
    ids=repr,
)
def test_other_errors_are_not_transient(exc):
    assert not retry.is_transient(exc)


@pytest.mark.parametrize("cause", [True, False])
def test_a_rate_limit_in_the_cause_or_context_chain_is_transient(cause):
    wrapped = chained(ValueError("scorers could not be built"), http_error(429), cause=cause)
    assert retry.is_transient(wrapped)
    assert not retry.is_transient(chained(ValueError("outer"), KeyError("inner"), cause=cause))


def test_a_cyclic_chain_ends():
    a, b = ValueError("a"), ValueError("b")
    a.__cause__, b.__cause__ = b, a
    assert not retry.is_transient(a)


@pytest.mark.parametrize(
    "exc",
    [
        SqlError(
            "statement failed: [DELTA_CONCURRENT_APPEND.WHOLE_TABLE_READ] Transaction conflict detected. "
            "A concurrent WRITE added data to table cat.common_evals.rubrics"
        ),
        RuntimeError("io.delta.exceptions.ConcurrentAppendException: Files were added"),
        RuntimeError("Transaction conflict detected"),
        chained(RuntimeError("record failed"), SqlError("[DELTA_CONCURRENT_APPEND] conflict")),
    ],
    ids=repr,
)
def test_concurrent_delta_writes_are_write_conflicts(exc):
    assert retry.is_write_conflict(exc)


@pytest.mark.parametrize(
    "exc",
    [SqlError("statement failed: [TABLE_OR_VIEW_NOT_FOUND]"), http_error(429), ValueError("bad row")],
    ids=repr,
)
def test_other_errors_are_not_write_conflicts(exc):
    assert not retry.is_write_conflict(exc)


def test_backoff_grows_exponentially_with_jitter_up_to_the_cap(monkeypatch):
    monkeypatch.setattr(retry.random, "random", lambda: 1.0)  # the top of the jitter
    assert [retry.backoff_s(a, base_s=2, cap_s=60) for a in range(1, 7)] == [2, 4, 8, 16, 32, 60]
    monkeypatch.setattr(retry.random, "random", lambda: 0.0)  # the bottom: half
    assert [retry.backoff_s(a, base_s=2, cap_s=60) for a in (1, 6)] == [1, 30]


def _flaky(errors):
    calls = []

    def fn():
        calls.append(len(calls))
        if errors:
            raise errors.pop(0)
        return "done"

    return fn, calls


def test_retrying_calls_again_after_a_retryable_error_and_logs_each_retry(caplog):
    slept = []
    fn, calls = _flaky([http_error(429), http_error(503)])
    with caplog.at_level(logging.WARNING, logger="skillhub.retry"):
        got = retry.retrying(
            fn,
            attempts=6,
            base_s=2,
            cap_s=60,
            retry_on=retry.is_transient,
            what="x setup",
            sleep=slept.append,
        )
    assert got == "done" and len(calls) == 3 and len(slept) == 2
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 2 and all(w.startswith("x setup: ") for w in warnings)
    assert "attempt 1 of 6" in warnings[0] and "attempt 2 of 6" in warnings[1]


def test_retrying_raises_the_last_error_after_the_last_attempt():
    slept = []
    fn, calls = _flaky([http_error(429) for _ in range(3)])
    with pytest.raises(requests.HTTPError):
        retry.retrying(
            fn, attempts=3, base_s=2, cap_s=60, retry_on=retry.is_transient, what="x", sleep=slept.append
        )
    assert len(calls) == 3 and len(slept) == 2


def test_retrying_raises_an_error_it_must_not_retry_at_once():
    slept = []
    fn, calls = _flaky([ValueError("bad")])
    with pytest.raises(ValueError):
        retry.retrying(
            fn, attempts=6, base_s=2, cap_s=60, retry_on=retry.is_transient, what="x", sleep=slept.append
        )
    assert len(calls) == 1 and slept == []
