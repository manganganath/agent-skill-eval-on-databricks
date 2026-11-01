import io
import json
from pathlib import Path

import boto3
import pytest
from botocore.response import StreamingBody
from botocore.stub import ANY, Stubber

from skillhub.agents import agent_for, agentcore
from skillhub.agents.agentcore import AgentCoreAgent, payload, session_id
from skillhub.agents.contract import AgentAnswer, AgentCase, attributes
from skillhub.config import ModelProfile
from skillhub.evalspec import EvalCase
from skillhub.gateway.aws import AwsKeys
from tests.fakes import make_cfg

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"
ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/skillhub_skill_agent-AbCd123456"
HOST = "https://example.invalid"
KEYS = AwsKeys("test-key-id-0001", "test-secret-0001")
CASE = EvalCase(id=101, kind="effectiveness", prompt="Audit model.xlsx", should_trigger=True,
                fixtures=["northwind/model.xlsx", "northwind/press_release_q3.md"])  # fmt: skip


def agent_case(mode="with_skill", case=CASE):
    attrs = attributes(
        run_key="hub_1", trace_key="ab" * 16, case_id=case.id, repeat=1, mode=mode, profile="sa"
    )
    urls = () if mode == "without_skill" else (f"{HOST}/ai-gateway/skills/?schema=skill_hub.common",)
    return AgentCase(prompt=case.prompt, mode=mode, skills_mcp=urls, attributes=attrs,
                     trace_table="skill_hub.common_evals.traces_otel_spans", max_turns=8, case=case,
                     fixtures_root=FIXTURES)  # fmt: skip


def answer(body, content_type="application/json"):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return {"response": StreamingBody(io.BytesIO(raw), len(raw)), "contentType": content_type, "statusCode": 200,
            "runtimeSessionId": "s" * 40}  # fmt: skip


def stubbed():
    client = boto3.client("bedrock-agentcore", region_name="us-east-1", aws_access_key_id="test-key-id-0001",
                          aws_secret_access_key="test-secret-0001")  # fmt: skip
    return client, Stubber(client)


def agent(client, sleeps=None):
    return AgentCoreAgent(runtime_arn=ARN, model_id="us.anthropic.claude-sonnet-5-5", host=HOST, client=client,
                          concurrency=6, keys=KEYS, sleep=(sleeps.append if sleeps is not None else lambda s: None))  # fmt: skip


def expected(body=ANY):
    return {"agentRuntimeArn": ARN, "runtimeSessionId": ANY, "payload": body, "contentType": "application/json",
            "accept": "application/json"}  # fmt: skip


def test_the_payload_holds_the_contract_and_the_cases_files_as_the_fixture_tools_show_them():
    body = payload(agent_case(), "us.anthropic.claude-sonnet-5-5", HOST)
    assert (body["prompt"], body["mode"], body["model"], body["max_turns"]) == (
        "Audit model.xlsx", "with_skill", "us.anthropic.claude-sonnet-5-5", 8,
    )  # fmt: skip
    assert body["skills_mcp"] == [f"{HOST}/ai-gateway/skills/?schema=skill_hub.common"]
    assert body["trace"]["skillhub.case_id"] == "101" and body["trace"]["skillhub.repeat"] == "1"
    assert body["otel"] == {
        "endpoint": f"{HOST}/api/2.0/otel/v1/traces",
        "uc_table": "skill_hub.common_evals.traces_otel_spans",
    }
    assert set(body["files"]) == {"northwind/model.xlsx", "northwind/press_release_q3.md"}
    assert "formula=" in body["files"]["northwind/model.xlsx"]  # the workbook as read_workbook shows it
    assert all(set(docs) <= set(body["files"]) for docs in body["search_index"].values())
    assert payload(agent_case("without_skill"), "m", HOST)["skills_mcp"] is None


def test_each_call_is_a_new_session_long_enough_for_agentcore():
    a, b = session_id(agent_case()), session_id(agent_case())
    assert a != b and 33 <= len(a) <= 256 and a.startswith("skillhub-hub_1-101-1-")


def test_the_answer_comes_back_from_the_runtime():
    client, stub = stubbed()
    stub.add_response("invoke_agent_runtime", answer({"answer": "2 critical findings"}), expected())
    with stub:
        got = agent(client).run(agent_case())
    assert (got.text, got.error) == ("2 critical findings", None)
    stub.assert_no_pending_responses()


def test_throttling_is_retried_in_a_new_session_with_backoff():
    client, stub = stubbed()
    sessions = []

    def record(params, **kw):
        sessions.append(params["runtimeSessionId"])

    client.meta.events.register("provide-client-params.bedrock-agentcore.InvokeAgentRuntime", record)
    stub.add_client_error(
        "invoke_agent_runtime", "ThrottlingException", "Rate exceeded", 429, expected_params=expected()
    )
    stub.add_client_error("invoke_agent_runtime", "ServiceQuotaExceededException", "Too many sessions", 402,
                          expected_params=expected())  # fmt: skip
    stub.add_response("invoke_agent_runtime", answer({"answer": "ok"}), expected())
    sleeps = []
    with stub:
        got = agent(client, sleeps).run(agent_case())
    assert got.text == "ok" and len(sleeps) == 2 and len(set(sessions)) == 3


def test_an_agent_that_keeps_being_throttled_is_an_error_after_the_last_try():
    client, stub = stubbed()
    for _ in range(agentcore.MAX_TRIES):
        stub.add_client_error("invoke_agent_runtime", "ThrottlingException", "Rate exceeded", 429)
    with stub:
        got = agent(client).run(agent_case())
    assert got == AgentAnswer("", "ThrottlingException: Rate exceeded", got.seconds)


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (
            answer({"error": "the skills MCP refused the token"}),
            "the agent reported: the skills MCP refused the token",
        ),
        (
            answer(b"data: hi\n\n", "text/event-stream"),
            "the agent answered with text/event-stream; it must answer JSON",
        ),
        (answer(b"not json"), "the agent's answer is not valid JSON"),
        (answer([1, 2]), "the agent's answer is not a JSON object"),
    ],
)
def test_an_answer_that_is_not_the_contracts_is_an_error(response, error):
    client, stub = stubbed()
    stub.add_response("invoke_agent_runtime", response, expected())
    with stub:
        assert agent(client).run(agent_case()).error == error


def test_a_refused_call_is_an_error_that_never_shows_a_key():
    client, stub = stubbed()
    stub.add_client_error("invoke_agent_runtime", "AccessDeniedException",
                          "User with key test-key-id-0001 is not authorized to perform InvokeAgentRuntime", 403)  # fmt: skip
    with stub:
        got = agent(client).run(agent_case())
    assert got.error.startswith("AccessDeniedException: User with key *** is not authorized")
    assert "test-key-id-0001" not in got.error


def test_an_agentcore_profile_builds_its_agent_with_the_bedrock_keys(monkeypatch):
    cfg = make_cfg()
    cfg.models["sa"] = ModelProfile(route="bedrock", model_id="us.anthropic.claude-sonnet-5-5", region="us-east-1",
                                    kind="external_agent", harness="agentcore", agent={"runtime_arn": ARN},
                                    **{"class": "frontier"})  # fmt: skip
    monkeypatch.setattr(agentcore, "bedrock_keys", lambda cfg: KEYS)
    built = agent_for(cfg, "sa")
    assert isinstance(built, AgentCoreAgent) and built.runtime_arn == ARN
    assert (
        built.client.meta.region_name == "us-east-1"
        and built.concurrency == cfg.defaults.concurrency_per_endpoint
    )
