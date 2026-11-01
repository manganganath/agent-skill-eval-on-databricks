import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from skillhub.agents import agent_for, claude_code
from skillhub.agents.claude_code import (
    AgentSetupError,
    ClaudeCodeAgent,
    command,
    environment,
    mcp_config,
    parse_stream,
)
from skillhub.agents.contract import AgentAnswer, AgentCase, attributes
from skillhub.config import ModelProfile
from skillhub.evalspec import EvalCase
from tests.fakes import make_cfg

RECORDED = Path(__file__).with_name("recorded")
HOST = "https://example.invalid"
TOKEN = "test-token-0001"
URLS = (
    f"{HOST}/ai-gateway/skills/?schema=skill_hub.dept_equity_research",
    f"{HOST}/ai-gateway/skills/?schema=skill_hub.common",
)
CASE = EvalCase(id=101, kind="effectiveness", prompt="-Audit model.xlsx", should_trigger=True,
                fixtures=["northwind/model.xlsx"])  # fmt: skip


def agent_case(mode="with_skill", urls=URLS, case=CASE, root=Path("/fixtures")):
    attrs = attributes(
        run_key="hub_1", trace_key="ab" * 16, case_id=case.id, repeat=0, mode=mode, profile="cc"
    )
    return AgentCase(prompt=case.prompt, mode=mode, skills_mcp=urls if mode != "without_skill" else (),
                     attributes=attrs, trace_table="skill_hub.dept_equity_research_evals.traces_otel_spans",
                     max_turns=3 if mode == "trigger" else 8, case=case, fixtures_root=root)  # fmt: skip


def lines(name):
    return (RECORDED / name).read_text(encoding="utf-8").splitlines()


def test_runs_headless_with_no_built_in_tool_and_only_the_servers_it_is_given():
    argv = command("/bin/claude", agent_case(), Path("/w/mcp.json"), ["s0", "s1", "fx"])
    assert argv[:5] == ["/bin/claude", "-p", "--output-format", "stream-json", "--verbose"]
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--mcp-config") + 1] == "/w/mcp.json" and "--strict-mcp-config" in argv
    assert argv[argv.index("--max-turns") + 1] == "8"
    assert argv[argv.index("--allowedTools") + 1] == "mcp__s0,mcp__s1,mcp__fx"
    denied = argv[argv.index("--disallowedTools") + 1].split(",")
    assert set(denied) == {
        f"mcp__{s}__{t}" for s in ("s0", "s1") for t in ("create_skill", "update_skill", "delete_skill")
    }
    assert CASE.prompt not in argv  # the prompt goes on stdin


def test_trigger_mode_has_three_turns():
    argv = command("claude", agent_case("trigger"), Path("m.json"), ["s0", "s1"])
    assert argv[argv.index("--max-turns") + 1] == "3"


def test_one_skills_server_per_schema_and_the_fixtures_server_without_any_credential():
    config = mcp_config(agent_case(), TOKEN, Path("/w/fixtures.json"), python="/py")
    servers = config["mcpServers"]
    assert list(servers) == ["s0", "s1", "fx"]
    assert servers["s0"] == {"type": "http", "url": URLS[0], "headers": {"Authorization": f"Bearer {TOKEN}"}}
    assert servers["fx"] == {"type": "stdio", "command": "/py",
                             "args": ["-m", "skillhub.agents.fixtures_mcp", "/w/fixtures.json"], "env": {}}  # fmt: skip
    assert list(mcp_config(agent_case("without_skill"), TOKEN, None)["mcpServers"]) == []


def test_the_cli_reaches_the_anthropic_route_and_exports_its_spans_to_the_departments_table():
    base = {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "DATABRICKS_TOKEN": "job-token",
        "AWS_SECRET_ACCESS_KEY": "k",
    }
    env = environment(host=HOST, token=TOKEN, endpoint="databricks-claude-sonnet-5-5", case=agent_case(),
                      config_dir=Path("/w/config"), base=base)  # fmt: skip
    assert env["ANTHROPIC_BASE_URL"] == f"{HOST}/serving-endpoints/anthropic"
    assert (env["ANTHROPIC_AUTH_TOKEN"], env["ANTHROPIC_MODEL"]) == (TOKEN, "databricks-claude-sonnet-5-5")
    assert env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] == f"{HOST}/api/2.0/otel/v1/traces"
    assert env["OTEL_EXPORTER_OTLP_TRACES_HEADERS"] == (
        f"Authorization=Bearer {TOKEN},X-Databricks-UC-Table-Name=skill_hub.dept_equity_research_evals.traces_otel_spans"
    )
    resource = dict(kv.split("=") for kv in env["OTEL_RESOURCE_ATTRIBUTES"].split(","))
    assert resource["skillhub.case_id"] == "101" and resource["skillhub.mode"] == "with_skill"
    assert (env["CLAUDE_CODE_ENABLE_TELEMETRY"], env["OTEL_TRACES_EXPORTER"]) == ("1", "otlp")
    assert (
        "DATABRICKS_TOKEN" not in env and "AWS_SECRET_ACCESS_KEY" not in env
    )  # the Job's credentials stay out
    assert env["PATH"] == "/usr/bin" and env["CLAUDE_CONFIG_DIR"] == "/w/config"


def test_the_answer_is_the_result_event():
    assert parse_stream(lines("claude_code_with_skill.jsonl")) == AgentAnswer(
        json.loads(lines("claude_code_with_skill.jsonl")[-1])["result"]
    )


@pytest.mark.parametrize(
    ("stream", "error"),
    [
        (lines("claude_code_mcp_failed.jsonl"), "MCP server(s) did not connect: s0"),
        (['{"type":"system","subtype":"init","mcp_servers":[]}'], "Claude Code ended without a result"),
        (
            ['{"type":"result","subtype":"error_during_execution","is_error":true}'],
            "Claude Code reported error_during_execution",
        ),
        (
            ['{"type":"result","subtype":"success","is_error":true,"result":"API Error: 401"}'],
            "Claude Code reported success",
        ),
    ],
)
def test_a_run_that_did_not_get_its_servers_or_its_answer_is_an_error(stream, error):
    assert parse_stream(stream) == AgentAnswer("", error)


def test_running_out_of_turns_is_judged_not_an_error_and_other_output_is_ignored():
    stream = ["Update available!", "[]", '{"type":"result","subtype":"error_max_turns","num_turns":3}']
    assert parse_stream(stream) == AgentAnswer("")


def fake_cli(tmp_path, stream, *, exit_code=0, stderr=""):
    """A `claude` stand-in: it saves what it was given and prints a recorded stream."""
    seen = tmp_path / "seen.json"
    script = tmp_path / "claude"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, stat, sys\n"
        "argv = sys.argv[1:]\n"
        "mcp = argv[argv.index('--mcp-config') + 1]\n"
        f"json.dump({{'argv': argv, 'env': dict(os.environ), 'stdin': sys.stdin.read(), 'cwd': os.getcwd(),\n"
        "           'mcp': json.load(open(mcp)), 'mode': oct(os.stat(mcp).st_mode & 0o777)}, "
        f"open({str(seen)!r}, 'w'))\n"
        f"sys.stdout.write({stream!r})\n"
        f"sys.stderr.write({stderr!r})\n"
        f"sys.exit({exit_code})\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script, seen


def test_a_case_runs_the_cli_in_a_scratch_folder_that_is_gone_afterwards(tmp_path):
    binary, seen = fake_cli(tmp_path, "\n".join(lines("claude_code_with_skill.jsonl")))
    agent = ClaudeCodeAgent(
        host=HOST, endpoint="databricks-claude-sonnet-5-5", binary=binary, token=lambda: TOKEN
    )
    answer = agent.run(agent_case(root=tmp_path))
    assert answer.text.startswith("**Model type:** 3-statement") and answer.error is None
    got = json.loads(seen.read_text())
    assert got["stdin"] == "-Audit model.xlsx" and got["mode"] == "0o600"
    assert list(got["mcp"]["mcpServers"]) == ["s0", "s1", "fx"]
    assert not Path(got["cwd"]).exists()  # the scratch folder, with the token file, is removed
    assert got["env"]["ANTHROPIC_AUTH_TOKEN"] == TOKEN


def test_a_failed_cli_is_an_error_that_never_shows_the_token(tmp_path):
    binary, _ = fake_cli(tmp_path, "", exit_code=2, stderr=f"401 for Bearer {TOKEN}\n")
    agent = ClaudeCodeAgent(host=HOST, endpoint="e", binary=binary, token=lambda: TOKEN)
    answer = agent.run(agent_case(root=tmp_path))
    assert answer.text == "" and answer.error == "Claude Code exited with 2: 401 for Bearer ***"


def test_a_cli_that_does_not_finish_in_time_is_killed_and_is_an_error(tmp_path, monkeypatch):
    class Slow:
        returncode = None

        def __init__(self, *a, **k):
            self.killed = False

        def communicate(self, data=None, timeout=None):
            if timeout is not None and not self.killed:
                raise subprocess.TimeoutExpired("claude", timeout)
            return "", ""

        def kill(self):
            self.killed = True

    agent = ClaudeCodeAgent(host=HOST, endpoint="e", binary=Path("claude"), token=lambda: TOKEN, popen=Slow)
    assert agent.run(agent_case("trigger")).error == "Claude Code did not finish within 300 s"


def test_a_cli_whose_child_keeps_the_pipe_open_is_killed_with_its_group_and_returns(tmp_path, monkeypatch):
    """The fixtures server is a child of the CLI and inherits its stderr: killing only the CLI would leave
    the read waiting until that child ends."""
    child_pid = tmp_path / "child.pid"
    script = tmp_path / "claude"
    script.write_text(
        f"#!{sys.executable}\n"
        "import subprocess, sys, time\n"
        f"child = subprocess.Popen([{sys.executable!r}, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(child_pid)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(claude_code, "TRIGGER_TIMEOUT_S", 2)
    agent = ClaudeCodeAgent(host=HOST, endpoint="e", binary=script, token=lambda: TOKEN)
    started = time.monotonic()
    answer = agent.run(agent_case("trigger", root=tmp_path))
    assert answer.error == "Claude Code did not finish within 2 s"
    assert time.monotonic() - started < 20
    pid = int(child_pid.read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(pid, signal.SIGKILL)
        pytest.fail("the CLI's child is still running")


class Installer:
    """Stands in for subprocess.run: `--version` answers what is on disk, the install script writes the binary."""

    def __init__(self, home, version, works=True):
        self.home, self.version, self.works, self.installs = home, version, works, 0

    def __call__(self, argv, **kw):
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, stdout=Path(argv[0]).read_text() + " (Claude Code)\n")
        self.installs += 1
        assert argv[:2] == ["bash", "-c"] and argv[2].endswith(f"| bash -s {self.version}")
        assert "curl -fsSL https://claude.ai/install.sh" in argv[2] and "pipefail" in argv[2]
        if self.works:
            path = self.home / ".local" / "share" / "claude" / "versions" / self.version
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self.version)
        return subprocess.CompletedProcess(
            argv, 0 if self.works else 1, stdout="", stderr="curl: (6) no host"
        )


def test_the_pinned_version_is_installed_once_and_then_reused(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_code, "_installed", {})
    run = Installer(tmp_path, "2.1.314")
    first = claude_code.ensure_cli("2.1.314", run=run, home=tmp_path)
    assert first == tmp_path / ".local/share/claude/versions/2.1.314" and run.installs == 1
    assert claude_code.ensure_cli("2.1.314", run=run, home=tmp_path) == first and run.installs == 1


def test_the_installer_and_the_version_check_run_without_the_jobs_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_code, "_installed", {})
    monkeypatch.setenv("DATABRICKS_TOKEN", TOKEN)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-key-id-0001")
    monkeypatch.setenv("HOME", str(tmp_path))
    seen = []

    class Recording(Installer):
        def __call__(self, argv, **kw):
            seen.append(kw.get("env"))
            return super().__call__(argv, **kw)

    claude_code.ensure_cli("2.1.314", run=Recording(tmp_path, "2.1.314"), home=tmp_path)
    assert len(seen) >= 2  # the install and the version check after it
    for env in seen:
        assert env is not None and "DATABRICKS_TOKEN" not in env and "AWS_SECRET_ACCESS_KEY" not in env
        assert env["HOME"] == str(tmp_path) and "PATH" in env


def test_an_install_that_fails_is_a_setup_error_that_names_the_version(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_code, "_installed", {})
    with pytest.raises(
        AgentSetupError, match=r"Claude Code 2\.1\.314 could not be installed: curl: \(6\) no host"
    ):
        claude_code.ensure_cli("2.1.314", run=Installer(tmp_path, "2.1.314", works=False), home=tmp_path)
    with pytest.raises(AgentSetupError, match="not a version number"):
        claude_code.ensure_cli("latest; rm -rf /", run=Installer(tmp_path, "x"), home=tmp_path)


def test_a_claude_code_profile_builds_its_agent(monkeypatch, tmp_path):
    cfg = make_cfg()
    profile = ModelProfile(route="ug-anthropic", endpoint="databricks-claude-sonnet-5-5", kind="external_agent",
                           harness="claude-code", agent={"cli_version": "2.1.314"}, **{"class": "frontier"})  # fmt: skip
    cfg.models["cc"] = profile
    monkeypatch.setattr(claude_code, "ensure_cli", lambda version: tmp_path / version)
    agent = agent_for(cfg, "cc")
    assert isinstance(agent, ClaudeCodeAgent) and agent.binary == tmp_path / "2.1.314"
    assert (agent.endpoint, agent.host, agent.concurrency) == (
        "databricks-claude-sonnet-5-5",
        cfg.workspace.host,
        2,
    )
