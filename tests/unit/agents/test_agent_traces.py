import json

import pytest

from skillhub.agents import contract
from skillhub.agents.traces import (
    find_sql,
    find_traces,
    for_judges,
    mlflow_trace_id,
    skill_calls,
    token_usage,
    tool_names,
    wait_for_traces,
)
from tests.unit.agents.agent_traces import load, recorded

TABLE = "skill_hub.common_evals.traces_otel_spans"
FQNS = [
    "skill_hub.common.audit-xls",
    "skill_hub.common.xlsx-author",
    "skill_hub.dept_equity_research.initiating-coverage",
]


def test_each_agents_name_for_a_skill_tool_maps_to_the_skill():
    names = tool_names(FQNS)
    assert names["skill_skill_hub.common.audit-xls"] == "audit-xls"  # as the gateway lists it
    assert names["skill_skill_hub_common_audit-xls"] == "audit-xls"  # dots to underscores (Claude Code)
    assert (
        names["skill_skill_hub_common_audit_xls"] == "audit-xls"
    )  # and hyphens too (a Nova model on Bedrock)
    long = "skill_skill_hub_dept_equity_research_initiating-coverage"
    assert len(long) <= 64 and names[long] == "initiating-coverage"


def test_the_first_skill_call_of_a_recorded_claude_code_trace_is_its_invoked_skill():
    trace = load(recorded("claude_code_trigger_spans.json"))
    assert skill_calls(trace, tool_names(FQNS)) == ["initiating-coverage"]
    # Claude Code's token counts are strings, and its input is the new, the cache-read and the cache-created tokens
    assert token_usage(trace) == ((2 + 0 + 9012) + (2 + 9012 + 35), 63 + 460)


def test_a_strands_trace_counts_only_its_model_call_spans_for_tokens():
    trace = load(recorded("strands_effectiveness_spans.json"))
    assert skill_calls(trace, tool_names(FQNS)) == ["audit-xls"]
    assert token_usage(trace) == (8000, 750)  # the agent span repeats the totals and is not counted


@pytest.mark.parametrize(
    ("tool", "skill"),
    [
        ("mcp__s1__skill_skill_hub_common_audit-xls", "audit-xls"),  # a server prefix is dropped
        (
            "skill_skill_hub_dept_equity_research_initiating-cov",
            "initiating-coverage",
        ),  # cut short, still one skill
        ("skill_skill_hub_common_", None),  # the start of two skills' names
        ("skill_skill_hub_common_made-up", None),  # not a skill the run offered
        ("load_skill", None),
    ],
)
def test_a_tool_name_matches_one_offered_skill_or_none(tool, skill):
    trace = load({"name": "root", "children": [{"name": "tool", "attributes": {"gen_ai.tool.name": tool}}]})
    assert skill_calls(trace, tool_names(FQNS)) == ([skill] if skill else [])


def test_a_model_call_counts_by_its_operation_name_when_mlflow_gives_it_no_span_type():
    trace = load({"name": "invoke_agent", "children": [
        {"name": "chat", "attributes": {"gen_ai.operation.name": "chat", "gen_ai.usage.input_tokens": 7,
                                        "gen_ai.usage.output_tokens": 3}},
    ]})  # fmt: skip
    assert token_usage(trace) == (7, 3)


def test_a_trace_without_spans_has_no_skill_and_no_tokens():
    trace = load({"name": "root"})
    assert skill_calls(trace, tool_names(FQNS)) == [] and token_usage(trace) == (0, 0)


class Rows:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def execute(self, statement, params=None):
        self.calls.append((statement, params))
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def row(trace_id, case_id, repeat, has_root=1, started_ns=1):
    return {
        "trace_id": trace_id,
        "case_id": case_id,
        "repeat": repeat,
        "has_root": has_root,
        "started_ns": started_ns,
    }


def test_traces_are_found_by_the_run_key_and_the_trace_key_in_resource_or_span_attributes():
    sql = Rows([row("aa01", "1", "0"), row("bb02", "2", "1", has_root=0), row("cc03", "x", "0")])
    found = find_traces(sql, TABLE, run_key="hub_1", trace_key="k" * 32, since_ns=5)
    assert found == {
        (1, 0): "trace:/skill_hub.common_evals.traces/aa01"
    }  # no root yet, or no case: not counted
    [(statement, params)] = sql.calls
    assert params == {"since_ns": 5, "run_key": "hub_1", "trace_key": "k" * 32}
    for key in (contract.RUN_KEY, contract.TRACE_KEY, contract.CASE_ID, contract.REPEAT):
        assert f"variant_get(resource.attributes, '$[\"{key}\"]', 'string')" in statement
        assert f"variant_get(attributes, '$[\"{key}\"]', 'string')" in statement


def test_a_retried_case_keeps_its_newest_trace():
    sql = Rows([row("old", "1", "0", started_ns=10), row("new", "1", "0", started_ns=20)])
    assert find_traces(sql, TABLE, run_key="r", trace_key="k", since_ns=0) == {
        (1, 0): mlflow_trace_id(TABLE, "new")
    }


def test_only_a_spans_table_of_the_hub_is_queried():
    with pytest.raises(ValueError, match="not a trace spans table"):
        find_sql("skill_hub.common_evals.traces_otel_spans; DROP TABLE x")


def test_waiting_stops_when_every_case_has_its_trace_and_gives_up_after_the_timeout(caplog):
    sql = Rows([], [row("aa", "1", "0")], [row("aa", "1", "0"), row("bb", "2", "0")])
    now = iter(range(0, 1000, 10))
    found = wait_for_traces(sql, TABLE, {(1, 0), (2, 0)}, run_key="r", trace_key="k", since_ns=0,
                            clock=lambda: next(now), sleep=lambda s: None)  # fmt: skip
    assert set(found) == {(1, 0), (2, 0)} and len(sql.calls) == 3
    late = Rows([row("aa", "1", "0")])
    now = iter(range(0, 1000, 100))
    found = wait_for_traces(late, TABLE, {(1, 0), (2, 0)}, run_key="r", trace_key="k", since_ns=0, timeout_s=300,
                            clock=lambda: next(now), sleep=lambda s: None)  # fmt: skip
    assert set(found) == {(1, 0)} and "1 of 2 agent traces did not reach" in caplog.text


def test_a_failed_poll_is_nothing_new_and_the_wait_goes_on(caplog):
    class Flaky(Rows):
        def execute(self, statement, params=None):
            if not self.calls:
                self.calls.append((statement, params))
                raise RuntimeError("warehouse is starting: test-token-0001")
            return super().execute(statement, params)

    sql = Flaky([row("aa", "1", "0")])
    now = iter(range(0, 1000, 10))
    found = wait_for_traces(sql, TABLE, {(1, 0)}, run_key="r", trace_key="k", since_ns=0,
                            clock=lambda: next(now), sleep=lambda s: None)  # fmt: skip
    assert set(found) == {(1, 0)}
    assert "RuntimeError" in caplog.text and "test-token-0001" not in caplog.text


def test_polls_that_keep_failing_return_what_was_found_at_the_deadline():
    class Down:
        def execute(self, statement, params=None):
            raise OSError("down")

    now = iter(range(0, 1000, 100))
    assert wait_for_traces(Down(), TABLE, {(1, 0)}, run_key="r", trace_key="k", since_ns=0, timeout_s=300,
                           clock=lambda: next(now), sleep=lambda s: None) == {}  # fmt: skip


def test_the_contract_fields_for_a_case():
    from tests.fakes import make_cfg

    cfg = make_cfg()
    assert (
        contract.trace_table(cfg, "equity_research")
        == f"{cfg.catalog}.dept_equity_research_evals.traces_otel_spans"
    )
    assert contract.skills_urls(cfg, "equity_research") == (
        f"{cfg.workspace.host}/ai-gateway/skills/?schema={cfg.catalog}.dept_equity_research",
        f"{cfg.workspace.host}/ai-gateway/skills/?schema={cfg.catalog}.common",
    )
    assert contract.skills_urls(cfg, "common") == (
        f"{cfg.workspace.host}/ai-gateway/skills/?schema={cfg.catalog}.common",
    )
    assert (contract.turns("trigger"), contract.turns("with_skill")) == (3, 8)
    attrs = contract.attributes(
        run_key="hub_1", trace_key="ab12", case_id=7, repeat=2, mode="trigger", profile="cc"
    )
    assert attrs[contract.CASE_ID] == "7" and attrs[contract.REPEAT] == "2"
    with pytest.raises(ValueError, match="trace attribute values"):
        contract.attributes(run_key="a,b=c", trace_key="k", case_id=1, repeat=0, mode="trigger", profile="cc")


def _otel_trace(*spans):
    """A trace as mlflow.get_trace reads it back from a trace table: the spans' OpenTelemetry attributes as the agent
    sent them, with no MLflow inputs or outputs. Built in memory: the test store would translate spans it stores."""
    import base64
    import json

    from mlflow.entities import Span, Trace, TraceData, TraceInfo, TraceLocation, TraceState

    def b64(n, size):
        return base64.b64encode(n.to_bytes(size, "big")).decode()

    built = [
        Span.from_dict(
            {
                "trace_id": b64(1, 16),
                "span_id": b64(i, 8),
                "parent_span_id": None if i == 1 else b64(1, 8),
                "name": name,
                "start_time_unix_nano": i,
                "end_time_unix_nano": 100 + i,
                "events": [{"name": e, "time_unix_nano": i, "attributes": a} for e, a in events],
                "status": {"code": "STATUS_CODE_OK", "message": ""},
                "attributes": {
                    "mlflow.traceRequestId": json.dumps("tr-1"),
                    **{k: json.dumps(v) for k, v in attrs.items()},
                },
            }
        )
        for i, (name, attrs, events) in enumerate(spans, start=1)
    ]
    info = TraceInfo(trace_id="tr-1", trace_location=TraceLocation.from_experiment_id("0"), request_time=0,
                     state=TraceState.OK)  # fmt: skip
    return Trace(info=info, data=TraceData(spans=built))


PROMPT = "Run a full model audit on Northwind's model.xlsx before it goes to IC. Model scope."


def test_a_claude_code_trace_gets_its_prompt_tool_names_and_tool_outputs_for_the_judges():
    trace = _otel_trace(
        ("claude_code.interaction", {"span.type": "interaction", "gen_ai.operation.name": "invoke_agent",
                                     "user_prompt": PROMPT}, []),
        ("claude_code.tool", {"span.type": "tool", "gen_ai.operation.name": "execute_tool",
                              "gen_ai.tool.name": "read_workbook", "tool_name": "mcp__fx__read_workbook"},
         [("tool.output", {"output": "Inputs!B1 value=Northwind Robotics"})]),
    )  # fmt: skip
    assert trace.data.spans[0].inputs is None  # as the judges saw it before
    judged = for_judges(trace)
    root, tool = judged.data.spans
    assert root.inputs == PROMPT and root.span_type == "AGENT"
    # Every Claude Code tool span is named claude_code.tool.
    assert (tool.name, tool.span_type) == ("read_workbook", "TOOL")
    assert tool.outputs == "Inputs!B1 value=Northwind Robotics"
    assert judged.info is trace.info and [s.span_id for s in judged.data.spans] == [
        s.span_id for s in trace.data.spans
    ]


def test_a_strands_trace_gets_its_request_and_each_tool_calls_arguments_and_result():
    from mlflow.genai.utils.trace_utils import extract_tools_called_from_trace

    messages = [{"role": "user", "parts": [{"type": "text", "content": "Audit model.xlsx"}]}]
    args = {"path": "northwind/model.xlsx"}
    # A Strands tool span carries the call as chat messages too, which MLflow's translation prefers.
    call = [
        {
            "role": "tool",
            "parts": [{"type": "tool_call", "name": "read_workbook", "id": "t1", "arguments": args}],
        }
    ]
    reply = [
        {
            "role": "tool",
            "parts": [{"type": "tool_call_response", "id": "t1", "response": [{"text": "Sheet BS"}]}],
        }
    ]
    trace = _otel_trace(
        ("invoke_agent Strands Agents", {"gen_ai.operation.name": "invoke_agent",
                                         "gen_ai.input.messages": json.dumps(messages)}, []),
        ("execute_tool read_workbook", {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "read_workbook",
                                        "gen_ai.input.messages": json.dumps(call),
                                        "gen_ai.output.messages": json.dumps(reply),
                                        "gen_ai.tool.call.arguments": json.dumps(args),
                                        "gen_ai.tool.call.result": json.dumps([{"text": "Sheet BS"}])}, []),
    )  # fmt: skip
    judged = for_judges(trace)
    root, tool = judged.data.spans
    assert root.inputs == messages
    assert (tool.name, tool.inputs, tool.outputs) == ("read_workbook", args, [{"text": "Sheet BS"}])
    # The tool-call judges read the calls with this, which fails on a call whose inputs are a list.
    assert [(c.name, c.arguments) for c in extract_tools_called_from_trace(judged)] == [
        ("read_workbook", args)
    ]


def test_a_trace_with_mlflow_inputs_keeps_them_and_a_span_that_cannot_be_translated_stays_as_it_was(
    monkeypatch,
):
    from skillhub.agents import traces

    trace = _otel_trace(
        ("eval_case", {"mlflow.spanInputs": {"prompt": "kept"}, "mlflow.spanType": "CHAIN"}, []),
        ("tool", {"gen_ai.tool.name": "read_workbook"}, []),
    )
    root, _ = for_judges(trace).data.spans
    assert root.inputs == {"prompt": "kept"}

    def broken(span):
        raise ValueError("cannot translate")

    monkeypatch.setattr(traces, "translate_span_when_storing", broken)
    assert [s.name for s in for_judges(trace).data.spans] == ["eval_case", "tool"]


def _available_tools(trace, monkeypatch):
    """The tools that the tool-call judges are told the agent had, by name: what MLflow finds on the model-call
    spans, without its fallback, which asks an LLM."""
    from mlflow.genai.utils import trace_utils

    monkeypatch.setattr(trace_utils, "_try_extract_available_tools_with_llm", lambda trace, model=None: [])
    return {t.function.name: t.function for t in trace_utils.extract_available_tools_from_trace(trace)}


READ_WORKBOOK_SCHEMA = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}


@pytest.mark.parametrize(
    "definition",
    [
        # Strands, in Bedrock's shape
        {"name": "read_workbook", "description": "Read a workbook.", "inputSchema": {"json": READ_WORKBOOK_SCHEMA},
         "outputSchema": None},
        {"toolSpec": {"name": "read_workbook", "description": "Read a workbook.",
                      "inputSchema": {"json": READ_WORKBOOK_SCHEMA}}},
        # the GenAI conventions
        {"type": "function", "name": "read_workbook", "description": "Read a workbook.",
         "parameters": READ_WORKBOOK_SCHEMA},
        # MLflow's and OpenAI's own
        {"type": "function", "function": {"name": "read_workbook", "description": "Read a workbook.",
                                          "parameters": READ_WORKBOOK_SCHEMA}},
    ],
)  # fmt: skip
def test_the_judges_get_the_tools_an_agent_defined_on_its_agent_span(definition, monkeypatch):
    skill = {"name": "skill_c_s_audit-xls", "description": "The audit-xls skill.",
             "inputSchema": {"json": {"type": "object", "properties": {}}}}  # fmt: skip
    trace = _otel_trace(
        ("invoke_agent Strands Agents", {"gen_ai.operation.name": "invoke_agent",
                                         "gen_ai.tool.definitions": json.dumps([definition, skill])}, []),
        ("chat", {"gen_ai.operation.name": "chat"}, []),
        ("execute_tool read_workbook", {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "read_workbook",
                                        "gen_ai.tool.call.arguments": json.dumps({"path": "northwind/model.xlsx"})},
         []),
    )  # fmt: skip
    # MLflow reads available tools only from model-call spans, and only in its own shape, so the judges were told
    # that the agent had no tools.
    tools = _available_tools(for_judges(trace), monkeypatch)
    assert sorted(tools) == ["read_workbook", "skill_c_s_audit-xls"]
    assert tools["read_workbook"].description == "Read a workbook."
    assert set(tools["read_workbook"].parameters.properties) == {"path"}
    assert tools["read_workbook"].parameters.required == ["path"]


def test_a_tool_whose_schema_mlflow_rejects_keeps_its_name_for_the_judges(monkeypatch):
    odd = {
        "name": "lookup",
        "inputSchema": {"json": {"type": "object", "properties": {"q": {"anyOf": [{}]}}}},
    }
    trace = _otel_trace(
        ("invoke_agent", {"gen_ai.operation.name": "invoke_agent", "gen_ai.tool.definitions": json.dumps([odd])}, []),
        ("chat", {"gen_ai.operation.name": "chat"}, []),
    )  # fmt: skip
    assert list(_available_tools(for_judges(trace), monkeypatch)) == ["lookup"]


def test_an_agent_that_records_no_tool_definitions_has_the_tools_it_called(monkeypatch):
    """Claude Code records no tool definitions. The tools it called existed, so the judges are not told that it had
    none, which made them fail every call."""
    trace = _otel_trace(
        ("claude_code.interaction", {"span.type": "interaction", "gen_ai.operation.name": "invoke_agent",
                                     "user_prompt": PROMPT}, []),
        ("claude_code.llm_request", {"span.type": "llm_request", "model": "claude-sonnet"}, []),
        ("claude_code.tool", {"span.type": "tool", "gen_ai.operation.name": "execute_tool",
                              "gen_ai.tool.name": "mcp__skills__skill_c_s_audit-xls"}, []),
        ("claude_code.tool", {"span.type": "tool", "gen_ai.operation.name": "execute_tool",
                              "gen_ai.tool.name": "read_workbook"}, []),
        ("claude_code.tool", {"span.type": "tool", "gen_ai.operation.name": "execute_tool",
                              "gen_ai.tool.name": "read_workbook"}, []),
    )  # fmt: skip
    tools = _available_tools(for_judges(trace), monkeypatch)
    assert list(tools) == ["mcp__skills__skill_c_s_audit-xls", "read_workbook"]
    assert "does not record" in tools["read_workbook"].description


def test_tools_that_a_model_call_span_already_has_are_kept(monkeypatch):
    own = [
        {
            "type": "function",
            "function": {"name": "search", "parameters": {"type": "object", "properties": {}}},
        }
    ]
    trace = _otel_trace(
        ("agent", {"gen_ai.operation.name": "invoke_agent",
                   "gen_ai.tool.definitions": json.dumps([{"name": "other", "inputSchema": {"json": {}}}])}, []),
        ("llm", {"mlflow.spanType": "CHAT_MODEL", "mlflow.chat.tools": own}, []),
    )  # fmt: skip
    assert list(_available_tools(for_judges(trace), monkeypatch)) == ["search"]
