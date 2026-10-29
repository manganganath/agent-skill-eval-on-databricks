from __future__ import annotations

import json
import random
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import openai

from skillhub.config import HubConfig, ModelProfile
from skillhub.gateway.request_auth import HeaderAuth

Provider = Literal["claude", "gpt", "gemini"]
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_TRIES = 3
TIMEOUT_S = 180
MAX_RETRY_AFTER_S = 30.0  # a longer Retry-After is capped: the caller's own deadline still applies


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class ChatResult:
    text: str
    tool_calls: list[ToolCall]
    finish_reason: str
    usage: dict[str, int]
    assistant_message: dict


class ModelClient(Protocol):
    profile_name: str

    def chat(
        self,
        messages: list[dict],
        tools: list[ToolSpec],
        *,
        max_tokens: int | None = None,
        trigger: bool = False,
    ) -> ChatResult: ...


# Output budget for a trigger decision. Thinking models (Claude, Gemini Flash) spend output tokens before they
# answer, and their thinking cannot be turned off portably; at 64 tokens most calls ended with
# finish_reason=length before any tool call. The model still stops at its first tool call, so a decision
# costs a few dozen tokens plus thinking.
TRIGGER_MAX_TOKENS = 1024


def adapt_request(
    profile: ModelProfile,
    messages: list[dict],
    tools: list[ToolSpec],
    *,
    max_tokens: int | None,
    trigger: bool,
) -> dict:
    """Build the chat-completions body. Drops every parameter the provider rejects."""
    prov = profile.provider
    params = dict(profile.params)
    params.pop("temperature", None)  # no model accepts temperature

    body: dict = {"model": profile.endpoint, "messages": messages}
    if tools:
        body["tools"] = [
            {
                "type": "function",
                "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
            }
            for t in tools
        ]
    if trigger:
        body["max_tokens"] = TRIGGER_MAX_TOKENS
        if tools:
            body["tool_choice"] = "auto"
    elif max_tokens is not None:
        body["max_tokens"] = max_tokens

    if "seed" in params and prov in ("gpt", "gemini"):
        body["seed"] = params["seed"]
    if "parallel_tool_calls" in params and prov == "gpt":
        body["parallel_tool_calls"] = params["parallel_tool_calls"]
    if "reasoning_effort" in params:
        if prov == "gpt":
            body["reasoning_effort"] = "none" if tools else params["reasoning_effort"]
        else:
            body["reasoning_effort"] = params["reasoning_effort"]
    if prov == "gpt" and trigger and tools:
        body["reasoning_effort"] = "none"
    return body


def _normalise_content(content: Any) -> str:
    """Gemini returns content as a list of parts. Join the text parts."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")


def parse_response(provider: Provider, body: dict) -> ChatResult:
    choice = body["choices"][0]
    msg = choice["message"]
    calls: list[ToolCall] = []
    for tc in msg.get("tool_calls") or []:
        call_id = tc.get("id") or ""
        name = tc["function"]["name"]
        # Gemini reuses the function name as the call ID, which breaks tool-result matching.
        if provider == "gemini" and (not call_id or call_id == name):
            call_id = f"call_{uuid.uuid4().hex[:12]}"
        args = tc["function"].get("arguments") or "{}"
        calls.append(
            ToolCall(id=call_id, name=name, arguments=json.loads(args) if isinstance(args, str) else args)
        )
    # Echo the assistant message unchanged, except the tool-call IDs we replaced (Gemini only).
    echo = json.loads(json.dumps(msg))
    if provider == "gemini" and echo.get("tool_calls"):
        for raw, call in zip(echo["tool_calls"], calls, strict=True):
            raw["id"] = call.id
    usage = body.get("usage") or {}
    return ChatResult(
        text=_normalise_content(msg.get("content")),
        tool_calls=calls,
        finish_reason=choice.get("finish_reason") or "",
        usage={
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
        },
        assistant_message=echo,
    )


def _wire_dict(completion: Any) -> dict:
    """Dump the SDK object without the None-valued keys the SDK adds (refusal, audio, ...), so the
    assistant message can be replayed as history. `content` is always present, as on the wire."""
    body = completion.model_dump(exclude_none=True, warnings=False)
    for choice in body.get("choices", []):
        choice.get("message", {}).setdefault("content", None)
    return body


# The openai SDK requires an api_key. The real credential is set per request by HeaderAuth in the HTTP client.
API_KEY_PLACEHOLDER = "per-request-auth"


class ServingModelClient:
    """Chat client for Databricks serving endpoints, built on the openai SDK so that
    mlflow.openai.autolog() records the LLM spans."""

    def __init__(
        self,
        profile_name: str,
        profile: ModelProfile,
        host: str,
        headers: Callable[[], dict],
        *,
        http_client: Any = None,
        max_tries: int = MAX_TRIES,
        timeout: float = TIMEOUT_S,
    ):
        self.profile_name = profile_name
        self.profile = profile
        self.host = host.rstrip("/")
        self.headers = headers
        self.max_tries = max(1, max_tries)
        self._sleep: Callable[[float], None] = time.sleep
        self._provider: Provider = profile.provider
        # The credential lives in the HTTP transport, read again for every request. It never reaches the
        # `create()` kwargs, which mlflow.openai.autolog() records in the trace. `http_client` is a test seam: its
        # `auth` is replaced.
        http = http_client if http_client is not None else openai.DefaultHttpxClient()
        http.auth = HeaderAuth(headers)
        # Retries are ours, so the SDK's own retry is off.
        self._client = openai.OpenAI(
            base_url=f"{self.host}/serving-endpoints",
            api_key=API_KEY_PLACEHOLDER,
            max_retries=0,
            timeout=timeout,
            http_client=http,
        )

    @staticmethod
    def _retry_after(exc: Exception) -> float | None:
        response = getattr(exc, "response", None)
        raw = response.headers.get("retry-after") if response is not None else None
        try:
            value = float(raw) if raw is not None else None
        except ValueError:  # an HTTP date: use the backoff
            return None
        return min(value, MAX_RETRY_AFTER_S) if value is not None and value >= 0 else None

    def _create(self, body: dict) -> dict:
        """One completion. 429/5xx and connection or timeout errors are retried with jittered backoff, honouring
        a numeric Retry-After."""
        for attempt in range(self.max_tries):
            try:
                # No credentials here: HeaderAuth sets them on the request.
                completion = self._client.chat.completions.create(**body)
                return _wire_dict(completion)
            except (openai.APIStatusError, openai.APIConnectionError) as exc:
                retryable = isinstance(exc, openai.APIConnectionError) or exc.status_code in RETRY_STATUSES
                if not retryable or attempt == self.max_tries - 1:
                    raise
                wait = self._retry_after(exc)
                self._sleep((2**attempt if wait is None else wait) + random.uniform(0, 0.5))
        raise AssertionError("unreachable")

    def chat(
        self,
        messages: list[dict],
        tools: list[ToolSpec],
        *,
        max_tokens: int | None = None,
        trigger: bool = False,
    ) -> ChatResult:
        body = adapt_request(self.profile, messages, tools, max_tokens=max_tokens, trigger=trigger)
        try:
            raw = self._create(body)
        except openai.BadRequestError as exc:
            if "max_completion_tokens" not in str(exc) or "max_tokens" not in body:
                raise
            body["max_completion_tokens"] = body.pop("max_tokens")  # some GPT endpoints want this name
            raw = self._create(body)
        return parse_response(self._provider, raw)


def model_client(
    cfg: HubConfig,
    profile_name: str,
    headers: Callable[[], dict],
    *,
    max_tries: int = MAX_TRIES,
    timeout: float = TIMEOUT_S,
) -> ModelClient:
    """`max_tries` and `timeout` let a caller with its own deadline (the Skill Hub Try panel) bound each call."""
    profile = cfg.models[profile_name]
    if profile.route != "serving":
        raise NotImplementedError("ug-anthropic route is not supported yet")
    return ServingModelClient(
        profile_name, profile, cfg.workspace.host, headers, max_tries=max_tries, timeout=timeout
    )
