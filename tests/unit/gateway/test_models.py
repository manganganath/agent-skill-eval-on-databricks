import json
from pathlib import Path

import httpx2
import openai
import pytest

from skillhub.config import ModelProfile
from skillhub.gateway.models import ServingModelClient, ToolSpec, adapt_request, parse_response

HOST = "https://ws.example.test"
ENDPOINT_URL = f"{HOST}/serving-endpoints/chat/completions"
RECORDED = Path(__file__).parent / "recorded"
MSGS = [{"role": "user", "content": "hi"}]
TOOL = ToolSpec(
    name="skill_cat__dept_x__earnings-preview",
    description="Preview earnings",
    parameters={"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]},
)


def load(name: str) -> dict:
    return json.loads((RECORDED / name).read_text())


def profile(endpoint: str, **params) -> ModelProfile:
    return ModelProfile(route="serving", endpoint=endpoint, **{"class": "frontier"}, params=params)


def adapt(ep: str, tools=(), trigger=False, max_tokens=None, **params) -> dict:
    return adapt_request(profile(ep, **params), MSGS, list(tools), max_tokens=max_tokens, trigger=trigger)


CLAUDE, GPT, GEMINI = "databricks-claude-sonnet-5-5", "databricks-gpt-5-5", "databricks-gemini-3-8-flash"


def test_temperature_is_always_dropped_for_every_provider():
    for ep in [CLAUDE, GPT, GEMINI]:
        assert "temperature" not in adapt(ep, [TOOL], temperature=0.7)


def test_seed_kept_for_gpt_and_gemini_dropped_for_claude():
    assert "seed" not in adapt(CLAUDE, seed=42)
    assert adapt(GPT, seed=42)["seed"] == 42
    assert adapt(GEMINI, seed=42)["seed"] == 42


def test_parallel_tool_calls_only_for_gpt():
    assert adapt(GPT, [TOOL], parallel_tool_calls=False)["parallel_tool_calls"] is False
    assert "parallel_tool_calls" not in adapt(CLAUDE, [TOOL], parallel_tool_calls=False)
    assert "parallel_tool_calls" not in adapt(GEMINI, [TOOL], parallel_tool_calls=False)


def test_gpt_reasoning_effort_forced_to_none_when_tools_present():
    assert adapt(GPT, [TOOL], reasoning_effort="low")["reasoning_effort"] == "none"
    assert adapt(GPT, [], reasoning_effort="low")["reasoning_effort"] == "low"
    assert adapt(CLAUDE, [TOOL], reasoning_effort="low")["reasoning_effort"] == "low"


def test_trigger_calls_leave_room_for_thinking_and_use_tool_choice_auto():
    """Thinking models spend output tokens before they decide; at 64 tokens Gemini Flash and Claude were cut
    off (finish_reason=length) before calling any tool, so most positives scored as missed."""
    from skillhub.gateway.models import TRIGGER_MAX_TOKENS

    body = adapt(CLAUDE, [TOOL], trigger=True)
    assert body["max_tokens"] == TRIGGER_MAX_TOKENS >= 1024
    assert body["tool_choice"] == "auto"


def test_tools_are_sent_in_openai_function_format():
    assert adapt(CLAUDE, [TOOL])["tools"][0] == {
        "type": "function",
        "function": {"name": TOOL.name, "description": TOOL.description, "parameters": TOOL.parameters},
    }


def test_gemini_list_content_becomes_text():
    assert parse_response("gemini", load("gemini_list_content.json")).text == "Here is the preview."


def test_gemini_reused_tool_call_id_is_replaced_by_local_id():
    res = parse_response("gemini", load("gemini_tool_call.json"))
    assert res.tool_calls[0].name == "skill_cat__dept_x__earnings-preview"
    assert res.tool_calls[0].id != res.tool_calls[0].name
    assert res.tool_calls[0].id.startswith("call_")
    assert res.assistant_message["tool_calls"][0]["id"] == res.tool_calls[0].id


def test_assistant_message_is_echoed_back_unchanged():
    body = load("gpt_tool_call.json")
    res = parse_response("gpt", body)
    assert res.assistant_message == body["choices"][0]["message"]
    assert res.tool_calls[0].arguments == {"ticker": "NVDA"}
    assert res.usage == {"prompt_tokens": 120, "completion_tokens": 18}


class Wire:
    """Serves scripted responses through the openai SDK's own HTTP stack (httpx2) and records requests."""

    def __init__(self, *responses: httpx2.Response) -> None:
        self._responses = list(responses)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]

    @property
    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]


def make_client(wire: Wire, endpoint: str = CLAUDE, headers=lambda: {"Authorization": "Bearer t"}, **kw):
    client = ServingModelClient(
        "m",
        profile(endpoint),
        HOST,
        headers,
        http_client=httpx2.Client(transport=httpx2.MockTransport(wire)),
        **kw,
    )
    client.slept = []
    client._sleep = client.slept.append
    return client


def test_uses_openai_sdk_with_bearer_token_and_returns_parsed_result():
    wire = Wire(httpx2.Response(200, json=load("claude_text.json")))
    res = make_client(wire).chat(MSGS, [])
    assert res.text == "ok"
    assert str(wire.requests[0].url) == ENDPOINT_URL
    assert wire.requests[0].headers["authorization"] == "Bearer t"
    assert wire.bodies[0]["model"] == CLAUDE


def test_gemini_list_content_survives_the_sdk_round_trip():
    wire = Wire(httpx2.Response(200, json=load("gemini_list_content.json")))
    assert make_client(wire, GEMINI).chat(MSGS, []).text == "Here is the preview."


def test_retries_on_429_then_succeeds_three_tries():
    wire = Wire(
        httpx2.Response(429, json={"error": "rate"}),
        httpx2.Response(503, json={"error": "busy"}),
        httpx2.Response(200, json=load("claude_text.json")),
    )
    assert make_client(wire).chat(MSGS, []).text == "ok"
    assert len(wire.requests) == 3


def test_gives_up_after_three_tries_on_persistent_5xx():
    wire = Wire(httpx2.Response(500, json={"error": "boom"}))
    with pytest.raises(openai.APIStatusError):
        make_client(wire, headers=lambda: {}).chat(MSGS, [])
    assert len(wire.requests) == 3


def test_gpt_max_tokens_error_retries_with_max_completion_tokens():
    wire = Wire(
        httpx2.Response(
            400, json={"error": "Unsupported parameter: 'max_tokens'; use 'max_completion_tokens'"}
        ),
        httpx2.Response(200, json=load("gpt_tool_call.json")),
    )
    make_client(wire, GPT).chat(MSGS, [TOOL], max_tokens=64, trigger=True)
    second = wire.bodies[1]
    from skillhub.gateway.models import TRIGGER_MAX_TOKENS

    assert second["max_completion_tokens"] == TRIGGER_MAX_TOKENS
    assert "max_tokens" not in second


def test_other_400_errors_are_not_retried():
    wire = Wire(httpx2.Response(400, json={"error": "bad"}))
    with pytest.raises(openai.BadRequestError):
        make_client(wire).chat(MSGS, [], max_tokens=10)
    assert len(wire.requests) == 1


@pytest.mark.parametrize("fixture", ["gpt_tool_call.json", "claude_text.json"])
def test_assistant_message_through_the_sdk_equals_the_wire_message(fixture):
    body = load(fixture)
    res = make_client(Wire(httpx2.Response(200, json=body)), GPT).chat(MSGS, [])
    assert res.assistant_message == body["choices"][0]["message"]


# --- Credentials never reach the call kwargs that openai autolog records --------------------------------

SECRET = "dapi-SECRET-token-0123456789"


@pytest.fixture
def autolog():
    import mlflow
    import mlflow.openai

    mlflow.openai.autolog()
    yield mlflow
    mlflow.openai.autolog(disable=True)


def _stored_traces(mlflow) -> list:
    mlflow.flush_trace_async_logging()
    return mlflow.search_traces(return_type="list")


def _span_text(trace) -> str:
    """Everything a reader of the trace can see: the full trace JSON plus every span's inputs, outputs,
    attributes and events."""
    parts = [trace.to_json()]
    for s in trace.data.spans:
        parts += [json.dumps(s.inputs, default=str), json.dumps(s.outputs, default=str)]
        parts += [json.dumps(s.attributes, default=str), json.dumps(s.to_dict(), default=str)]
    return "\n".join(parts)


def test_the_trace_probe_sees_extra_headers_in_call_kwargs(autolog):
    """Control: autolog records `create()` kwargs, so a header passed there would be found by the probe."""
    wire = Wire(httpx2.Response(200, json=load("claude_text.json")))
    client = make_client(wire)
    client._client.chat.completions.create(
        model=CLAUDE, messages=MSGS, extra_headers={"Authorization": f"Bearer {SECRET}"}
    )
    (trace,) = _stored_traces(autolog)
    assert SECRET in _span_text(trace)


def test_bearer_token_is_never_recorded_in_autolog_traces(autolog):
    wire = Wire(
        httpx2.Response(503, json={"error": "busy"}),  # a retried failure is traced too
        httpx2.Response(200, json=load("claude_text.json")),
    )
    client = make_client(wire, headers=lambda: {"Authorization": f"Bearer {SECRET}"})
    assert client.chat(MSGS, [TOOL]).text == "ok"
    with pytest.raises(openai.BadRequestError):  # a failed call records an exception event
        make_client(
            Wire(httpx2.Response(400, json={"error": "bad"})),
            headers=lambda: {"Authorization": f"Bearer {SECRET}"},
        ).chat(MSGS, [])
    traces = _stored_traces(autolog)
    assert len(traces) == 3 and all(t.data.spans for t in traces)
    for t in traces:
        assert SECRET not in _span_text(t)
    # the token still reaches the server, on every request
    assert [r.headers["authorization"] for r in wire.requests] == [f"Bearer {SECRET}"] * 2


def test_token_is_read_per_request_and_never_given_to_the_sdk():
    tokens = iter(["first", "second"])
    current = {"t": next(tokens)}
    wire = Wire(httpx2.Response(200, json=load("claude_text.json")))
    client = make_client(wire, headers=lambda: {"Authorization": f"Bearer {current['t']}"})
    client.chat(MSGS, [])
    current["t"] = next(tokens)
    client.chat(MSGS, [])
    assert [r.headers["authorization"] for r in wire.requests] == ["Bearer first", "Bearer second"]
    assert "first" not in client._client.api_key and "second" not in client._client.api_key


def test_every_header_the_provider_returns_is_set_and_none_are_left_when_it_returns_none():
    wire = Wire(httpx2.Response(200, json=load("claude_text.json")))
    make_client(wire, headers=lambda: {"Authorization": "Bearer t", "X-Extra-Auth": "e"}).chat(MSGS, [])
    make_client(wire, headers=lambda: {}).chat(MSGS, [])
    assert wire.requests[0].headers["x-extra-auth"] == "e"
    assert "authorization" not in wire.requests[1].headers  # no placeholder credential is sent


# --- Retries ----------------------------------------------------------------------------------------


class FlakyWire(Wire):
    """Raises a transport error for the first `failures` requests."""

    def __init__(self, exc: Exception, failures: int, *responses: httpx2.Response) -> None:
        super().__init__(*responses)
        self._exc, self._failures = exc, failures

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        if self._failures:
            self._failures -= 1
            self.requests.append(request)
            raise self._exc
        return super().__call__(request)


@pytest.mark.parametrize(
    "exc",
    [httpx2.ConnectError("connection refused"), httpx2.ReadTimeout("read timed out")],
)
def test_connection_and_timeout_errors_are_retried(exc):
    wire = FlakyWire(exc, 2, httpx2.Response(200, json=load("claude_text.json")))
    client = make_client(wire)
    assert client.chat(MSGS, []).text == "ok"
    assert len(wire.requests) == 3 and len(client.slept) == 2


def test_connection_errors_give_up_after_max_tries():
    wire = FlakyWire(httpx2.ConnectError("down"), 9, httpx2.Response(200, json=load("claude_text.json")))
    with pytest.raises(openai.APIConnectionError):
        make_client(wire).chat(MSGS, [])
    assert len(wire.requests) == 3


def test_retry_after_is_honoured_capped_and_jittered():
    wire = Wire(
        httpx2.Response(429, headers={"retry-after": "7"}, json={"error": "rate"}),
        httpx2.Response(429, headers={"retry-after": "3600"}, json={"error": "rate"}),
        httpx2.Response(200, json=load("claude_text.json")),
    )
    client = make_client(wire)
    client.chat(MSGS, [])
    first, second = client.slept
    assert 7 <= first <= 7.5 and 30 <= second <= 30.5


def test_backoff_without_retry_after_doubles_with_jitter():
    wire = Wire(
        httpx2.Response(503, json={"error": "busy"}),
        httpx2.Response(503, json={"error": "busy"}),
        httpx2.Response(200, json=load("claude_text.json")),
    )
    client = make_client(wire)
    client.chat(MSGS, [])
    assert 1 <= client.slept[0] <= 1.5 and 2 <= client.slept[1] <= 2.5


def test_a_caller_can_bound_tries_and_timeout():
    wire = Wire(httpx2.Response(503, json={"error": "busy"}))
    client = make_client(wire, max_tries=1, timeout=12)
    with pytest.raises(openai.APIStatusError):
        client.chat(MSGS, [])
    assert len(wire.requests) == 1 and client._client.timeout == 12


def test_usage_context_is_sent_as_a_top_level_body_key_on_every_request():
    context = {"skillhub_source": "try", "skillhub_skill": "dept_x/earnings-preview"}
    wire = Wire(httpx2.Response(200, json=load("claude_text.json")))
    make_client(wire, usage_context=context).chat(MSGS, [TOOL], trigger=True)
    assert wire.bodies[0]["usage_context"] == context
    assert wire.bodies[0]["messages"] == MSGS


def test_unset_usage_context_sends_no_key():
    wire = Wire(httpx2.Response(200, json=load("claude_text.json")))
    make_client(wire).chat(MSGS, [])
    assert "usage_context" not in wire.bodies[0]
    assert "usage_context" not in adapt(CLAUDE, usage_context=None)
    assert "usage_context" not in adapt_request(
        profile(CLAUDE), MSGS, [], max_tokens=None, trigger=False, usage_context={}
    )


def test_usage_context_survives_the_gpt_max_completion_tokens_retry():
    wire = Wire(
        httpx2.Response(
            400, json={"error": "Unsupported parameter: 'max_tokens'; use 'max_completion_tokens'"}
        ),
        httpx2.Response(200, json=load("gpt_tool_call.json")),
    )
    make_client(wire, GPT, usage_context={"skillhub_source": "eval", "skillhub_skill": "d/s"}).chat(
        MSGS, [], max_tokens=64
    )
    assert wire.bodies[1]["usage_context"] == {"skillhub_source": "eval", "skillhub_skill": "d/s"}
