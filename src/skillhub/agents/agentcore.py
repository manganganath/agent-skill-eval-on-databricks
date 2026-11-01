"""An agent on Amazon Bedrock AgentCore as an external agent: the eval Job invokes its runtime once per case-repeat.

- **Call.** `bedrock-agentcore` `invoke_agent_runtime`, with the contract as a JSON payload and a new runtime session
  for each case-repeat. The call is signed with the IAM user's key from the secret scope (`gateway.aws`), the same key
  the Bedrock models use: its policy must also allow `bedrock-agentcore:InvokeAgentRuntime` on the runtime.
- **Payload.** `prompt`, `mode`, `skills_mcp` (null in without_skill mode), `model` (the row's Bedrock model ID),
  `max_turns`, `trace` (the attributes to put on every span), `otel` (where to send the spans) and the case's fixture
  files, as `files` and `search_index` (`FixtureStore.payload`): the agent runs elsewhere and cannot read them here.
- **Answer.** A JSON object: `{"answer": "<final text>"}`, or `{"error": "<why>"}`.
- **Retries.** Throttling, a quota, a retryable conflict and an internal error are retried with backoff, each time
  in a new session. Any other error is the case's error. No key value reaches an error: they are redacted.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from typing import Any

from skillhub.agent.fixtures import FixtureStore
from skillhub.agents.contract import CASE_ID, REPEAT, RUN_KEY, AgentAnswer, AgentCase, otlp_endpoint
from skillhub.config import HubConfig, ModelProfile
from skillhub.gateway.aws import AwsKeys, bedrock_keys, redact, session_for
from skillhub.retry import backoff_s

HARNESS = "agentcore"
MAX_TRIES = 4
BACKOFF_S = 2.0
BACKOFF_MAX_S = 30.0
CONNECT_TIMEOUT_S = 10
READ_TIMEOUT_S = 900  # one effectiveness case
MESSAGE_MAX = 500
RETRYABLE = frozenset(
    {
        "ThrottlingException",
        "ServiceQuotaExceededException",
        "RetryableConflictException",
        "InternalServerException",
    }
)


def payload(case: AgentCase, model_id: str, host: str) -> dict[str, Any]:
    fixtures = {"files": {}, "search_index": {}}
    if case.case is not None and case.case.fixtures and case.fixtures_root is not None:
        fixtures = FixtureStore(case.fixtures_root).payload(case.case)
    return {
        "prompt": case.prompt,
        "mode": case.mode,
        "skills_mcp": list(case.skills_mcp) or None,
        "model": model_id,
        "max_turns": case.max_turns,
        "trace": dict(case.attributes),
        "otel": {"endpoint": otlp_endpoint(host), "uc_table": case.trace_table},
        **fixtures,
    }


def session_id(case: AgentCase) -> str:
    """A new session for each call: 33 to 256 characters, as AgentCore requires."""
    a = case.attributes
    return f"skillhub-{a[RUN_KEY]}-{a[CASE_ID]}-{a[REPEAT]}-{uuid.uuid4().hex}"


def parse_response(response: dict) -> AgentAnswer:
    content_type = str(response.get("contentType") or "")
    body = response["response"].read()
    if "json" not in content_type:
        return AgentAnswer(
            "", f"the agent answered with {content_type or 'no content type'}; it must answer JSON"
        )
    try:
        data = json.loads(body)
    except ValueError:
        return AgentAnswer("", "the agent's answer is not valid JSON")
    if not isinstance(data, dict):
        return AgentAnswer("", "the agent's answer is not a JSON object")
    if data.get("error"):
        return AgentAnswer("", f"the agent reported: {str(data['error'])[:MESSAGE_MAX]}")
    return AgentAnswer(str(data.get("answer") or ""))


class AgentCoreAgent:
    harness = HARNESS

    def __init__(
        self,
        *,
        runtime_arn: str,
        model_id: str,
        host: str,
        client: Any,
        concurrency: int,
        keys: AwsKeys | None = None,
        sleep: Callable[[float], object] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.runtime_arn, self.model_id, self.host = runtime_arn, model_id, host
        self.client, self.concurrency = client, concurrency
        self._keys, self._sleep, self._clock = keys, sleep, clock

    @classmethod
    def from_profile(cls, cfg: HubConfig, name: str, profile: ModelProfile) -> AgentCoreAgent:
        from botocore.config import Config

        keys = bedrock_keys(cfg)  # an AwsCredentialsError (a ConfigError) names the scope and the key only
        config = Config(
            connect_timeout=CONNECT_TIMEOUT_S, read_timeout=READ_TIMEOUT_S, retries={"max_attempts": 1}
        )
        client = session_for(keys, profile.region or "").client("bedrock-agentcore", config=config)
        return cls(
            runtime_arn=(profile.agent or {}).get("runtime_arn", ""),
            model_id=profile.model_id or "",
            host=cfg.workspace.host,
            client=client,
            concurrency=cfg.defaults.concurrency_per_endpoint,
            keys=keys,
        )

    def _error(self, text: str) -> str:
        return redact(text, self._keys)[:MESSAGE_MAX]

    def run(self, case: AgentCase) -> AgentAnswer:
        from botocore.exceptions import BotoCoreError, ClientError

        start = self._clock()
        body = json.dumps(payload(case, self.model_id, self.host)).encode("utf-8")
        for attempt in range(1, MAX_TRIES + 1):
            try:
                response = self.client.invoke_agent_runtime(
                    agentRuntimeArn=self.runtime_arn,
                    runtimeSessionId=session_id(case),
                    payload=body,
                    contentType="application/json",
                    accept="application/json",
                )
                answer = parse_response(response)
                return AgentAnswer(answer.text, answer.error, self._clock() - start)
            except ClientError as exc:
                error = exc.response.get("Error", {})
                code = str(error.get("Code") or "ClientError")
                if code in RETRYABLE and attempt < MAX_TRIES:
                    self._sleep(backoff_s(attempt, base_s=BACKOFF_S, cap_s=BACKOFF_MAX_S))
                    continue
                return AgentAnswer(
                    "", self._error(f"{code}: {error.get('Message') or ''}".strip()), self._clock() - start
                )
            except BotoCoreError as exc:  # its text can hold a request: only its type leaves
                return AgentAnswer(
                    "", f"{type(exc).__name__}: the call to the agent runtime failed", self._clock() - start
                )
        raise AssertionError("unreachable")
