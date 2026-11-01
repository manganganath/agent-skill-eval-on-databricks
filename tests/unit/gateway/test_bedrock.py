import json
from pathlib import Path
from types import SimpleNamespace

import boto3
import mlflow
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from skillhub.config import ConfigError, ModelProfile
from skillhub.gateway import bedrock
from skillhub.gateway.aws import AwsKeys
from skillhub.gateway.bedrock import BEDROCK_CONTENT, BedrockCallError, BedrockModelClient, tool_name
from skillhub.gateway.models import TRIGGER_MAX_TOKENS, ToolSpec, model_client
from tests.fakes import make_cfg

RECORDED = Path(__file__).parent / "recorded"
CLAUDE = ModelProfile(
    route="bedrock", model_id="us.anthropic.claude-sonnet-5-5", region="us-east-1", kind="external_model",
    params={"temperature": 0.2}, **{"class": "frontier"},
)  # fmt: skip
NOVA = ModelProfile(
    route="bedrock",
    model_id="amazon.nova-lite-v1:0",
    region="us-east-1",
    kind="external_model",
    **{"class": "fast"},
)
SKILL = "skill_skill_hub__dept_equity_research__earnings-preview"
TOOL = ToolSpec(SKILL, "Preview earnings", {"type": "object", "properties": {"ticker": {"type": "string"}}})
KEYS = AwsKeys("test-key-id-0001", "test-secret-value-0001", "test-session-token-0001")


def load(name):
    return json.loads((RECORDED / name).read_text())


class FakeRuntime:
    """Stands in for a bedrock-runtime client: answers `converse` from a script and records each request."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def converse(self, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs)))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


SYSTEM_AND_USER = [
    {"role": "system", "content": "You are an analyst."},
    {"role": "user", "content": "Preview NVDA"},
]


def test_a_tool_call_and_its_usage_come_back_in_the_chat_shape():
    runtime = FakeRuntime(load("bedrock_claude_tool_use.json"))
    res = BedrockModelClient("claude-bedrock", CLAUDE, runtime).chat(SYSTEM_AND_USER, [TOOL])
    assert res.text == "I'll use the earnings preview skill."
    [call] = res.tool_calls
    assert (call.id, call.name, call.arguments) == (
        "tooluse_kZJMlvQmRJ6eAyJE5GIl7Q",
        SKILL,
        {"ticker": "NVDA"},
    )
    assert res.finish_reason == "tool_calls"
    assert res.usage == {"prompt_tokens": 512, "completion_tokens": 61}
    assert res.assistant_message["tool_calls"][0]["function"] == {
        "name": SKILL,
        "arguments": '{"ticker": "NVDA"}',
    }
    [request] = runtime.requests
    assert request["modelId"] == "us.anthropic.claude-sonnet-5-5"
    assert request["system"] == [{"text": "You are an analyst."}]
    assert request["messages"] == [{"role": "user", "content": [{"text": "Preview NVDA"}]}]
    assert request["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"] == {"json": TOOL.parameters}


def test_a_text_answer_ends_the_turn():
    res = BedrockModelClient("nova-lite", NOVA, FakeRuntime(load("bedrock_nova_text.json"))).chat(
        SYSTEM_AND_USER, []
    )
    assert (res.text, res.tool_calls, res.finish_reason) == ("NVDA reports on 19 November.", [], "stop")


@pytest.mark.parametrize("stop", ["content_filtered", "guardrail_intervened"])
def test_a_filtered_answer_reads_as_content_filter_on_both_routes(stop):
    response = {**load("bedrock_nova_text.json"), "stopReason": stop}
    res = BedrockModelClient("nova-lite", NOVA, FakeRuntime(response)).chat(SYSTEM_AND_USER, [])
    assert res.finish_reason == "content_filter"


def test_no_request_carries_temperature_and_trigger_calls_keep_the_trigger_cap():
    runtime = FakeRuntime(load("bedrock_nova_text.json"), load("bedrock_nova_text.json"))
    client = BedrockModelClient("claude-bedrock", CLAUDE, runtime)
    client.chat(SYSTEM_AND_USER, [TOOL], trigger=True)
    client.chat(SYSTEM_AND_USER, [TOOL])
    trigger, full = runtime.requests
    assert trigger["inferenceConfig"] == {"maxTokens": TRIGGER_MAX_TOKENS}
    assert "inferenceConfig" not in full  # the model's own output limit, as for serving endpoints
    assert "temperature" not in json.dumps(runtime.requests)


def test_consecutive_tool_results_merge_into_one_user_turn_and_no_block_is_empty():
    """The agent loop sends one tool message per call; Converse wants alternating roles and no empty text."""
    history = [
        *SYSTEM_AND_USER,
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": SKILL, "arguments": '{"ticker": "NVDA"}'}},
            {"id": "t2", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "t1", "content": "Preview: beat expected."},
        {"role": "tool", "tool_call_id": "t2", "content": ""},
    ]  # fmt: skip
    _, turns = bedrock.to_converse(history)
    assert [t["role"] for t in turns] == ["user", "assistant", "user"]
    assert turns[1]["content"] == [
        {"toolUse": {"toolUseId": "t1", "name": SKILL, "input": {"ticker": "NVDA"}}},
        {"toolUse": {"toolUseId": "t2", "name": "read_file", "input": {}}},
    ]
    assert turns[2]["content"] == [
        {"toolResult": {"toolUseId": "t1", "content": [{"text": "Preview: beat expected."}]}},
        {"toolResult": {"toolUseId": "t2", "content": [{"text": "(no output)"}]}},
    ]


def test_long_tool_names_are_shortened_for_converse_and_mapped_back():
    long = "skill_skill_hub_dev__dept_investment_banking__merger-model-sensitivity-tables"
    assert len(long) > 64 and len(tool_name(long)) == 64 and tool_name(long) == tool_name(long)
    answer = load("bedrock_claude_tool_use.json")
    answer["output"]["message"]["content"][1]["toolUse"]["name"] = tool_name(long)
    runtime = FakeRuntime(answer)
    res = BedrockModelClient("c", CLAUDE, runtime).chat(
        SYSTEM_AND_USER, [ToolSpec(long, "Model a merger", {})]
    )
    assert runtime.requests[0]["toolConfig"]["tools"][0]["toolSpec"]["name"] == tool_name(long)
    assert res.tool_calls[0].name == long  # the agent loop sees the tool's own name


def test_the_assistant_turn_is_replayed_exactly_as_bedrock_sent_it():
    runtime = FakeRuntime(load("bedrock_claude_tool_use.json"), load("bedrock_nova_text.json"))
    client = BedrockModelClient("c", CLAUDE, runtime)
    first = client.chat(SYSTEM_AND_USER, [TOOL])
    history = [*SYSTEM_AND_USER, first.assistant_message,
               {"role": "tool", "tool_call_id": first.tool_calls[0].id, "content": "beat"}]  # fmt: skip
    client.chat(history, [TOOL])
    replayed = runtime.requests[1]["messages"][1]
    assert replayed == {"role": "assistant", "content": first.assistant_message[BEDROCK_CONTENT]}


def test_the_requests_and_answers_match_the_converse_schema():
    """botocore checks each request and response against the Bedrock service model. No call leaves the process."""
    real = boto3.Session(aws_access_key_id="x", aws_secret_access_key="y", region_name="us-east-1").client(
        "bedrock-runtime"
    )
    client = BedrockModelClient("c", CLAUDE, real)
    first = load("bedrock_claude_tool_use.json")
    with Stubber(real) as stub:
        stub.add_response("converse", first)
        stub.add_response("converse", load("bedrock_nova_text.json"))
        res = client.chat(SYSTEM_AND_USER, [TOOL], trigger=True)
        history = [*SYSTEM_AND_USER, res.assistant_message,
                   {"role": "tool", "tool_call_id": res.tool_calls[0].id, "content": "beat"}]  # fmt: skip
        client.chat(history, [TOOL])
        stub.assert_no_pending_responses()


def test_a_bedrock_error_is_the_calls_answer_and_never_carries_a_key_value():
    denied = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": f"key {KEYS.access_key_id} may not invoke"}},
        "Converse",
    )
    client = BedrockModelClient("c", CLAUDE, FakeRuntime(denied), keys=KEYS)
    with pytest.raises(BedrockCallError, match="AccessDeniedException") as e:
        client.chat(SYSTEM_AND_USER, [])
    assert KEYS.access_key_id not in str(e.value) and "***" in str(e.value)
    assert e.value.__cause__ is None


def test_each_call_inside_an_eval_case_is_a_chat_model_span():
    client = BedrockModelClient("nova-lite", NOVA, FakeRuntime(load("bedrock_nova_text.json")), keys=KEYS)
    with mlflow.start_span(name="eval_case"):
        client.chat(SYSTEM_AND_USER, [])
    mlflow.flush_trace_async_logging()
    trace = mlflow.get_trace(mlflow.get_last_active_trace_id())
    [span] = [s for s in trace.data.spans if s.name == "bedrock.converse"]
    assert span.span_type == "CHAT_MODEL"
    assert span.attributes["usage.output_tokens"] == 9
    assert not any(v in json.dumps(trace.to_dict(), default=str) for v in KEYS.values())


def test_the_client_reads_the_keys_and_calls_the_profile_region_with_bounded_retries():
    seen = {}

    def session(**kw):
        seen["session"] = kw
        return SimpleNamespace(
            client=lambda service, config: seen.update(service=service, config=config) or "rt"
        )

    client = bedrock.bedrock_client(make_cfg(), "nova-lite", NOVA, max_tries=2, timeout=30,
                                    keys=lambda cfg: KEYS, session_factory=session)  # fmt: skip
    assert seen["session"]["region_name"] == "us-east-1"
    assert seen["service"] == "bedrock-runtime"
    assert seen["config"].retries == {"max_attempts": 2, "mode": "standard"}
    assert seen["config"].read_timeout == 30
    assert client._runtime == "rt"


def test_a_profile_without_a_region_cannot_be_served():
    with pytest.raises(ConfigError, match="needs a Bedrock model_id and region"):
        bedrock.bedrock_client(
            make_cfg(), "x", NOVA.model_copy(update={"region": None}), keys=lambda cfg: KEYS
        )


def test_model_client_gives_a_bedrock_profile_the_bedrock_client(monkeypatch):
    cfg = make_cfg()
    cfg = cfg.model_copy(update={"models": {**cfg.models, "nova-lite": NOVA}})
    monkeypatch.setattr(bedrock, "bedrock_client", lambda c, name, p, **kw: ("bedrock", name, kw))
    assert model_client(cfg, "nova-lite", dict) == ("bedrock", "nova-lite", {"max_tries": 3, "timeout": 180})


def test_nova_tool_names_have_no_hyphen_and_map_back():
    """Nova rejects a call to a tool whose name holds a hyphen; Claude takes it as it is."""
    assert tool_name(SKILL, plain=True) == SKILL.replace("-", "_") and tool_name(SKILL) == SKILL
    answer = load("bedrock_claude_tool_use.json")
    answer["output"]["message"]["content"][1]["toolUse"]["name"] = SKILL.replace("-", "_")
    runtime = FakeRuntime(answer)
    res = BedrockModelClient("n", NOVA, runtime).chat(SYSTEM_AND_USER, [TOOL])
    assert runtime.requests[0]["toolConfig"]["tools"][0]["toolSpec"]["name"] == SKILL.replace("-", "_")
    assert res.tool_calls[0].name == SKILL


def test_two_tools_that_share_a_converse_name_are_refused():
    runtime = FakeRuntime()
    tools = [ToolSpec("a-b", "one", {}), ToolSpec("a_b", "two", {})]
    with pytest.raises(BedrockCallError, match="'a-b' and 'a_b'"):
        BedrockModelClient("n", NOVA, runtime).chat(SYSTEM_AND_USER, tools)
    assert runtime.requests == []


def test_nova_history_tool_calls_are_sent_with_plain_names_including_long_ones():
    long = "skill_skill_hub_dev__dept_investment_banking__merger-model-sensitivity-tables"

    def call(i, name):
        return {"id": i, "type": "function", "function": {"name": name, "arguments": "{}"}}

    history = [*SYSTEM_AND_USER,
               {"role": "assistant", "content": None, "tool_calls": [call("t1", SKILL), call("t2", long)]},
               {"role": "tool", "tool_call_id": "t1", "content": "a"},
               {"role": "tool", "tool_call_id": "t2", "content": "b"}]  # fmt: skip
    runtime = FakeRuntime(load("bedrock_nova_text.json"))
    BedrockModelClient("n", NOVA, runtime).chat(history, [TOOL])
    sent = [b["toolUse"]["name"] for b in runtime.requests[0]["messages"][1]["content"]]
    assert sent == [SKILL.replace("-", "_"), tool_name(long, plain=True)]
    assert len(sent[1]) == 64 and "-" not in sent[1]


@pytest.mark.parametrize(
    ("model_id", "plain"),
    [
        ("amazon.nova-lite-v1:0", True),
        ("us.amazon.nova-lite-v1:0", True),
        ("global.amazon.nova-2-lite-v1:0", True),
        ("arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-pro-v1:0", True),
        ("arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.amazon.nova-pro-v1:0", True),
        ("anthropic.claude-sonnet-5-5", False),
        ("us.anthropic.claude-sonnet-5-5", False),
        ("meta.llama4-maverick-17b-instruct-v1:0", False),
    ],
)
def test_nova_gets_plain_tool_names_through_a_region_prefix_or_an_arn(model_id, plain):
    assert bedrock._plain_names(NOVA.model_copy(update={"model_id": model_id})) is plain


def test_whitespace_only_text_is_sent_as_a_placeholder():
    _, turns = bedrock.to_converse(
        [
            {"role": "user", "content": "  \n "},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "t1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "t1", "content": " \t\n"},
        ]
    )  # fmt: skip
    assert turns[0]["content"] == [{"text": "(empty)"}]
    assert turns[2]["content"][0]["toolResult"]["content"] == [{"text": "(no output)"}]
