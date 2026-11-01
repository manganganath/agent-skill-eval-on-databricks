"""The sample AgentCore agent's contract module (examples/agentcore/skill-agent/contract.py) against the Skill Hub's
own code: the payload the eval Job builds parses, its tool names map back to the skills, and its fixture tools answer
as the built-in agent loop's do. The module uses the standard library only, so it is loaded from its file."""

import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from skillhub.agent.fixtures import FixtureStore
from skillhub.agents.agentcore import payload
from skillhub.agents.contract import AgentCase, attributes
from skillhub.agents.traces import tool_names
from skillhub.evalspec import EvalCase

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "fixtures"


def _load():
    spec = importlib.util.spec_from_file_location(
        "skill_agent_contract", ROOT / "examples/agentcore/skill-agent/contract.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their annotations through sys.modules
    spec.loader.exec_module(module)
    return module


sample = _load()
CASE = EvalCase(id=101, kind="effectiveness", prompt="Audit model.xlsx", should_trigger=True,
                fixtures=["northwind/model.xlsx", "northwind/press_release_q3.md", "northwind/consensus_q3.json"])  # fmt: skip


def job_payload(mode="with_skill", case=CASE):
    attrs = attributes(
        run_key="hub_1", trace_key="ab" * 16, case_id=case.id, repeat=0, mode=mode, profile="sa"
    )
    urls = (
        ()
        if mode == "without_skill"
        else ("https://example.invalid/ai-gateway/skills/?schema=skill_hub.common",)
    )
    job = AgentCase(prompt=case.prompt, mode=mode, skills_mcp=urls, attributes=attrs,
                    trace_table="skill_hub.common_evals.traces_otel_spans", max_turns=8, case=case,
                    fixtures_root=FIXTURES)  # fmt: skip
    return json.loads(json.dumps(payload(job, "us.anthropic.claude-sonnet-5-5", "https://example.invalid")))


def test_the_payload_the_eval_job_sends_is_a_case_for_the_sample_agent():
    case = sample.Case.from_payload(job_payload())
    assert (case.mode, case.model, case.max_turns, case.uc_table) == (
        "with_skill", "us.anthropic.claude-sonnet-5-5", 8, "skill_hub.common_evals.traces_otel_spans",
    )  # fmt: skip
    assert case.trace["skillhub.case_id"] == "101" and set(case.files) == set(CASE.fixtures)
    assert sample.Case.from_payload(job_payload("without_skill")).skills_mcp == ()


@pytest.mark.parametrize(
    ("change", "says"),
    [
        ({"mode": "sideways"}, "mode:"),
        ({"prompt": ""}, "prompt:"),
        ({"mode": "without_skill"}, "a without_skill case must not load skills"),
        ({"max_turns": True}, "max_turns:"),
        ({"trace": {"skillhub.case_id": "1,2"}}, "trace:"),
        ({"otel": {"uc_table": "x; DROP"}}, "otel.uc_table:"),
    ],
)
def test_a_payload_that_is_not_a_case_is_refused_in_plain_words(change, says):
    with pytest.raises(ValueError, match=says):
        sample.Case.from_payload({**job_payload(), **change})


@pytest.mark.parametrize(
    ("fqn", "model"),
    [
        ("skill_hub.common.audit-xls", "us.anthropic.claude-sonnet-5-5"),
        ("skill_hub.common.audit-xls", "amazon.nova-pro-v1:0"),
        (
            "skill_hub.dept_investment_banking.merger-model-sensitivity-tables-and-football-field",
            "us.anthropic.x",
        ),
        (
            "skill_hub.dept_investment_banking.merger-model-sensitivity-tables-and-football-field",
            "us.amazon.nova-pro",
        ),
    ],
)
def test_the_sample_agents_tool_names_map_back_to_the_skill(fqn, model):
    name = sample.safe_tool_name(f"skill_{fqn}", plain=sample.plain_names(model))
    assert len(name) <= 64 and tool_names([fqn])[name] == fqn.rsplit(".", 1)[-1]


def test_the_sample_agents_fixture_tools_answer_as_the_agent_loops_do():
    body = job_payload()
    tools = sample.FixtureTools(body["files"], body["search_index"])
    store = FixtureStore(FIXTURES)
    assert tools.descriptions() == {t.name: t.description for t in store.tools_for(CASE)}
    for name, args in [
        ("read_workbook", {"path": "northwind/model.xlsx"}),
        ("read_file", {"path": "fixtures/northwind/press_release_q3.md"}),
        ("read_file", {"path": "consensus_q3.json"}),
        ("read_file", {"path": "northwind/transcript_q3.md"}),
        ("web_search", {"query": "Northwind quarter results consensus"}),
        ("web_search", {"query": "nothing matches this"}),
    ]:
        assert getattr(tools, name)(**args) == store.call(name, args, CASE), (name, args)


def test_the_agents_token_goes_only_to_its_own_workspace():
    sample.check_skills_urls(
        ["https://example.invalid/ai-gateway/skills/?schema=a.b"], "https://example.invalid/"
    )
    with pytest.raises(ValueError, match="only the skills MCP of https://example.invalid"):
        sample.check_skills_urls(
            ["https://example.invalid.evil.test/ai-gateway/skills/?schema=a.b"], "https://example.invalid"
        )


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_the_m2m_token_is_cached_until_a_minute_before_it_expires():
    calls, now = [], [1000.0]

    def opener(request, timeout):
        calls.append((request.full_url, request.get_header("Authorization"), request.data))
        return _Response(json.dumps({"access_token": f"tok-{len(calls)}", "expires_in": 3600}).encode())

    token = sample.M2MToken("https://example.invalid/", "client-1", lambda: "test-secret-0001", opener=opener,
                            clock=lambda: now[0])  # fmt: skip
    assert token() == token() == "tok-1" and len(calls) == 1
    url, auth, data = calls[0]
    assert url == "https://example.invalid/oidc/v1/token" and auth.startswith("Basic ")
    assert data == b"grant_type=client_credentials&scope=all-apis"
    now[0] += 3600 - 59
    assert token() == "tok-2"


def test_a_failed_token_fetch_names_no_secret():
    def broken(request, timeout):
        raise OSError(f"refused: {request.get_header('Authorization')}")

    token = sample.M2MToken("https://example.invalid", "client-1", lambda: "test-secret-0001", opener=broken)
    with pytest.raises(RuntimeError) as err:
        token()
    assert str(err.value) == "the agent's Databricks token could not be fetched (OSError)"


def test_spans_are_sorted_by_the_trace_table_their_case_named():
    a = SimpleNamespace(attributes={sample.UC_TABLE_ATTR: "c.s.traces_otel_spans"})
    b = SimpleNamespace(attributes={sample.UC_TABLE_ATTR: "c.t.traces_otel_spans"})
    loose = SimpleNamespace(attributes={})
    odd = SimpleNamespace(attributes={sample.UC_TABLE_ATTR: "c.s.t; DROP"})
    assert sample.group_by_table([a, b, loose, odd, a]) == {
        "c.s.traces_otel_spans": [a, a],
        "c.t.traces_otel_spans": [b],
    }
