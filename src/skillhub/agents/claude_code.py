"""Claude Code as an external agent: the eval Job runs the pinned `claude` CLI once per case-repeat.

- **Install.** The native binary of the row's pinned version, with Claude Code's installer, once per machine and
  version (serverless has no Node). A version already installed is used as it is.
- **Lockdown.** Each case runs non-interactively in a fresh scratch folder with its own config folder. `--tools ""`
  turns off every built-in tool (files, shell, web, local skills), so the agent has only the MCP servers it is given,
  with `--mcp-config` and `--strict-mcp-config`: the skills MCP, one server per schema (`s0`, `s1`), except in
  without_skill mode, and the case's fixture files (`fx`, skillhub.agents.fixtures_mcp). The skills MCP's authoring
  tools are taken away with `--disallowedTools`, and `--allowedTools` lets the given servers' tools run unprompted.
- **Model.** The Unity Gateway Anthropic route, `<host>/serving-endpoints/anthropic`, as the eval identity, with the
  row's serving endpoint as the model, also for Claude Code's background calls.
- **Trace.** Claude Code exports its own spans, one trace per run, to the department's trace table, with the
  contract's attributes as OTEL_RESOURCE_ATTRIBUTES. Its spans are the evidence. The answer is the `result` event of
  `--output-format stream-json`, because no span holds it.
- **Turns.** At most 3 in trigger mode and 8 otherwise (`contract.turns`).

The eval identity's token reaches the CLI only through its environment and a scratch file readable by this user, both
gone when the case ends. No token is logged: errors keep the end of the CLI's stderr with the token taken out.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

from skillhub.agents.contract import TABLE_HEADER, AgentAnswer, AgentCase, otlp_endpoint
from skillhub.agents.fixtures_mcp import spec_json
from skillhub.config import ConfigError, HubConfig, ModelProfile
from skillhub.gateway.skills_mcp import AUTHORING_TOOLS

HARNESS = "claude-code"
INSTALL_URL = "https://claude.ai/install.sh"
INSTALL_TIMEOUT_S = 300
TRIGGER_TIMEOUT_S = 300
CASE_TIMEOUT_S = 900
# Each case is a CLI process next to the eval worker processes: two at a time per worker keep serverless memory
# in bounds.
CONCURRENCY = 2
FIXTURES_SERVER = "fx"
STDERR_TAIL = 400
NO_RESULT = "Claude Code ended without a result"
# Only these variables of the Job's environment reach the CLI. The Job's own credentials do not.
_INHERIT = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR")
_VERSION = re.compile(r"\d{1,4}\.\d{1,4}\.\d{1,6}")

Runner = Callable[..., subprocess.CompletedProcess]


class AgentSetupError(ConfigError):
    """The agent cannot run on this machine. A ConfigError, so the runner records the profile as one it cannot
    serve, before anything is staged."""


def skills_servers(case: AgentCase) -> list[str]:
    return [f"s{i}" for i in range(len(case.skills_mcp))]


def mcp_config(case: AgentCase, token: str, fixtures_spec: Path | None, python: str = sys.executable) -> dict:
    servers: dict[str, dict] = {
        name: {"type": "http", "url": url, "headers": {"Authorization": f"Bearer {token}"}}
        for name, url in zip(skills_servers(case), case.skills_mcp, strict=True)
    }
    if fixtures_spec is not None:
        servers[FIXTURES_SERVER] = {
            "type": "stdio",
            "command": python,
            "args": ["-m", "skillhub.agents.fixtures_mcp", str(fixtures_spec)],
            "env": {},
        }
    return {"mcpServers": servers}


def command(binary: Path | str, case: AgentCase, mcp_path: Path, servers: Iterable[str]) -> list[str]:
    servers = list(servers)
    argv = [
        str(binary), "-p", "--output-format", "stream-json", "--verbose",
        "--tools", "",
        "--mcp-config", str(mcp_path), "--strict-mcp-config",
        "--max-turns", str(case.max_turns),
    ]  # fmt: skip
    if servers:
        argv += ["--allowedTools", ",".join(f"mcp__{s}" for s in servers)]
    deny = [f"mcp__{s}__{t}" for s in servers if s != FIXTURES_SERVER for t in sorted(AUTHORING_TOOLS)]
    if deny:
        argv += ["--disallowedTools", ",".join(deny)]
    return argv


def environment(
    *, host: str, token: str, endpoint: str, case: AgentCase, config_dir: Path, base: Mapping[str, str]
) -> dict[str, str]:
    env = {k: base[k] for k in _INHERIT if k in base}
    env.update(
        {
            "ANTHROPIC_BASE_URL": f"{host.rstrip('/')}/serving-endpoints/anthropic",
            "ANTHROPIC_AUTH_TOKEN": token,
            "ANTHROPIC_MODEL": endpoint,
            "ANTHROPIC_SMALL_FAST_MODEL": endpoint,  # background calls go to the same endpoint
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": endpoint,
            "CLAUDE_CONFIG_DIR": str(config_dir),
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "DISABLE_BUG_COMMAND": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_NON_ESSENTIAL_MODEL_CALLS": "1",
            "MCP_TIMEOUT": "30000",
            "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
            "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
            "OTEL_TRACES_EXPORTER": "otlp",
            "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL": "http/protobuf",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": otlp_endpoint(host),
            "OTEL_EXPORTER_OTLP_TRACES_HEADERS": f"Authorization=Bearer {token},{TABLE_HEADER}={case.trace_table}",
            "OTEL_RESOURCE_ATTRIBUTES": ",".join(f"{k}={v}" for k, v in case.attributes.items()),
            "OTEL_BSP_SCHEDULE_DELAY": "1000",
        }
    )
    return env


def parse_stream(lines: Iterable[str]) -> AgentAnswer:
    """The answer from `--output-format stream-json`. An MCP server that did not connect, a run with no `result`
    event, or a result that is an error makes the case an error. Running out of turns is not: the answer so far is
    judged, as in the built-in agent loop, and in trigger mode the first skill call has already been made."""
    result: dict[str, Any] | None = None
    failed: list[str] = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            failed = sorted(
                str(s.get("name")) for s in event.get("mcp_servers") or [] if s.get("status") != "connected"
            )
        elif event.get("type") == "result":
            result = event
    if failed:
        return AgentAnswer("", f"MCP server(s) did not connect: {', '.join(failed)}")
    if result is None:
        return AgentAnswer("", NO_RESULT)
    subtype = result.get("subtype")
    if subtype == "error_max_turns":
        return AgentAnswer(str(result.get("result") or ""))
    if subtype != "success" or result.get("is_error"):
        return AgentAnswer("", f"Claude Code reported {subtype or 'an error'}")
    return AgentAnswer(str(result.get("result") or ""))


def _redact(text: str, *secrets: str) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


def _version_of(binary: Path, run: Runner) -> str | None:
    if not binary.is_file():
        return None
    try:
        out = run([str(binary), "--version"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    found = _VERSION.search(out.stdout or "")
    return found.group(0) if found else None


@contextlib.contextmanager
def _machine_lock(path: Path) -> Iterator[None]:
    """One install at a time on this machine: the eval worker processes may start together."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


_installed: dict[str, Path] = {}
_install_lock = threading.Lock()


def _find(version: str, home: Path, run: Runner) -> Path | None:
    """The installed binary of `version`: its own file, else what `~/.local/bin/claude` points at."""
    for path in (
        home / ".local" / "share" / "claude" / "versions" / version,
        home / ".local" / "bin" / "claude",
    ):
        real = path.resolve() if path.exists() else path
        if _version_of(real, run) == version:
            return real
    return None


def ensure_cli(version: str, *, run: Runner = subprocess.run, home: Path | None = None) -> Path:
    """The `claude` binary of `version`, installed when it is not there yet. The path of the version's own file is
    kept, so two pinned versions in one run do not get in each other's way."""
    if not _VERSION.fullmatch(version):
        raise AgentSetupError(f"Claude Code version {version!r} is not a version number")
    home = home or Path.home()
    with _install_lock:
        known = _installed.get(version)
        if known is not None and known.is_file():
            return known
        with _machine_lock(home / ".local" / "share" / "skillhub-claude-install.lock"):
            binary = _find(version, home, run)
            if binary is None:
                script = f"set -o pipefail; curl -fsSL {INSTALL_URL} | bash -s {version}"
                try:
                    res = run(
                        ["bash", "-c", script], capture_output=True, text=True, timeout=INSTALL_TIMEOUT_S
                    )
                except subprocess.TimeoutExpired:
                    raise AgentSetupError(
                        f"Claude Code {version} was not installed within {INSTALL_TIMEOUT_S} s"
                    ) from None
                binary = _find(version, home, run)
                if binary is None:
                    tail = (res.stderr or res.stdout or "")[-STDERR_TAIL:].strip()
                    raise AgentSetupError(
                        f"Claude Code {version} could not be installed: {tail or res.returncode}"
                    )
        _installed[version] = binary
    return binary


def _bearer() -> str:
    from skillhub.auth import auth_headers

    scheme, _, token = auth_headers().get("Authorization", "").partition(" ")
    if scheme != "Bearer" or not token:
        raise AgentSetupError("the eval identity has no bearer token to give Claude Code")
    return token


class ClaudeCodeAgent:
    harness = HARNESS
    concurrency = CONCURRENCY

    def __init__(
        self,
        *,
        host: str,
        endpoint: str,
        binary: Path,
        token: Callable[[], str] = _bearer,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.host, self.endpoint, self.binary = host, endpoint, binary
        self._token, self._popen, self._clock = token, popen, clock

    @classmethod
    def from_profile(cls, cfg: HubConfig, name: str, profile: ModelProfile) -> ClaudeCodeAgent:
        version = (profile.agent or {}).get("cli_version", "")
        if not profile.endpoint:
            raise AgentSetupError(f"{name}: Claude Code needs a serving endpoint")
        return cls(host=cfg.workspace.host, endpoint=profile.endpoint, binary=ensure_cli(version))

    def run(self, case: AgentCase) -> AgentAnswer:
        start = self._clock()
        token = self._token()
        work = Path(tempfile.mkdtemp(prefix="skillhub-claude-"))
        try:
            fixtures = None
            if case.case is not None and case.case.fixtures and case.fixtures_root is not None:
                fixtures = work / "fixtures.json"
                fixtures.write_text(spec_json(case.fixtures_root, case.case), encoding="utf-8")
            config = mcp_config(case, token, fixtures)
            mcp_path = work / "mcp.json"
            fd = os.open(mcp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # it holds the token
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(config, f)
            argv = command(self.binary, case, mcp_path, config["mcpServers"])
            env = environment(
                host=self.host, token=token, endpoint=self.endpoint, case=case, config_dir=work / "config",
                base=os.environ,
            )  # fmt: skip
            timeout = TRIGGER_TIMEOUT_S if case.mode == "trigger" else CASE_TIMEOUT_S
            proc = self._popen(
                argv, cwd=work, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
            )  # fmt: skip
            try:
                out, err = proc.communicate(
                    case.prompt, timeout=timeout
                )  # on stdin: a leading "-" is no flag
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                return AgentAnswer(
                    "", f"Claude Code did not finish within {timeout} s", self._clock() - start
                )
            answer = parse_stream(out.splitlines())
            if answer.error == NO_RESULT and proc.returncode != 0:  # the end of its stderr says why
                tail = _redact(err or "", token)[-STDERR_TAIL:].strip()
                answer = AgentAnswer("", f"Claude Code exited with {proc.returncode}: {tail}")
            elif answer.error is not None:
                answer = AgentAnswer("", _redact(answer.error, token))
            return AgentAnswer(answer.text, answer.error, self._clock() - start)
        finally:
            shutil.rmtree(work, ignore_errors=True)
