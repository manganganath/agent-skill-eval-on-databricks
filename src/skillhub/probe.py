"""The connection test of one model catalog row: `skillhub probe`, which the `skillhub-probe` Job runs.

The Job runs as skillhub-ci-eval, the identity every eval runs as, so a passing test proves what an eval of the row
would use: that identity's read of the model catalog, and its access to the serving endpoint, or its AWS keys in the
secret scope and their Bedrock permissions. The test is one chat call of at most 16 tokens.

The probe reads the row itself, as an eval would, and refuses a row whose target is not the one the Skill Hub asked
to test. It prints one line, `SKILLHUB_PROBE {json}`, which the Skill Hub reads from the Job run's output, and exits
0 when the test passed and 1 when it failed. It writes nothing: the Skill Hub records the result.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Literal

from skillhub.config import HubConfig
from skillhub.model_catalog.entry import CatalogEntry, Target
from skillhub.model_catalog.merge import with_catalog

RESULT_PREFIX = "SKILLHUB_PROBE "
PROMPT = "Reply with the single word: ok"
MAX_TOKENS = 16
TIMEOUT_S = 60
MESSAGE_MAX = 500


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


def run_probe(
    cfg: HubConfig,
    entries: list[CatalogEntry],
    version: int | None,
    name: str,
    expect: Target,
    *,
    client_for: Callable[[HubConfig, str], Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> ProbeResult:
    """Test the row `name`, enabled or not, if its target is still `expect`. Every failure is the test's answer."""
    entry = next((e for e in entries if e.name == name), None)
    if entry is None:
        return failed(name, f"there is no model named {name!r} in the model catalog")
    if not entry.runnable:
        return failed(name, f"a {entry.kind} cannot be tested in this release")
    if entry.target() != expect:
        return failed(name, "the model changed after the test was started; test it again")
    merged = with_catalog(cfg, [entry], version, include_disabled=True)
    start = clock()
    try:
        (client_for or _client)(merged, name).chat(
            [{"role": "user", "content": PROMPT}], [], max_tokens=MAX_TOKENS
        )
    except Exception as exc:
        return failed(name, describe(exc))
    ms = int((clock() - start) * 1000)
    return ProbeResult(name, "passed", f"answered in {ms} ms", ms)
