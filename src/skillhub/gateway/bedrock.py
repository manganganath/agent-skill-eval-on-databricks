"""Amazon Bedrock models through the Converse API, for the eval runner's agent loop.

The agent loop keeps its conversation in the chat-completions shape of gateway.models. This client turns that
history into Converse messages, calls `bedrock-runtime` `converse`, and turns the answer back into a ChatResult:
text, tool calls and token usage. It sends only the model, the messages, the system prompt, the tools and the token
cap: never `temperature`, which the eval models reject. Converse allows tool names of 64 characters at most, so a
longer name is shortened with a hash and mapped back.

Each call is a CHAT_MODEL span when a trace is active (an eval case), and makes no span otherwise (the connection
test). The AWS keys come from gateway.aws and never reach a span, a log or an error message.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from typing import Any

import mlflow
from mlflow.entities import SpanType

from skillhub.config import ConfigError, HubConfig, ModelProfile
from skillhub.gateway.aws import AwsKeys, bedrock_keys, redact, session_for
from skillhub.gateway.models import MAX_TRIES, TIMEOUT_S, TRIGGER_MAX_TOKENS, ChatResult, ToolCall, ToolSpec

MAX_TOOL_NAME = 64
CONNECT_TIMEOUT_S = 10
# The assistant turn's own Converse content, kept on the echoed message so that the next call replays it unchanged.
BEDROCK_CONTENT = "bedrock_content"
FINISH = {"tool_use": "tool_calls", "end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length"}


class BedrockCallError(RuntimeError):
    """Bedrock refused or failed a call. The message names the error and never holds a credential."""


def _plain_names(profile: ModelProfile) -> bool:
    """Amazon Nova fails a call whose tool name holds a hyphen ("invalid sequence as part of ToolUse").
    The model id may carry a region prefix (`us.amazon.`) or be an ARN, so `amazon.` is matched after `.` or `/`."""
    return re.search(r"(?:^|[./])amazon\.", profile.model_id or "") is not None


def tool_name(name: str, *, plain: bool = False) -> str:
    """A name Converse accepts: the name itself, or its first 55 characters, an underscore and 8 hash digits.
    With `plain`, hyphens become underscores first, for the models that cannot call a tool with a hyphen."""
    short = name.replace("-", "_") if plain else name
    if len(short) <= MAX_TOOL_NAME:
        return short
    return f"{short[: MAX_TOOL_NAME - 9]}_{hashlib.sha1(name.encode()).hexdigest()[:8]}"


def _assistant_blocks(message: dict, plain: bool) -> list[dict]:
    if message.get(BEDROCK_CONTENT):
        return list(message[BEDROCK_CONTENT])
    blocks: list[dict] = []
    if message.get("content"):
        blocks.append({"text": str(message["content"])})
    for tc in message.get("tool_calls") or []:
        args = tc["function"].get("arguments") or "{}"
        use = {"toolUseId": tc["id"], "name": tool_name(tc["function"]["name"], plain=plain)}
        use["input"] = json.loads(args) if isinstance(args, str) else args
        blocks.append({"toolUse": use})
    return blocks


def to_converse(messages: list[dict], *, plain: bool = False) -> tuple[list[dict], list[dict]]:
    """(system, messages) for Converse.

    A tool result becomes a toolResult block of a user turn. Turns of one role in a row are merged, because Converse
    wants the roles to alternate, and no block is empty, because Converse refuses an empty text."""
    system: list[dict] = []
    turns: list[dict] = []

    def add(role: str, blocks: list[dict]) -> None:
        if not blocks:
            return
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"].extend(blocks)
        else:
            turns.append({"role": role, "content": list(blocks)})

    for m in messages:
        role = m.get("role")
        if role == "system":
            if m.get("content"):
                system.append({"text": str(m["content"])})
        elif role == "user":
            add("user", [{"text": str(m.get("content") or "").strip() or "(empty)"}])
        elif role == "assistant":
            add("assistant", _assistant_blocks(m, plain))
        elif role == "tool":
            result = {
                "toolUseId": m["tool_call_id"],
                "content": [{"text": str(m.get("content") or "").strip() or "(no output)"}],
            }
            add("user", [{"toolResult": result}])
        else:
            raise ValueError(f"unknown message role {role!r}")
    return system, turns


def converse_request(
    profile: ModelProfile,
    messages: list[dict],
    tools: list[ToolSpec],
    *,
    max_tokens: int | None,
    trigger: bool,
) -> dict:
    """The keyword arguments of one `converse` call."""
    plain = _plain_names(profile)
    system, turns = to_converse(messages, plain=plain)
    body: dict[str, Any] = {"modelId": profile.model_id, "messages": turns}
    if system:
        body["system"] = system
    if tools:
        body["toolConfig"] = {
            "tools": [
                {
                    "toolSpec": {
                        "name": tool_name(t.name, plain=plain),
                        "description": t.description or t.name,  # Converse refuses an empty description
                        "inputSchema": {"json": t.parameters},
                    }
                }
                for t in tools
            ]
        }
    limit = TRIGGER_MAX_TOKENS if trigger else max_tokens
    if limit is not None:
        body["inferenceConfig"] = {"maxTokens": int(limit)}
    return body


def parse_converse(response: dict, names: dict[str, str]) -> ChatResult:
    """The ChatResult of a Converse response. `names` maps each name sent to Converse back to the tool's own."""
    message = (response.get("output") or {}).get("message") or {}
    content = list(message.get("content") or [])
    texts: list[str] = []
    calls: list[ToolCall] = []
    for block in content:
        if "text" in block:
            texts.append(block["text"])
        elif "toolUse" in block:
            use = block["toolUse"]
            args = use.get("input")
            calls.append(
                ToolCall(
                    id=use["toolUseId"],
                    name=names.get(use["name"], use["name"]),
                    arguments=args if isinstance(args, dict) else {},
                )
            )
    text = "".join(texts)
    echo: dict[str, Any] = {"role": "assistant", "content": text or None, BEDROCK_CONTENT: content}
    if calls:
        echo["tool_calls"] = [
            {
                "id": c.id,
                "type": "function",
                "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
            }
            for c in calls
        ]
    usage = response.get("usage") or {}
    stop = response.get("stopReason") or ""
    return ChatResult(
        text=text,
        tool_calls=calls,
        finish_reason=FINISH.get(stop, stop),
        usage={
            "prompt_tokens": usage.get("inputTokens", 0),
            "completion_tokens": usage.get("outputTokens", 0),
        },
        assistant_message=echo,
    )


class BedrockModelClient:
    """The ModelClient of a Bedrock profile. `runtime` is a `bedrock-runtime` client; a test passes a fake one."""

    def __init__(
        self, profile_name: str, profile: ModelProfile, runtime: Any, *, keys: AwsKeys | None = None
    ):
        self.profile_name = profile_name
        self.profile = profile
        self._runtime = runtime
        self._keys = keys

    def _converse(self, body: dict) -> dict:
        try:
            return self._runtime.converse(**body)
        except Exception as exc:  # throttling is retried by botocore; anything left is the call's answer
            raise BedrockCallError(redact(f"{type(exc).__name__}: {exc}", self._keys)) from None

    def chat(
        self,
        messages: list[dict],
        tools: list[ToolSpec],
        *,
        max_tokens: int | None = None,
        trigger: bool = False,
    ) -> ChatResult:
        body = converse_request(self.profile, messages, tools, max_tokens=max_tokens, trigger=trigger)
        plain = _plain_names(self.profile)
        names: dict[str, str] = {}
        for t in tools:
            sent = tool_name(t.name, plain=plain)
            if names.get(sent, t.name) != t.name:
                raise BedrockCallError(
                    f"tools {names[sent]!r} and {t.name!r} have the same name for this model"
                )
            names[sent] = t.name
        if mlflow.get_current_active_span() is None:
            return parse_converse(self._converse(body), names)
        with mlflow.start_span(name="bedrock.converse", span_type=SpanType.CHAT_MODEL) as span:
            span.set_inputs({k: body[k] for k in ("modelId", "system", "messages") if k in body})
            span.set_attributes(
                {"bedrock.model_id": self.profile.model_id, "bedrock.region": self.profile.region}
            )
            result = parse_converse(self._converse(body), names)
            span.set_outputs({"text": result.text, "finish_reason": result.finish_reason})
            span.set_attributes(
                {
                    "usage.input_tokens": result.usage["prompt_tokens"],
                    "usage.output_tokens": result.usage["completion_tokens"],
                }
            )
            return result


def bedrock_client(
    cfg: HubConfig,
    profile_name: str,
    profile: ModelProfile,
    *,
    max_tries: int = MAX_TRIES,
    timeout: float = TIMEOUT_S,
    keys: Callable[[HubConfig], AwsKeys] = bedrock_keys,
    session_factory: Callable[..., Any] | None = None,
) -> BedrockModelClient:
    """The client of a Bedrock profile. It reads the keys first, so a missing secret stops the run before staging
    (AwsCredentialsError, a ConfigError). Throttling and 5xx answers are retried by botocore, `max_tries` times."""
    if not profile.model_id or not profile.region:
        raise ConfigError(f"model profile {profile_name!r} needs a Bedrock model_id and region")
    from botocore.config import Config

    secret = keys(cfg)
    runtime = session_for(secret, profile.region, factory=session_factory).client(
        "bedrock-runtime",
        config=Config(
            retries={"max_attempts": max(1, max_tries), "mode": "standard"},
            read_timeout=timeout,
            connect_timeout=CONNECT_TIMEOUT_S,
        ),
    )
    return BedrockModelClient(profile_name, profile, runtime, keys=secret)
