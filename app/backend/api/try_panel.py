"""POST /api/try: run one prompt with and without a skill, on up to three models, as the signed-in user.

The model calls and the Unity Gateway skills server use the user's token, so Unity Catalog permissions and
gateway limits apply to the user. Only the MLflow trace write uses the app's own identity. The agent loop
belongs to the eval engine and is imported inside the route, so the app still starts without it.

Each agent run is the child of a `hub_try` root span, and its trace is tagged while it is live. One Try run
holds the process lock at a time. At the deadline the request answers 504, the lock is released, and the
worker stops before its next model call or tool call.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import logging
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Annotated, Any
from urllib.parse import quote

from databricks.sdk import WorkspaceClient
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend import authz, skill_ref
from backend.authz import Reason
from backend.deps import get_app_client, get_cfg, get_user_client, get_user_token, problem, workspace_host
from skillhub.config import HubConfig
from skillhub.evalspec import EvalCase
from skillhub.naming import live_schema

log = logging.getLogger("skillhub.app.try")

router = APIRouter(tags=["try"])
MAX_PROMPT_CHARS = 4000
MAX_MODELS = 3
_TRY_LOCK = threading.Lock()  # MLflow's active experiment is process-global
LOCK_WAIT_S = 10.0  # how long a request waits for another Try run before answering 429
DEADLINE_S = 180.0  # overall limit for one request, including the wait for the lock
NO_OP_TRACE_ID = "MLFLOW_NO_OP_SPAN_TRACE_ID"  # the trace ID MLflow gives a span when tracing is off


class TryIn(BaseModel):
    fqn: str = Field(max_length=256)
    prompt: str = Field(min_length=1)
    models: list[str] = Field(min_length=1, max_length=20)


class TryAnswer(BaseModel):
    text: str
    invoked: bool


class TryPlain(BaseModel):
    text: str


class TryModelResult(BaseModel):
    model: str
    with_skill: TryAnswer
    without_skill: TryPlain
    trace_url: str | None


class _Cancelled(Exception):
    """The request timed out; the worker stops at its next step."""


class _Lease:
    """One Try request's hold on _TRY_LOCK, and its cancellation.

    MLflow's active experiment is process-global, so only the lock holder may set it or open a trace. Those
    steps run inside `exclusive()`. At the deadline, `cancel()` stops every later step and releases the lock
    at once; when the worker is inside an exclusive step, the lock is released as soon as that step ends. A
    cancelled worker can still be waiting on one model or skills call. It stops at its next check and opens
    no new trace, so the next request can safely take the lock.
    """

    def __init__(self, lock: threading.Lock):
        self._lock = lock
        self._mu = threading.Lock()
        self._held = True
        self._busy = 0
        self._cancelled = False

    def check(self) -> None:
        if self._cancelled:
            raise _Cancelled

    @contextlib.contextmanager
    def exclusive(self) -> Iterator[None]:
        with self._mu:
            self.check()
            self._busy += 1
        try:
            yield
        finally:
            with self._mu:
                self._busy -= 1
                if self._cancelled and not self._busy:
                    self._release()

    def cancel(self) -> None:
        with self._mu:
            self._cancelled = True
            if not self._busy:
                self._release()

    def release(self) -> None:
        with self._mu:
            self._release()

    def _release(self) -> None:  # the caller holds _mu
        if self._held:
            self._held = False
            self._lock.release()


class _Checked:
    """Wraps an agent-loop collaborator so that each named call first checks for cancellation.

    The agent loop calls the model once per turn, and the skills server or a fixture once per tool call. With
    the model, the skills bridge and the fixtures wrapped, a timed-out run stops between turns and between
    tool calls, and the agent loop itself needs no hook.
    """

    def __init__(self, inner: Any, lease: _Lease, calls: Iterable[str]):
        self._inner = inner
        self._lease = lease
        self._calls = frozenset(calls)

    def __getattr__(self, attr: str) -> Any:
        value = getattr(self._inner, attr)
        if attr not in self._calls:
            return value

        def checked(*args: Any, **kwargs: Any) -> Any:
            self._lease.check()
            return value(*args, **kwargs)

        return checked


def _load_engine() -> SimpleNamespace:
    """Import the eval engine on first use. A missing piece is a 503, not a failed start-up."""
    try:
        import mlflow

        from skillhub.agent.fixtures import FixtureStore
        from skillhub.agent.loop import SkillsBridge, run_agent
        from skillhub.gateway.models import model_client
        from skillhub.gateway.skills_mcp import SkillsMcpClient
        from skillhub.gateway.toolnames import ToolNameMap
        from skillhub.prompts import load_packaged
        from skillhub.results.experiments import ensure_experiment
    except ImportError as exc:
        log.warning("Try panel unavailable: %s", type(exc).__name__)
        raise problem(503, Reason.TRY_UNAVAILABLE) from None
    return SimpleNamespace(
        mlflow=mlflow,
        FixtureStore=FixtureStore,
        SkillsBridge=SkillsBridge,
        run_agent=run_agent,
        model_client=model_client,
        SkillsMcpClient=SkillsMcpClient,
        ToolNameMap=ToolNameMap,
        load_packaged=load_packaged,
        ensure_experiment=ensure_experiment,
    )


def _trace_url(host: str, exp_id: str, trace_id: str) -> str:
    # Same path as traceUrl() in app/frontend/src/links.ts. `host` comes from workspace_host(), with a scheme.
    return f"{host}/ml/experiments/{quote(exp_id, safe='')}/traces?selectedEvaluationId={quote(trace_id, safe='')}"


def _with_host(cfg: HubConfig, host: str) -> HubConfig:
    """The config with the normalised workspace URL. The model client builds its base URL from it, and the raw
    config value can be a bare hostname (DATABRICKS_HOST inside Databricks Apps)."""
    return cfg.model_copy(update={"workspace": cfg.workspace.model_copy(update={"host": host})})


@dataclass(frozen=True)
class _TryRun:
    eng: SimpleNamespace
    cfg: HubConfig  # with the normalised workspace URL (_with_host)
    dept: str
    name: str
    fqn: str
    prompt: str
    headers: Callable[[], dict]
    requester: str
    exp_id: str
    lease: _Lease
    deadline: float  # time.monotonic() value at which the request answers 504


def _trace_id(span: Any) -> str | None:
    trace_id = getattr(span, "trace_id", None)
    if not isinstance(trace_id, str) or not trace_id or trace_id == NO_OP_TRACE_ID:
        return None
    return trace_id


def _traced(run: _TryRun, mode: str, agent: Callable[[], Any]) -> tuple[Any, str | None]:
    """Run `agent()` as the root span of a new trace in the skill's experiment, tagged before any work starts.

    The tags go on the live trace, so they are in place when the agent fails and never race the
    trace export. A tagging fault is logged and never fails the run.
    """
    eng = run.eng
    with contextlib.ExitStack() as stack:
        with run.lease.exclusive():  # a new trace takes the active experiment
            span = stack.enter_context(eng.mlflow.start_span(name="hub_try", span_type="CHAIN"))
        try:
            span.set_inputs({"prompt": run.prompt})
            eng.mlflow.update_current_trace(
                tags={"trigger_source": "hub-try", "mode": mode, "requested_by": run.requester}
            )
        except Exception as exc:
            log.warning("Try trace tagging failed: %s", type(exc).__name__)
        result = agent()
        try:
            span.set_outputs({"final_text": result.final_text})
        except Exception as exc:
            log.warning("Try trace output failed: %s", type(exc).__name__)
    return result, _trace_id(span)


def _close_client(model: Any) -> None:
    """Close the model client's HTTP connections. Best effort: a close error never hides the run's result."""
    try:
        model._client.close()
    except Exception as exc:
        log.warning("closing the model client failed: %s", type(exc).__name__)


def _run_one(run: _TryRun, profile: str) -> dict:
    eng, cfg = run.eng, run.cfg
    # One try per call, and no call outlives the request: the timeout is what is left of the Try budget.
    client = eng.model_client(
        cfg, profile, run.headers, max_tries=1, timeout=max(1.0, run.deadline - time.monotonic())
    )
    try:
        return _run_with(run, profile, client)
    finally:
        _close_client(client)


def _run_with(run: _TryRun, profile: str, client: Any) -> dict:
    eng, cfg, lease = run.eng, run.cfg, run.lease
    host = cfg.workspace.host  # the model client, the skills server and the trace link use the same host
    system_prompt = eng.load_packaged("agent_system")  # a packaged prompt, never skill text
    case = EvalCase(id=0, prompt=run.prompt, should_trigger=True, expected_skill=run.name)
    model = _Checked(client, lease, ("chat",))

    def agent(skills: Any) -> Any:
        return eng.run_agent(
            case,
            model=model,
            skills=skills,
            names=eng.ToolNameMap(),
            fixtures=_Checked(eng.FixtureStore(), lease, ("call",)),
            trigger=False,
            system_prompt=system_prompt,
        )

    schemas = sorted({live_schema(cfg, run.dept), live_schema(cfg, "common")})
    lease.check()
    # The sync bridge runs the async MCP client on its own loop. close() runs even when the agent fails.
    bridge = eng.SkillsBridge(eng.SkillsMcpClient(host, schemas, run.headers))
    try:
        skills = _Checked(bridge, lease, ("list_tools", "call_tool"))
        with_result, trace_with = _traced(run, "with_skill", lambda: agent(skills))
    finally:
        bridge.close()
    without_result, _ = _traced(run, "without_skill", lambda: agent(None))
    return {
        "model": profile,
        "with_skill": {"text": with_result.final_text, "invoked": run.fqn in with_result.invoked_skills},
        "without_skill": {"text": without_result.final_text},
        "trace_url": _trace_url(host, run.exp_id, trace_with) if trace_with else None,
    }


@router.post("/try", response_model=list[TryModelResult])
def try_skill(
    body: TryIn,
    token: Annotated[str, Depends(get_user_token)],
    user_w: Annotated[WorkspaceClient, Depends(get_user_client)],
    app_w: Annotated[WorkspaceClient, Depends(get_app_client)],
    cfg: Annotated[HubConfig, Depends(get_cfg)],
) -> list[dict]:
    # A sync route runs in FastAPI's threadpool, so the blocking bridge never blocks the event loop.
    if len(body.prompt) > MAX_PROMPT_CHARS:
        raise problem(422, Reason.PROMPT_TOO_LONG)
    dept, name = skill_ref.parse_fqn(cfg, body.fqn)
    try:
        authz.authorize_read(user_w, app_w, body.fqn)
    except authz.Forbidden as exc:
        if exc.reason is Reason.SKILL_NOT_READABLE:
            raise problem(404, Reason.SKILL_NOT_FOUND) from None
        raise
    if any(m not in cfg.models for m in body.models):
        raise problem(422, Reason.UNKNOWN_MODEL)
    models = list(dict.fromkeys(body.models))
    if len(models) > MAX_MODELS:
        raise problem(422, Reason.TOO_MANY_MODELS)
    try:
        run_cfg = _with_host(cfg, workspace_host())
    except RuntimeError:  # DATABRICKS_HOST unset: the same answer as the user client gives
        raise problem(403, Reason.AUTHZ_CHECK_FAILED) from None
    eng = _load_engine()
    requester = authz.fetch_me(user_w).user

    def headers() -> dict:
        return {
            "Authorization": f"Bearer {token}"
        }  # the user's token, so the gateway offers only their skills

    deadline = time.monotonic() + DEADLINE_S
    if not _TRY_LOCK.acquire(timeout=LOCK_WAIT_S):
        raise problem(429, Reason.TRY_BUSY)
    lease = _Lease(_TRY_LOCK)
    done: concurrent.futures.Future = concurrent.futures.Future()

    def work() -> None:
        try:
            # ensure_experiment can create the experiment, which also sets the active one: an exclusive step.
            with lease.exclusive():
                exp_id = eng.ensure_experiment(cfg, dept, name)  # the app's identity
                lease.check()
                eng.mlflow.set_experiment(experiment_id=exp_id)
            run = _TryRun(
                eng, run_cfg, dept, name, body.fqn, body.prompt, headers, requester, exp_id, lease, deadline
            )
            out = []
            for m in models:
                lease.check()
                out.append(_run_one(run, m))
            done.set_result(out)
        except BaseException as exc:
            done.set_exception(exc)
        finally:
            lease.release()

    try:
        threading.Thread(target=work, daemon=True, name="try-run").start()
    except BaseException:
        lease.release()
        raise
    finished, _ = concurrent.futures.wait([done], timeout=max(0.0, deadline - time.monotonic()))
    if not finished:
        lease.cancel()  # the worker stops at its next step and closes its bridge; the lock is free now
        raise problem(504, Reason.TRY_TIMEOUT)
    try:
        return done.result()
    except HTTPException:
        raise
    except Exception as exc:  # transport, auth and model errors: log the type only, never raw text
        log.warning("Try run failed: %s", type(exc).__name__)
        raise problem(502, Reason.TRY_FAILED) from None
