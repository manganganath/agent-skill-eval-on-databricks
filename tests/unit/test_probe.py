from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from skillhub import cli, probe
from skillhub.gateway.aws import AwsCredentialsError
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
        ([AGENT], "skill-agent", AGENT.target(), "a external_agent cannot be tested"),
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
        RuntimeError("PERMISSION_DENIED: no CAN_QUERY on the endpoint"),
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
