"""A sample agent on Amazon Bedrock AgentCore that the Skill Hub can evaluate.

It is a Strands agent served by the AgentCore runtime SDK. For each case it:
1. checks the payload (`contract.Case`), and that every skills MCP URL is on its own workspace;
2. loads the skills from the Unity Gateway skills MCP, one connection per URL, as its own Databricks principal,
   and gives the model the case's fixture tools when the case has files;
3. calls the Bedrock model that the case names, for at most `max_turns` turns;
4. puts the case's trace attributes on its spans and sends them, as OTLP/HTTP, to the trace table the case names;
5. answers {"answer": "<final text>"}, or {"error": "<why>"}.

It is the reference implementation of the Skill Hub's external agent contract: an agent in any framework that does
the same can be evaluated. Settings, from the runtime's environment:
- DATABRICKS_HOST: the workspace URL, such as https://<workspace>.cloud.databricks.com
- DATABRICKS_CLIENT_ID: the application ID of the agent's own service principal
- SKILLHUB_AGENT_SECRET_ARN: the AWS Secrets Manager secret that holds that principal's OAuth secret
- AWS_REGION: the region of the Bedrock models (the runtime sets it)
"""

from __future__ import annotations

import logging
import os
from contextlib import ExitStack
from typing import Any

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from contract import (
    AUTHORING_TOOLS,
    UC_TABLE_ATTR,
    Case,
    FixtureTools,
    M2MToken,
    check_skills_urls,
    group_by_table,
    plain_names,
    safe_tool_name,
)
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from strands import Agent, tool
from strands.models import BedrockModel
from strands.telemetry import StrandsTelemetry
from strands.tools.mcp import MCPClient
from strands.tools.mcp.mcp_agent_tool import MCPAgentTool

log = logging.getLogger("skill-agent")
HOST = os.environ["DATABRICKS_HOST"].rstrip("/")
REGION = os.environ.get("AWS_REGION", "us-east-1")
OTLP_URL = f"{HOST}/api/2.0/otel/v1/traces"


def _client_secret() -> str:
    secrets = boto3.client("secretsmanager", region_name=REGION)
    return secrets.get_secret_value(SecretId=os.environ["SKILLHUB_AGENT_SECRET_ARN"])["SecretString"]


TOKEN = M2MToken(HOST, os.environ["DATABRICKS_CLIENT_ID"], _client_secret)


class DatabricksSpans(SpanExporter):
    """Sends each case's spans to the trace table that its payload named, with a fresh token."""

    def export(self, spans: Any) -> SpanExportResult:
        result = SpanExportResult.SUCCESS
        for table, group in group_by_table(spans).items():
            headers = {"Authorization": f"Bearer {TOKEN()}", "X-Databricks-UC-Table-Name": table}
            exporter = OTLPSpanExporter(endpoint=OTLP_URL, headers=headers)
            if exporter.export(group) is not SpanExportResult.SUCCESS:
                result = SpanExportResult.FAILURE
            exporter.shutdown()
        return result

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


telemetry = StrandsTelemetry()  # the GenAI conventions: invoke_agent, chat and execute_tool spans
telemetry.tracer_provider.add_span_processor(BatchSpanProcessor(DatabricksSpans()))
app = BedrockAgentCoreApp()


def fixture_tools(fx: FixtureTools) -> list:
    if not fx.files:
        return []
    text = fx.descriptions()

    @tool(name="web_search", description=text["web_search"])
    def web_search(query: str) -> str:
        return fx.web_search(query)

    @tool(name="read_file", description=text["read_file"])
    def read_file(path: str) -> str:
        return fx.read_file(path)

    @tool(name="read_workbook", description=text["read_workbook"])
    def read_workbook(path: str) -> str:
        return fx.read_workbook(path)

    return [web_search, read_file, read_workbook]


def skill_tools(clients: list[MCPClient], plain: bool) -> list:
    """Each connection's tools, named as Bedrock accepts. A tool that every connection lists (load_skill,
    list_skills, get_skill_files) is offered once, from the first."""
    out, seen = [], set()
    for client in clients:
        for t in client.list_tools_sync():
            name = safe_tool_name(t.tool_name, plain=plain)
            if name not in seen:
                seen.add(name)
                out.append(MCPAgentTool(t.mcp_tool, client, name_override=name))
    return out


@app.entrypoint
def invoke(payload: dict) -> dict:
    try:
        case = Case.from_payload(payload)
        check_skills_urls(case.skills_mcp, HOST)
    except ValueError as exc:
        return {"error": f"the payload is not a Skill Hub case: {exc}"}
    try:
        with ExitStack() as stack:
            clients = [
                stack.enter_context(
                    MCPClient(
                        url=url,
                        headers={"Authorization": f"Bearer {TOKEN()}"},
                        tool_filters={"rejected": list(AUTHORING_TOOLS)},
                    )
                )
                for url in case.skills_mcp
            ]
            tools = fixture_tools(FixtureTools(case.files, case.search_index)) + skill_tools(
                clients, plain_names(case.model)
            )
            agent = Agent(
                model=BedrockModel(model_id=case.model, region_name=REGION),
                tools=tools,
                trace_attributes={**case.trace, UC_TABLE_ATTR: case.uc_table},
                callback_handler=None,
            )
            result = agent(case.prompt, limits={"turns": case.max_turns})
            return {"answer": str(result)}
    except Exception as exc:  # the case is an error for the Skill Hub, not a fail
        log.exception("case failed")
        return {"error": f"{type(exc).__name__}: {exc}"[:500]}
    finally:
        telemetry.tracer_provider.force_flush()  # before the session can be frozen


if __name__ == "__main__":
    app.run()
