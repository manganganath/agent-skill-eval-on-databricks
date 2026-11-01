from datetime import UTC, datetime

import httpx
import openai
import pytest
from typer.testing import CliRunner

from skillhub import cli, probe
from skillhub.gateway.aws import AwsCredentialsError
from skillhub.gateway.bedrock import BedrockCallError
from skillhub.gateway.models import ChatResult
from skillhub.model_catalog import store
from skillhub.model_catalog.entry import CatalogEntry, Target
from skillhub.probe import ProbeResult, parse_result, run_probe
from tests.fakes import FakeSql, make_cfg

NOVA = CatalogEntry(
    name="nova-lite", kind="external_model", route="bedrock", model_id="amazon.nova-lite-v1:0",
    region="us-east-1", model_class="fast",
)  # fmt: skip
AGENT = CatalogEntry(name="skill-agent", kind="external_agent", harness="agentcore", model_class="frontier")
OK = ChatResult("ok", [], "stop", {"prompt_tokens": 9, "completion_tokens": 1}, {"role": "assistant"})


class Client:
    def __init__(self, answer=OK):
        self.answer = answer
        self.calls = []

    def chat(self, messages, tools, *, max_tokens=None, trigger=False):
        self.calls.append((messages, tools, max_tokens))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def ticks(*values):
    it = iter(values)
    return lambda: next(it)


def test_a_disabled_row_is_tested_with_one_short_call_of_the_merged_profile():
    seen, client = {}, Client()

    def client_for(cfg, name):
        seen["profile"] = cfg.models[name]
        return client

    result = run_probe(
        make_cfg(), [NOVA], 5, "nova-lite", NOVA.target(), client_for=client_for, clock=ticks(1.0, 1.25)
    )
    assert result == ProbeResult("nova-lite", "passed", "answered in 250 ms", 250)
    assert seen["profile"].route == "bedrock" and seen["profile"].catalog_version == 5
    [(messages, tools, max_tokens)] = client.calls
    assert tools == [] and max_tokens == probe.MAX_TOKENS == 16
    assert messages == [{"role": "user", "content": probe.PROMPT}]


@pytest.mark.parametrize(
    ("entries", "name", "expect", "says"),
    [
        ([NOVA], "nope", NOVA.target(), "there is no model named 'nope'"),
        (
            [AGENT],
            "skill-agent",
            AGENT.target(),
            "skill-agent cannot be tested: route: agentcore reaches its model",
        ),
        (
            [NOVA],
            "nova-lite",
            Target("bedrock", "", "amazon.nova-lite-v1:0", "us-west-2"),
            "the model changed",
        ),
    ],
)
def test_a_row_that_cannot_be_tested_as_asked_fails_without_a_call(entries, name, expect, says):
    client = Client()
    result = run_probe(make_cfg(), entries, None, name, expect, client_for=lambda c, n: client)
    assert result.status == "failed" and says in result.message
    assert client.calls == []


@pytest.mark.parametrize(
    "error",
    [
        AwsCredentialsError("the secret scope 'skillhub-aws' has no aws-access-key-id key"),
        BedrockCallError("bedrock call failed: AccessDeniedException on amazon.nova-lite-v1:0"),
    ],
)
def test_a_client_that_cannot_be_built_or_called_is_a_failed_test(error):
    builds = {"client": lambda c, n: (_ for _ in ()).throw(error), "call": lambda c, n: Client(error)}
    for client_for in builds.values():
        result = run_probe(make_cfg(), [NOVA], None, "nova-lite", NOVA.target(), client_for=client_for)
        assert result == ProbeResult("nova-lite", "failed", f"{type(error).__name__}: {error}")


def test_the_result_line_reads_back_and_junk_is_ignored():
    line = ProbeResult("nova-lite", "failed", "x" * 900, None).line()
    logs = f"INFO starting\n{ProbeResult('nova-lite', 'passed', 'old').line()}\n  {line}\nINFO done\n"
    got = parse_result(logs)
    assert (got.status, len(got.message)) == ("failed", probe.MESSAGE_MAX)
    for junk in (None, "", "SKILLHUB_PROBE not json", 'SKILLHUB_PROBE {"status": "maybe"}', "no result here"):
        assert parse_result(junk) is None


@pytest.fixture
def probe_cli(monkeypatch):
    cfg, sql = make_cfg(), FakeSql()
    store.ensure_table(cfg, sql)
    store.insert_entry(sql, cfg, NOVA, "rita@example.com", datetime(2026, 10, 31, tzinfo=UTC))
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(cli, "workspace_client", lambda: object())
    monkeypatch.setattr("skillhub.sql.SqlClient", lambda *a, **k: sql)
    client = Client()
    monkeypatch.setattr(probe, "_client", lambda c, n: client)

    def invoke(**target):
        args = ["probe", "--name", "nova-lite", "--requested-by", "rita@example.com"]
        for k, v in {**NOVA.target().params(), **target}.items():
            args += [f"--{k.replace('_', '-')}", v]
        return CliRunner().invoke(cli.app, args)

    return invoke, client


def test_the_probe_command_prints_one_result_line_and_exits_0_when_it_passed(probe_cli):
    invoke, client = probe_cli
    result = invoke()
    assert result.exit_code == 0, result.output
    got = parse_result(result.stdout)
    assert (got.name, got.status) == ("nova-lite", "passed")
    assert len(client.calls) == 1


def test_the_probe_command_exits_1_when_the_test_failed(probe_cli):
    invoke, client = probe_cli
    result = invoke(region="us-west-2")
    assert result.exit_code == 1
    assert parse_result(result.stdout).status == "failed"
    assert client.calls == []


def test_a_catalog_the_identity_cannot_read_is_a_failed_test(monkeypatch, probe_cli):
    invoke, _ = probe_cli

    class Denied:
        def execute(self, *a, **k):
            raise RuntimeError("PERMISSION_DENIED: no SELECT on skill_hub.eval.model_profiles")

    monkeypatch.setattr("skillhub.sql.SqlClient", lambda *a, **k: Denied())
    result = invoke()
    assert result.exit_code == 1
    assert "this identity cannot read the model catalog" in parse_result(result.stdout).message


@pytest.mark.parametrize("where", ["client", "call"])
def test_an_error_of_an_unknown_class_never_puts_its_text_on_the_result_line(where):
    error = RuntimeError("request failed with key test-secret-0001 in the header")
    client_for = (
        (lambda c, n: (_ for _ in ()).throw(error)) if where == "client" else lambda c, n: Client(error)
    )
    result = run_probe(make_cfg(), [NOVA], None, "nova-lite", NOVA.target(), client_for=client_for)
    assert result == ProbeResult("nova-lite", "failed", "RuntimeError: the call failed")
    assert "test-secret-0001" not in result.line()


def test_a_bedrock_call_error_keeps_its_message():
    error = BedrockCallError("the model amazon.nova-lite-v1:0 refused the call: ThrottlingException")
    result = run_probe(
        make_cfg(), [NOVA], None, "nova-lite", NOVA.target(), client_for=lambda c, n: Client(error)
    )
    assert result.message == f"BedrockCallError: {error}"


def test_a_serving_endpoint_refusal_keeps_the_endpoints_message():
    request = httpx.Request("POST", "https://example.cloud.databricks.com/serving-endpoints/chat/completions")
    response = httpx.Response(403, request=request)
    error = openai.PermissionDeniedError(
        "PERMISSION_DENIED: no CAN_QUERY on the endpoint", response=response, body=None
    )
    result = run_probe(
        make_cfg(), [NOVA], None, "nova-lite", NOVA.target(), client_for=lambda c, n: Client(error)
    )
    assert result.message == "PermissionDeniedError: PERMISSION_DENIED: no CAN_QUERY on the endpoint"


CC = CatalogEntry(
    name="cc-sonnet", kind="external_agent", harness="claude-code", route="ug-anthropic", model_class="frontier",
    endpoint="databricks-claude-sonnet-5-5", agent={"cli_version": "2.1.314"},
)  # fmt: skip


class Agent:
    def __init__(self, answer=None):
        from skillhub.agents.contract import AgentAnswer

        self.answer = answer or AgentAnswer("ok")
        self.cases = []

    def run(self, case):
        self.cases.append(case)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def probe_agent_row(agent, found=True, entries=(CC,)):
    waits = []

    def wait(sql, table, expected, **kw):
        waits.append((table, expected, kw))
        return {(0, 0): "trace:/x/1"} if found else {}

    result = run_probe(
        make_cfg(), list(entries), 6, "cc-sonnet", CC.target(), sql="SQL", agent_for=lambda cfg, name: agent,
        wait=wait, clock=ticks(1.0, 3.5),
    )  # fmt: skip
    return result, waits


def test_an_agent_is_tested_with_one_case_on_commons_skills_and_its_trace():
    agent = Agent()
    result, [(table, expected, kw)] = probe_agent_row(agent)
    assert result == ProbeResult(
        "cc-sonnet",
        "passed",
        "answered in 2500 ms, and its trace reached skill_hub.common_evals.traces_otel_spans",
        2500,
    )
    [case] = agent.cases
    assert case.prompt == probe.PROMPT and case.mode == "trigger" and case.max_turns == 1
    assert case.skills_mcp == ("https://example.invalid/ai-gateway/skills/?schema=skill_hub.common",)
    assert table == case.trace_table == "skill_hub.common_evals.traces_otel_spans" and expected == {(0, 0)}
    assert kw["run_key"] == case.attributes["skillhub.run_key"] and kw["run_key"].startswith("probe_")
    assert kw["trace_key"] == case.attributes["skillhub.trace_key"] and kw["timeout_s"] == 120


def test_an_agent_whose_trace_does_not_arrive_fails_its_test():
    result, _ = probe_agent_row(Agent(), found=False)
    assert result.status == "failed"
    assert result.message == (
        "the agent answered in 2500 ms, but its trace did not reach skill_hub.common_evals.traces_otel_spans within 120 s"
    )


def test_an_agent_that_does_not_answer_or_cannot_be_built_fails_without_a_wait():
    from skillhub.agents.claude_code import AgentSetupError
    from skillhub.agents.contract import AgentAnswer

    result, waits = probe_agent_row(Agent(AgentAnswer("", "MCP server(s) did not connect: s0")))
    assert result.message == "the agent did not answer: MCP server(s) did not connect: s0" and waits == []
    broken = run_probe(
        make_cfg(), [CC], 6, "cc-sonnet", CC.target(), sql="SQL",
        agent_for=lambda c, n: (_ for _ in ()).throw(AgentSetupError("Claude Code 2.1.314 could not be installed: x")),
    )  # fmt: skip
    assert broken.message == "AgentSetupError: Claude Code 2.1.314 could not be installed: x"


def test_an_agent_whose_settings_changed_after_the_test_started_is_not_run():
    agent = Agent()
    moved = CC.model_copy(update={"agent": '{"cli_version":"2.1.400"}'})
    result = run_probe(
        make_cfg(), [moved], 6, "cc-sonnet", CC.target(), sql="SQL", agent_for=lambda c, n: agent
    )
    assert "the model changed" in result.message and agent.cases == []


def test_the_probe_command_passes_the_agent_settings_it_was_asked_to_test(monkeypatch):
    cfg, sql = make_cfg(), FakeSql()
    store.ensure_table(cfg, sql)
    store.insert_entry(sql, cfg, CC, "rita@example.com", datetime(2026, 11, 1, tzinfo=UTC))
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(cli, "workspace_client", lambda: object())
    monkeypatch.setattr("skillhub.sql.SqlClient", lambda *a, **k: sql)
    seen = {}

    def fake(cfg, entries, version, name, expect, **kw):
        seen.update(expect=expect, sql=kw.get("sql"))
        return ProbeResult(name, "passed", "ok", 1)

    monkeypatch.setattr(probe, "run_probe", fake)
    args = ["probe", "--name", "cc-sonnet"]
    for k, v in CC.target().params().items():
        args += [f"--{k.replace('_', '-')}", v]
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert seen == {"expect": CC.target(), "sql": sql}


def test_a_trace_that_cannot_be_read_or_a_case_that_cannot_be_built_fails_the_test(monkeypatch):
    def boom(*a, **k):
        raise ValueError("no warehouse")

    unread = run_probe(
        make_cfg(), [CC], 6, "cc-sonnet", CC.target(), sql="SQL", agent_for=lambda c, n: Agent(), wait=boom,
        clock=ticks(1.0, 3.5),
    )  # fmt: skip
    assert unread.status == "failed" and "its trace could not be read" in unread.message
    assert unread.message.startswith("the agent answered in 2500 ms")

    agent = Agent()
    monkeypatch.setattr("skillhub.agents.contract.trace_table", boom)
    unbuilt = run_probe(
        make_cfg(), [CC], 6, "cc-sonnet", CC.target(), sql="SQL", agent_for=lambda c, n: agent
    )
    assert unbuilt.status == "failed" and agent.cases == []
