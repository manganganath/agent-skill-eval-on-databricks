"""The connection test of one model catalog row: `skillhub probe`, which the `skillhub-probe` Job runs.

The Job runs as skillhub-ci-eval, the identity every eval runs as, so a passing test proves what an eval of the row
would use: that identity's read of the model catalog, and its access to the serving endpoint, or its AWS keys in the
secret scope and their Bedrock permissions. The test of a model is one chat call of at most 16 tokens. The test of an
external agent is one case, run as an eval runs it (the agent loads common's skills), then a wait of up to two minutes
for the agent's trace to reach common's trace table.

The probe reads the row itself, as an eval would, and refuses a row whose target is not the one the Skill Hub asked
to test. It prints one line, `SKILLHUB_PROBE {json}`, which the Skill Hub reads from the Job run's output, and exits
0 when the test passed and 1 when it failed. It writes nothing: the Skill Hub records the result.
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Literal

from skillhub.config import HubConfig
from skillhub.model_catalog.entry import CatalogEntry, Target, problems
from skillhub.model_catalog.merge import with_catalog

RESULT_PREFIX = "SKILLHUB_PROBE "
PROMPT = "Reply with the single word: ok"
MAX_TOKENS = 16
TIMEOUT_S = 60
MESSAGE_MAX = 500
AGENT_DEPT = "common"  # an agent's test loads common's skills and writes to common's trace table
AGENT_TRACE_WAIT_S = 120


@dataclass(frozen=True)
class ProbeResult:
    name: str
    status: Literal["passed", "failed"]
    message: str
    latency_ms: int | None = None

    def line(self) -> str:
        return RESULT_PREFIX + json.dumps(asdict(self), sort_keys=True)


def failed(name: str, message: str) -> ProbeResult:
    return ProbeResult(name, "failed", message[:MESSAGE_MAX])


def parse_result(logs: str | None) -> ProbeResult | None:
    """The last result line in a probe run's output, or None when there is none."""
    for line in reversed((logs or "").splitlines()):
        line = line.strip()
        if not line.startswith(RESULT_PREFIX):
            continue
        try:
            data = json.loads(line[len(RESULT_PREFIX) :])
        except ValueError:
            return None
        if not isinstance(data, dict) or data.get("status") not in ("passed", "failed"):
            return None
        latency = data.get("latency_ms")
        return ProbeResult(
            str(data.get("name") or ""),
            data["status"],
            str(data.get("message") or "")[:MESSAGE_MAX],
            latency if isinstance(latency, int) else None,
        )
    return None


def describe(exc: BaseException) -> str:
    """What may leave the probe about an error: the message only of classes known to carry no secret value.

    The Bedrock client redacts its call errors, a missing-key error names the scope and key only, and the config and
    catalog errors are written by this code. The serving client's openai errors carry the endpoint's own answer, such as
    a 403 PERMISSION_DENIED, and Databricks serving never echoes the bearer token. Any other error (a library's, a transport's) may quote a request or a
    credential, so only its type name leaves."""
    import openai

    from skillhub.config import ConfigError
    from skillhub.gateway.bedrock import BedrockCallError
    from skillhub.model_catalog.merge import CatalogError

    known = BedrockCallError | ConfigError | CatalogError | openai.APIStatusError | openai.APIConnectionError
    if isinstance(
        exc, known
    ):  # AwsCredentialsError is a ConfigError; the openai errors carry the endpoint's text
        return f"{type(exc).__name__}: {exc}"
    return f"{type(exc).__name__}: the call failed"


def _client(cfg: HubConfig, name: str) -> Any:
    from skillhub.auth import auth_headers
    from skillhub.gateway.models import model_client

    return model_client(cfg, name, auth_headers, max_tries=1, timeout=TIMEOUT_S)


def _agent(cfg: HubConfig, name: str) -> Any:
    from skillhub.agents import agent_for

    return agent_for(cfg, name)


def _wait(sql: Any, table: str, expected: set, **kw: Any) -> dict:
    from skillhub.agents.traces import wait_for_traces

    return wait_for_traces(sql, table, expected, **kw)


def probe_agent(
    cfg: HubConfig,
    name: str,
    *,
    sql: Any,
    agent_for: Callable[[HubConfig, str], Any] | None = None,
    wait: Callable[..., dict] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> ProbeResult:
    """One case on the agent, then its trace in common's trace table. `cfg` holds the row as a profile."""
    from skillhub.agents.contract import AgentCase, attributes, skills_urls, trace_table

    try:
        agent = (agent_for or _agent)(cfg, name)
    except Exception as exc:
        return failed(name, describe(exc))
    run_key, trace_key = f"probe_{secrets.token_hex(6)}", secrets.token_hex(16)
    table = trace_table(cfg, AGENT_DEPT)
    case = AgentCase(
        prompt=PROMPT,
        mode="trigger",
        skills_mcp=skills_urls(cfg, AGENT_DEPT),
        attributes=attributes(
            run_key=run_key, trace_key=trace_key, case_id=0, repeat=0, mode="trigger", profile=name
        ),
        trace_table=table,
        max_turns=1,
    )
    since_ns = time.time_ns() - 60 * 10**9
    start = clock()
    try:
        answer = agent.run(case)
    except Exception as exc:
        return failed(name, describe(exc))
    if answer.error is not None:
        return failed(name, f"the agent did not answer: {answer.error}")
    ms = int((clock() - start) * 1000)
    found = (wait or _wait)(
        sql,
        table,
        {(0, 0)},
        run_key=run_key,
        trace_key=trace_key,
        since_ns=since_ns,
        timeout_s=AGENT_TRACE_WAIT_S,
    )
    if (0, 0) not in found:
        return failed(
            name,
            f"the agent answered in {ms} ms, but its trace did not reach {table} within {AGENT_TRACE_WAIT_S} s",
        )
    return ProbeResult(name, "passed", f"answered in {ms} ms, and its trace reached {table}", ms)


def run_probe(
    cfg: HubConfig,
    entries: list[CatalogEntry],
    version: int | None,
    name: str,
    expect: Target,
    *,
    client_for: Callable[[HubConfig, str], Any] | None = None,
    sql: Any = None,
    agent_for: Callable[[HubConfig, str], Any] | None = None,
    wait: Callable[..., dict] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> ProbeResult:
    """Test the row `name`, enabled or not, if its target is still `expect`. Every failure is the test's answer.
    `sql` reads the trace table for an external agent's test."""
    entry = next((e for e in entries if e.name == name), None)
    if entry is None:
        return failed(name, f"there is no model named {name!r} in the model catalog")
    if not entry.runnable:
        return failed(name, f"{name} cannot be tested: {'; '.join(problems(entry)) or 'its kind is unknown'}")
    if entry.target() != expect:
        return failed(name, "the model changed after the test was started; test it again")
    merged = with_catalog(cfg, [entry], version, include_disabled=True)
    if entry.kind == "external_agent":
        return probe_agent(merged, name, sql=sql, agent_for=agent_for, wait=wait, clock=clock)
    start = clock()
    try:
        (client_for or _client)(merged, name).chat(
            [{"role": "user", "content": PROMPT}], [], max_tokens=MAX_TOKENS
        )
    except Exception as exc:
        return failed(name, describe(exc))
    ms = int((clock() - start) * 1000)
    return ProbeResult(name, "passed", f"answered in {ms} ms", ms)
