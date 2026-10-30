import json
import threading

import anyio
import mlflow
import pytest
from mlflow.entities import SpanType

from skillhub.agent.fixtures import FixtureStore
from skillhub.agent.loop import MultiSkillsBridge, SkillsBridge, run_agent
from skillhub.evalspec import EvalCase
from skillhub.gateway.models import ChatResult, ToolCall
from skillhub.gateway.skills_mcp import McpTool, SkillToolError
from skillhub.gateway.toolnames import ToolNameMap
from tests.fakes import FakeModelClient, FakeSkillsMcp

STAGED_FQN = "cat.run_pr12_ab12cd3_dept_x.earnings_preview"
PRODUCTION_FQN = "cat.dept_x.earnings_preview"
STAGED_TOOL = "skill_" + STAGED_FQN
PRODUCTION_TOOL = "skill_" + PRODUCTION_FQN
SANITISED = "skill_cat__dept_x__earnings_preview"
SYSTEM = "You are a helpful assistant."


def chat(text="", tool_calls=None, usage=(10, 5)):
    tool_calls = tool_calls or []
    msg = {
        "role": "assistant",
        "content": text,
        "tool_calls": [
            {
                "id": t.id,
                "type": "function",
                "function": {"name": t.name, "arguments": json.dumps(t.arguments)},
            }
            for t in tool_calls
        ],
    }
    return ChatResult(
        text=text,
        tool_calls=tool_calls,
        finish_reason="tool_calls" if tool_calls else "stop",
        usage={"prompt_tokens": usage[0], "completion_tokens": usage[1]},
        assistant_message=msg,
    )


def skill_names():
    m = ToolNameMap()
    m.set_fqn_rewrite({STAGED_FQN: PRODUCTION_FQN})
    return m


def case(prompt="Preview NVDA", should_trigger=True, fixtures=None):
    return EvalCase(id=1, prompt=prompt, should_trigger=should_trigger, fixtures=fixtures or [])


def skills_with(text="SKILL BODY"):
    return SkillsBridge(FakeSkillsMcp({STAGED_TOOL: ("Preview earnings for a ticker. " + STAGED_FQN, text)}))


def test_trigger_stops_at_first_decision_with_max_tokens_64_and_does_not_execute():
    model = FakeModelClient([chat(tool_calls=[ToolCall("c1", SANITISED, {})])])
    mcp = FakeSkillsMcp({STAGED_TOOL: ("d " + STAGED_FQN, "BODY")})
    with SkillsBridge(mcp) as skills:
        res = run_agent(
            case(),
            model=model,
            skills=skills,
            names=skill_names(),
            fixtures=FixtureStore("fixtures"),
            trigger=True,
            system_prompt=SYSTEM,
            max_turns=8,
        )
    assert len(model.calls) == 1
    assert model.calls[0]["max_tokens"] == 64
    assert res.invoked_skills == [PRODUCTION_FQN]
    assert res.turns == 1
    assert mcp.calls == []


def test_trigger_no_tool_means_not_invoked():
    model = FakeModelClient([chat(text="I can answer directly.")])
    res = run_agent(
        case(should_trigger=False),
        model=model,
        skills=None,
        names=ToolNameMap(),
        fixtures=FixtureStore("fixtures"),
        trigger=True,
        system_prompt=SYSTEM,
    )
    assert res.invoked_skills == []
    assert res.final_text == "I can answer directly."


def test_model_sees_production_names_never_staged():
    model = FakeModelClient([chat(text="done")])
    with skills_with() as skills:
        run_agent(
            case(),
            model=model,
            skills=skills,
            names=skill_names(),
            fixtures=FixtureStore("fixtures"),
            trigger=False,
            system_prompt=SYSTEM,
        )
    offered = json.dumps(model.tools_seen[0])
    assert "run_pr12" not in offered
    assert STAGED_FQN not in offered
    assert SANITISED in offered


def test_tool_result_text_is_rewritten_before_reaching_the_model():
    model = FakeModelClient(
        [
            chat(tool_calls=[ToolCall("c1", SANITISED, {})]),
            chat(text="done"),
        ]
    )
    with skills_with(text=f"Use {STAGED_FQN} next") as skills:
        run_agent(
            case(),
            model=model,
            skills=skills,
            names=skill_names(),
            fixtures=FixtureStore("fixtures"),
            trigger=False,
            system_prompt=SYSTEM,
        )
    tool_msg = next(m for m in model.calls[1]["messages"] if m["role"] == "tool")
    assert "run_pr12" not in tool_msg["content"]
    assert PRODUCTION_FQN in tool_msg["content"]


def test_outputs_is_final_message_after_last_tool():
    model = FakeModelClient(
        [
            chat(tool_calls=[ToolCall("c1", "read_file", {"path": "northwind/press_release.md"})]),
            chat(text="Here is the preview."),
        ]
    )
    res = run_agent(
        case(fixtures=["northwind/press_release.md"]),
        model=model,
        skills=None,
        names=ToolNameMap(),
        fixtures=FixtureStore("fixtures"),
        trigger=False,
        system_prompt=SYSTEM,
    )
    assert res.final_text == "Here is the preview."
    assert res.turns == 2
    assert res.error is None


def test_max_turns_bounds_the_loop():
    model = FakeModelClient(
        [chat(tool_calls=[ToolCall(f"c{i}", "read_file", {"path": "x"})]) for i in range(10)]
    )
    res = run_agent(
        case(fixtures=["x"]),
        model=model,
        skills=None,
        names=ToolNameMap(),
        fixtures=FixtureStore("fixtures"),
        trigger=False,
        system_prompt=SYSTEM,
        max_turns=3,
    )
    assert res.turns == 3
    assert res.error is not None and "max_turns" in res.error
    assert len(model.calls) == 3


def test_max_turns_error_is_on_the_root_span_and_last_turn_tools_are_not_run():
    mcp = FakeSkillsMcp({STAGED_TOOL: ("d " + STAGED_FQN, "BODY")})
    model = FakeModelClient([chat(tool_calls=[ToolCall("c1", SANITISED, {})])])
    with SkillsBridge(mcp) as skills:
        res = run_agent(
            case(),
            model=model,
            skills=skills,
            names=skill_names(),
            fixtures=FixtureStore("fixtures"),
            trigger=False,
            system_prompt=SYSTEM,
            max_turns=1,
        )
    assert res.error and mcp.calls == []
    mlflow.flush_trace_async_logging()
    trace = mlflow.get_trace(mlflow.get_last_active_trace_id())
    root = next(s for s in trace.data.spans if s.name == "agent_run")
    assert root.outputs["error"] == res.error


def test_tracing_contract_root_span_and_tool_span_attributes():
    model = FakeModelClient(
        [
            chat(tool_calls=[ToolCall("c1", SANITISED, {"ticker": "NVDA"})]),
            chat(text="final"),
        ]
    )
    with skills_with() as skills:
        run_agent(
            case(),
            model=model,
            skills=skills,
            names=skill_names(),
            fixtures=FixtureStore("fixtures"),
            trigger=False,
            system_prompt=SYSTEM,
        )
    mlflow.flush_trace_async_logging()
    trace = mlflow.get_trace(mlflow.get_last_active_trace_id())
    spans = {s.name: s for s in trace.data.spans}
    root = spans["agent_run"]
    assert root.span_type == SpanType.AGENT
    assert root.inputs == {"prompt": "Preview NVDA"}
    assert root.outputs == {"final_text": "final"}
    tool = spans[PRODUCTION_TOOL]
    assert tool.span_type == SpanType.TOOL
    assert tool.attributes["skill.fqn"] == PRODUCTION_FQN
    assert tool.attributes["tool.name"] == PRODUCTION_TOOL
    assert tool.inputs == {"ticker": "NVDA"}
    assert tool.attributes["tool.args"] == {"ticker": "NVDA"}  # MLflow decodes JSON attributes on read
    assert "run_pr12" not in json.dumps(trace.to_dict())


def test_trigger_mode_still_records_the_tool_span():
    model = FakeModelClient([chat(tool_calls=[ToolCall("c1", SANITISED, {})])])
    with skills_with() as skills:
        run_agent(
            case(),
            model=model,
            skills=skills,
            names=skill_names(),
            fixtures=FixtureStore("fixtures"),
            trigger=True,
            system_prompt=SYSTEM,
        )
    mlflow.flush_trace_async_logging()
    trace = mlflow.get_trace(mlflow.get_last_active_trace_id())
    tool = next(s for s in trace.data.spans if s.span_type == SpanType.TOOL)
    assert tool.name == PRODUCTION_TOOL
    assert tool.attributes["skill.fqn"] == PRODUCTION_FQN


def test_non_skill_tool_has_no_skill_fqn_attribute():
    model = FakeModelClient(
        [
            chat(tool_calls=[ToolCall("c1", "read_file", {"path": "northwind/press_release.md"})]),
            chat(text="ok"),
        ]
    )
    run_agent(
        case(fixtures=["northwind/press_release.md"]),
        model=model,
        skills=None,
        names=ToolNameMap(),
        fixtures=FixtureStore("fixtures"),
        trigger=False,
        system_prompt=SYSTEM,
    )
    mlflow.flush_trace_async_logging()
    trace = mlflow.get_trace(mlflow.get_last_active_trace_id())
    tool = next(s for s in trace.data.spans if s.span_type == SpanType.TOOL)
    assert "skill.fqn" not in tool.attributes


def test_tool_args_are_mapped_back_to_staged_and_server_errors_become_results():
    mcp = FakeSkillsMcp({STAGED_TOOL: ("d " + STAGED_FQN, "ok")})
    model = FakeModelClient(
        [
            chat(tool_calls=[ToolCall("c1", SANITISED, {"ref": PRODUCTION_FQN})]),
            chat(text="done"),
        ]
    )
    with SkillsBridge(mcp) as skills:
        run_agent(
            case(),
            model=model,
            skills=skills,
            names=skill_names(),
            fixtures=FixtureStore("fixtures"),
            trigger=False,
            system_prompt=SYSTEM,
        )
    assert mcp.calls == [(STAGED_TOOL, {"ref": STAGED_FQN})]


class ScriptedMcp(FakeSkillsMcp):
    def __init__(self, exc):
        super().__init__({STAGED_TOOL: ("d " + STAGED_FQN, "x")})
        self._exc = exc

    async def call_tool(self, name, arguments):
        raise self._exc


def _call_once(mcp):
    model = FakeModelClient([chat(tool_calls=[ToolCall("c1", SANITISED, {})]), chat(text="done")])
    with SkillsBridge(mcp) as skills:
        return model, run_agent(
            case(),
            model=model,
            skills=skills,
            names=skill_names(),
            fixtures=FixtureStore("fixtures"),
            trigger=False,
            system_prompt=SYSTEM,
        )


def test_server_flagged_tool_error_becomes_a_model_visible_result():
    model, res = _call_once(ScriptedMcp(SkillToolError(f"{STAGED_TOOL} failed: bad {STAGED_FQN}")))
    tool_msg = next(m for m in model.calls[1]["messages"] if m["role"] == "tool")
    assert tool_msg["content"].startswith("Error:") and "run_pr12" not in tool_msg["content"]
    assert res.final_text == "done"


def test_transport_error_propagates_without_staged_names():
    with pytest.raises(RuntimeError) as ei:
        _call_once(ScriptedMcp(ConnectionError(f"down for {STAGED_FQN}")))
    assert "run_pr12" not in str(ei.value) and "ConnectionError" in str(ei.value)
    mlflow.flush_trace_async_logging()
    trace = mlflow.get_trace(mlflow.get_last_active_trace_id())
    assert "run_pr12" not in json.dumps(trace.to_dict())


def test_tool_span_output_is_rewritten():
    model = FakeModelClient([chat(tool_calls=[ToolCall("c1", SANITISED, {})]), chat(text="ok")])
    with skills_with(text=f"see {STAGED_FQN}") as skills:
        run_agent(
            case(),
            model=model,
            skills=skills,
            names=skill_names(),
            fixtures=FixtureStore("fixtures"),
            trigger=False,
            system_prompt=SYSTEM,
        )
    mlflow.flush_trace_async_logging()
    trace = mlflow.get_trace(mlflow.get_last_active_trace_id())
    tool = next(s for s in trace.data.spans if s.span_type == SpanType.TOOL)
    assert PRODUCTION_FQN in json.dumps(tool.outputs) and "run_pr12" not in json.dumps(tool.outputs)


def test_tool_schema_is_rewritten():
    class SchemaMcp(FakeSkillsMcp):
        async def list_tools(self):
            return [McpTool(STAGED_TOOL, "d", {"type": "object", "description": f"use {STAGED_FQN}"})]

    model = FakeModelClient([chat(text="done")])
    with SkillsBridge(SchemaMcp({})) as skills:
        run_agent(
            case(),
            model=model,
            skills=skills,
            names=skill_names(),
            fixtures=FixtureStore("fixtures"),
            trigger=False,
            system_prompt=SYSTEM,
        )
    offered = json.dumps(model.tools_seen[0])
    assert "run_pr12" not in offered and PRODUCTION_FQN in offered


def test_hallucinated_skill_name_and_bad_args_do_not_count_as_invocations():
    model = FakeModelClient(
        [
            chat(tool_calls=[ToolCall("c1", "skill_cat.dept_x.ghost", {}), ToolCall("c2", "load_skill", {})]),
            chat(text="done"),
        ]
    )
    res = run_agent(
        case(),
        model=model,
        skills=None,
        names=ToolNameMap(),
        fixtures=FixtureStore("fixtures"),
        trigger=False,
        system_prompt=SYSTEM,
    )
    assert res.invoked_skills == []
    odd = FakeModelClient(
        [chat(tool_calls=[ToolCall("c1", "read_file", ["not", "a", "dict"])]), chat(text="x")]
    )
    assert (
        run_agent(
            case(fixtures=["x"]),
            model=odd,
            skills=None,
            names=ToolNameMap(),
            fixtures=FixtureStore("fixtures"),
            trigger=False,
            system_prompt=SYSTEM,
        ).final_text
        == "x"
    )


class TaskGroupClient:
    """Enters and exits a real anyio task group, like the MCP ClientSession."""

    async def __aenter__(self):
        self._tg = anyio.create_task_group()
        await self._tg.__aenter__()
        self._tg.start_soon(anyio.sleep_forever)
        return self

    async def __aexit__(self, *exc):
        self._tg.cancel_scope.cancel()
        return await self._tg.__aexit__(None, None, None)

    async def list_tools(self):
        return []

    async def call_tool(self, name, arguments):
        return "ok"


def test_bridge_survives_anyio_task_group_client_and_closes_cleanly():
    bridge = SkillsBridge(TaskGroupClient())
    assert bridge.list_tools() == []
    assert bridge.call_tool("t", {}) == "ok"
    bridge.close()
    assert not bridge._thread.is_alive()
    assert bridge._thread.name == "skills-bridge"


def test_bridge_close_is_idempotent_and_calls_after_close_raise():
    bridge = SkillsBridge(FakeSkillsMcp({}))
    bridge.close()
    bridge.close()
    assert not bridge._thread.is_alive()
    with pytest.raises(RuntimeError, match="closed"):
        bridge.list_tools()
    with pytest.raises(RuntimeError, match="closed"):
        bridge.call_tool("t", {})


def test_bridge_enter_failure_propagates_and_stops_the_thread():
    class Boom:
        async def __aenter__(self):
            raise ValueError("no connect")

        async def __aexit__(self, *exc):
            return None

    with pytest.raises(ValueError, match="no connect"):
        SkillsBridge(Boom())
    assert not any(t.name == "skills-bridge" and t.is_alive() for t in threading.enumerate())


def test_bridge_open_timeout_lets_the_client_clean_up_before_the_loop_stops():
    """The cancelled open must run its cleanup (the MCP client closes its HTTP session) on the bridge
    loop; stopping the loop at once left it pending."""
    log = []

    class Slow:
        async def __aenter__(self):
            try:
                await anyio.sleep(30)
            except BaseException:
                await anyio.sleep(0.05)  # cleanup that needs the loop, like AsyncExitStack.aclose()
                log.append("cleaned up")
                raise

        async def __aexit__(self, *exc):
            return None

    with pytest.raises(TimeoutError):
        SkillsBridge(Slow(), timeout=0.2)
    assert log == ["cleaned up"]
    assert not any(t.name == "skills-bridge" and t.is_alive() for t in threading.enumerate())


# --- one connection per schema -------------------------------------------------------------------------------

A_SKILL, B_SKILL = "skill_cat.a.one", "skill_cat.b.two"


class Connection(FakeSkillsMcp):
    """A fake connection that records when it is closed, and can fail to close."""

    def __init__(self, tools, log, name, fail_close=False):
        super().__init__(tools)
        self.log, self.name, self.fail_close = log, name, fail_close

    async def __aexit__(self, *exc):
        self.log.append(self.name)
        if self.fail_close:
            raise RuntimeError(f"{self.name} did not close")


def two_connections(log, *, fail_close_b=False):
    """Schema cat.a lists skill one and load_skill; cat.b lists skill two and load_skill too."""
    conns = {
        "cat.a": Connection({A_SKILL: ("a", "one text"), "load_skill": ("load", "A load")}, log, "cat.a"),
        "cat.b": Connection(
            {B_SKILL: ("b", "two text"), "load_skill": ("load", "B load")},
            log,
            "cat.b",
            fail_close=fail_close_b,
        ),
    }
    return conns


def test_multi_bridge_lists_the_union_and_offers_each_generic_tool_once():
    log = []
    conns = two_connections(log)
    with MultiSkillsBridge(["cat.a", "cat.b"], lambda s: conns[s]) as bridge:
        names = sorted(t.name for t in bridge.list_tools())
    assert names == ["load_skill", A_SKILL, B_SKILL]
    assert sorted(log) == ["cat.a", "cat.b"]  # every connection closed


def test_multi_bridge_routes_a_skill_call_to_the_connection_that_listed_it():
    log = []
    conns = two_connections(log)
    with MultiSkillsBridge(["cat.a", "cat.b"], lambda s: conns[s]) as bridge:
        bridge.list_tools()
        assert bridge.call_tool(B_SKILL, {}) == "two text"
    assert conns["cat.b"].calls == [(B_SKILL, {})]
    assert conns["cat.a"].calls == []


def test_multi_bridge_routes_a_generic_call_by_the_schema_its_arguments_name():
    log = []
    conns = two_connections(log)
    with MultiSkillsBridge(["cat.a", "cat.b"], lambda s: conns[s]) as bridge:
        bridge.list_tools()
        assert bridge.call_tool("load_skill", {"full_name": "cat.b.two"}) == "B load"
        assert bridge.call_tool("load_skill", {"full_name": "CAT.A.one"}) == "A load"  # case-insensitive
        assert bridge.call_tool("load_skill", {"full_name": "cat.z.none"}) == "A load"  # else the first
    assert [c[1]["full_name"] for c in conns["cat.b"].calls] == ["cat.b.two"]
    assert [c[1]["full_name"] for c in conns["cat.a"].calls] == ["CAT.A.one", "cat.z.none"]


def test_multi_bridge_hides_a_staged_skill_from_the_tools_and_refuses_its_calls():
    log = []
    conns = two_connections(log)
    with MultiSkillsBridge(["cat.a", "cat.b"], lambda s: conns[s], hidden=["cat.b.two"]) as bridge:
        assert B_SKILL not in {t.name for t in bridge.list_tools()}
        with pytest.raises(SkillToolError, match="not available"):
            bridge.call_tool(B_SKILL, {})
        with pytest.raises(SkillToolError, match="not available"):
            bridge.call_tool("load_skill", {"full_name": "cat.b.two"})
    assert conns["cat.b"].calls == []


def test_multi_bridge_fails_as_a_whole_and_closes_the_connections_that_opened():
    log = []
    conns = two_connections(log)

    def connect(schema):
        if schema == "cat.b":
            raise ConnectionError("gateway down")
        return conns[schema]

    with pytest.raises(ConnectionError, match="gateway down"):
        MultiSkillsBridge(["cat.a", "cat.b"], connect)
    assert log == ["cat.a"]


def test_multi_bridge_closes_every_connection_even_when_one_fails_to_close():
    log = []
    conns = two_connections(log, fail_close_b=True)
    bridge = MultiSkillsBridge(["cat.a", "cat.b"], lambda s: conns[s])
    with pytest.raises(RuntimeError, match="cat.b did not close"):
        bridge.close()
    assert sorted(log) == ["cat.a", "cat.b"]


def test_run_agent_calls_a_skill_on_the_second_connection_through_the_merged_tools():
    log = []
    conns = two_connections(log)
    names = ToolNameMap()
    names.set_fqn_rewrite({"cat.b.two": "cat.b.two"})
    model = FakeModelClient([chat(tool_calls=[ToolCall("c1", "skill_cat__b__two", {})]), chat(text="done")])
    with MultiSkillsBridge(["cat.a", "cat.b"], lambda s: conns[s]) as skills:
        res = run_agent(
            case(),
            model=model,
            skills=skills,
            names=names,
            fixtures=FixtureStore("fixtures"),
            trigger=False,
            system_prompt=SYSTEM,
        )
    assert res.final_text == "done"
    assert res.invoked_skills == ["cat.b.two"]
    assert conns["cat.b"].calls == [(B_SKILL, {})]
