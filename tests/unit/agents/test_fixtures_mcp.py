import asyncio
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from skillhub.agent.fixtures import FixtureStore
from skillhub.agents.fixtures_mcp import spec_json
from skillhub.evalspec import EvalCase

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"
CASE = EvalCase(id=101, kind="effectiveness", prompt="p", should_trigger=True,
                fixtures=["northwind/model.xlsx", "northwind/press_release_q3.md"])  # fmt: skip


async def _session(spec: Path, calls: list[tuple[str, dict]]):
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "skillhub.agents.fixtures_mcp", str(spec)]
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = (await session.list_tools()).tools
        answers = [(await session.call_tool(name, args)).content[0].text for name, args in calls]
        return tools, answers


def test_a_cli_agent_reads_the_cases_files_as_the_agent_loop_does(tmp_path):
    spec = tmp_path / "fixtures.json"
    spec.write_text(spec_json(FIXTURES, CASE))
    calls = [
        ("read_workbook", {"path": "northwind/model.xlsx"}),
        ("read_file", {"path": "northwind/press_release_q3.md"}),
        ("web_search", {"query": "northwind quarter results"}),
        ("read_file", {"path": "northwind/transcript_q3.md"}),  # not a file of this case
    ]
    tools, answers = asyncio.run(_session(spec, calls))
    store = FixtureStore(FIXTURES)
    assert {t.name: t.description for t in tools} == {t.name: t.description for t in store.tools_for(CASE)}
    assert answers == [store.call(name, args, CASE) for name, args in calls]
    assert answers[3].startswith("Error: not a fixture of this case")
