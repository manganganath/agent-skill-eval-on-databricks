from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import mlflow
from mlflow.entities import SpanType

from skillhub.agent.fixtures import FAKE_TOOLS, FixtureStore
from skillhub.evalspec import EvalCase
from skillhub.gateway.models import ModelClient, ToolCall, ToolSpec
from skillhub.gateway.skills_mcp import SkillToolError
from skillhub.gateway.toolnames import ToolNameMap

SKILL_PREFIX = "skill_"
TRIGGER_MAX_TOKENS = 64


@dataclass
class AgentResult:
    final_text: str
    invoked_skills: list[str]  # production FQNs, in call order
    tool_calls: list[ToolCall]
    turns: int
    error: str | None


class SkillsBridge:
    """Runs an async skills client on a private event loop so the synchronous agent loop can call it.

    The client is entered and exited inside one long-lived Task, because anyio task groups (used by the
    MCP session) must be exited in the Task that entered them.
    """

    TIMEOUT = 60.0
    CLEANUP_TIMEOUT = 5.0  # how long a cancelled open may take to close what it opened

    def __init__(self, client: Any, *, timeout: float | None = None):
        self._client = client
        self._timeout = timeout or self.TIMEOUT
        self._closed = False
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True, name="skills-bridge")
        self._thread.start()
        self._ready: concurrent.futures.Future = concurrent.futures.Future()
        self._stop: asyncio.Event | None = None
        self._finished = threading.Event()  # set when the lifecycle task has run its cleanup
        self._lifecycle = asyncio.run_coroutine_threadsafe(self._lifecycle_task(), self._loop)
        try:
            self._ready.result(timeout=self._timeout)
        except BaseException:
            self._closed = True
            self._lifecycle.cancel()
            # Let the cancelled open close what it opened (the MCP HTTP session) before the loop stops.
            self._finished.wait(timeout=self.CLEANUP_TIMEOUT)
            self._stop_loop()
            raise

    async def _lifecycle_task(self) -> None:
        self._stop = asyncio.Event()
        try:
            async with self._client:
                self._ready.set_result(None)
                await self._stop.wait()
        except BaseException as e:
            if not self._ready.done():
                self._ready.set_exception(e)
            else:
                raise
        finally:
            self._finished.set()

    def _run(self, coro):
        if self._closed:
            coro.close()
            raise RuntimeError("SkillsBridge is closed")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=self._timeout)

    def _stop_loop(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        if not self._thread.is_alive():
            self._loop.close()

    def list_tools(self):
        return self._run(self._client.list_tools())

    def call_tool(self, name: str, arguments: dict) -> str:
        return self._run(self._client.call_tool(name, arguments))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._loop.call_soon_threadsafe(self._stop.set)
            self._lifecycle.result(timeout=self._timeout)
        finally:
            self._stop_loop()

    def __enter__(self) -> SkillsBridge:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _schema_of(arguments: dict) -> str:
    """The `<catalog>.<schema>` that a generic tool's arguments name: from `full_name`, else from `schema`."""
    full = str(arguments.get("full_name") or "")
    return (full.rpartition(".")[0] if full else str(arguments.get("schema") or "")).casefold()


class MultiSkillsBridge:
    """One SkillsBridge per skills MCP connection, used as one: the agent loop sees the union of their tools.

    The gateway offers at most MAX_SKILL_TOOLS per-skill tools per connection, so a run opens one connection per
    schema. A per-skill tool is called on the connection that listed it. A generic tool (load_skill,
    get_skill_files, list_skills) is listed by every connection: it is offered once, and called on the connection
    of the schema its arguments name, else on the first.

    `hidden` holds staged FQNs that the run leaves out: their tools are not listed, and a call to one is refused
    as a tool error. Every connection is opened or none is: a failure closes the ones that opened.
    """

    def __init__(
        self,
        schemas: list[str],
        connect: Callable[[str], Any],
        *,
        timeout: float | None = None,
        hidden: Iterable[str] = (),
    ):
        if not schemas:
            raise ValueError("MultiSkillsBridge needs at least one schema")
        self._schemas = list(schemas)
        self._hidden = frozenset(f.casefold() for f in hidden)
        self._hidden_tools = frozenset(f"skill_{f}" for f in self._hidden)
        self._bridges: list[SkillsBridge] = []
        self._owners: dict[str, list[int]] = {}  # tool name -> indexes of the connections that listed it
        try:
            for schema in self._schemas:
                self._bridges.append(SkillsBridge(connect(schema), timeout=timeout))
        except BaseException:
            self.close()
            raise

    def list_tools(self):
        tools: dict[str, Any] = {}
        self._owners = {}
        for i, bridge in enumerate(self._bridges):
            for t in bridge.list_tools():
                if t.name.casefold() in self._hidden_tools:
                    continue
                self._owners.setdefault(t.name, []).append(i)
                tools.setdefault(t.name, t)  # the first connection's copy of a generic tool
        return list(tools.values())

    def call_tool(self, name: str, arguments: dict) -> str:
        full_name = str(arguments.get("full_name") or "").casefold()
        if name.casefold() in self._hidden_tools or (name == "load_skill" and full_name in self._hidden):
            raise SkillToolError("this skill is not available in this run")
        return self._route(name, arguments).call_tool(name, arguments)

    def _route(self, name: str, arguments: dict) -> SkillsBridge:
        owners = self._owners.get(name) or [0]
        if len(owners) > 1:  # a generic tool: the connection of the schema it names
            want = _schema_of(arguments)
            for i in owners:
                if self._schemas[i].casefold() == want:
                    return self._bridges[i]
        return self._bridges[owners[0]]

    def close(self) -> None:
        """Close every connection that opened, even when one fails; the first failure is raised afterwards."""
        first: BaseException | None = None
        for bridge in self._bridges:
            try:
                bridge.close()
            except BaseException as e:
                first = first or e
        if first is not None:
            raise first

    def __enter__(self) -> MultiSkillsBridge:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _is_skill_tool(prod: str) -> bool:
    return prod.startswith(SKILL_PREFIX) or prod == "load_skill"


def _skill_fqn(prod: str, args: dict) -> str:
    return str(args.get("full_name") or "") if prod == "load_skill" else prod[len(SKILL_PREFIX) :]


def _production_name(names: ToolNameMap, offered_name: str) -> str:
    return names.original(offered_name) if offered_name in names.known() else offered_name


def _args(tc: ToolCall) -> dict:
    return tc.arguments if isinstance(tc.arguments, dict) else {}


def _record_tool_span(prod: str, tc: ToolCall, invoked: list[str], is_offered: bool, run=None) -> str | None:
    """Open a TOOL span for one call, set inputs and attributes, and run `run()` inside it for its output."""
    args = _args(tc)
    attrs = {"tool.name": prod, "tool.args": json.dumps(args)}
    if is_offered and _is_skill_tool(prod):
        fqn = _skill_fqn(prod, args)
        if fqn:
            attrs["skill.fqn"] = fqn
            invoked.append(fqn)
    with mlflow.start_span(name=prod, span_type=SpanType.TOOL) as span:
        span.set_inputs(args)
        span.set_attributes(attrs)
        out = run() if run is not None else None
        if out is not None:
            span.set_outputs({"result": out})
    return out


def run_agent(
    case: EvalCase,
    *,
    model: ModelClient,
    skills: SkillsBridge | MultiSkillsBridge | None,
    names: ToolNameMap,
    fixtures: FixtureStore,
    trigger: bool,
    system_prompt: str,
    max_turns: int = 8,
) -> AgentResult:
    with mlflow.start_span(name="agent_run", span_type=SpanType.AGENT) as root:
        root.set_inputs({"prompt": case.prompt})
        result = _run(case, model, skills, names, fixtures, trigger, system_prompt, max_turns)
        root.set_outputs(
            {"final_text": result.final_text} | ({"error": result.error} if result.error else {})
        )
        return result


def _run(case, model, skills, names, fixtures, trigger, system_prompt, max_turns) -> AgentResult:
    specs: list[ToolSpec] = list(fixtures.tools_for(case))
    offered: dict[str, str] = {}  # name the model sees -> name on the skills server
    if skills is not None:
        for t in skills.list_tools():
            offered_name = names.register(names.to_production(t.name))
            offered[offered_name] = t.name
            schema = json.loads(names.to_production(json.dumps(t.input_schema)))
            specs.append(ToolSpec(offered_name, names.to_production(t.description), schema))

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": case.prompt},
    ]
    invoked: list[str] = []
    calls: list[ToolCall] = []
    turns = 0
    while True:
        turns += 1
        res = model.chat(messages, specs, max_tokens=TRIGGER_MAX_TOKENS if trigger else None, trigger=trigger)
        messages.append(res.assistant_message)
        if not res.tool_calls:
            return AgentResult(res.text, invoked, calls, turns, error=None)
        if trigger:
            # The first decision is the answer: record the chosen tools but do not execute them.
            for tc in res.tool_calls:
                _record_tool_span(_production_name(names, tc.name), tc, invoked, tc.name in offered)
            return AgentResult(res.text, invoked, list(res.tool_calls), turns, error=None)
        if turns >= max_turns:  # out of budget: do not execute the tools this turn requests
            return AgentResult("", invoked, calls, turns, error=f"max_turns={max_turns} reached")
        for tc in res.tool_calls:
            calls.append(tc)
            out = _dispatch(tc, skills, names, fixtures, case, offered, invoked)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": out})


def _dispatch(tc: ToolCall, skills, names, fixtures, case, offered, invoked) -> str:
    prod = _production_name(names, tc.name)

    def execute() -> str:
        args = _args(tc)
        if tc.name in FAKE_TOOLS:
            out = fixtures.call(tc.name, args, case)
        elif tc.name in offered and skills is not None:
            try:
                out = skills.call_tool(offered[tc.name], names.to_staged(args))
            except SkillToolError as e:  # the server flagged the result as an error: the model may see it
                out = f"Error: {e}"
            except (
                Exception
            ) as e:  # an outage or auth failure is not a model failure: let the trace show ERROR
                raise RuntimeError(names.to_production(f"{type(e).__name__}: {e}")) from None
        else:
            out = f"Error: unknown tool {prod}"
        return names.to_production(out)  # a staged FQN must never reach the model

    return _record_tool_span(prod, tc, invoked, tc.name in offered, run=execute) or ""
