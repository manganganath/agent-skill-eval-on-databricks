"""A case's fixture files as an MCP server on stdio, for a CLI agent: `python -m skillhub.agents.fixtures_mcp <spec>`.

It offers the built-in agent loop's fixture tools (web_search, read_file and read_workbook) with the same descriptions
and the same answers (`skillhub.agent.fixtures.FixtureStore`), over this case's files only. So an external agent reads
a case's files exactly as the built-in agent loop does, and it needs no file or shell tool of its own. `<spec>` is a
JSON file that holds the fixture root and the evals.json case.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from skillhub.agent.fixtures import FixtureStore
from skillhub.evalspec import EvalCase

SERVER = "fixtures"


def spec_json(root: Path | str, case: EvalCase) -> str:
    return json.dumps({"root": str(root), "case": case.model_dump(mode="json")})


def build_server(store: FixtureStore, case: EvalCase) -> MCPServer:
    descriptions = {t.name: t.description for t in store.tools_for(case)}

    def web_search(query: str) -> str:
        return store.call("web_search", {"query": query}, case)

    def read_file(path: str) -> str:
        return store.call("read_file", {"path": path}, case)

    def read_workbook(path: str) -> str:
        return store.call("read_workbook", {"path": path}, case)

    server = MCPServer(SERVER)
    for fn in (web_search, read_file, read_workbook):
        server.add_tool(fn, name=fn.__name__, description=descriptions[fn.__name__], structured_output=False)
    return server


def main(argv: list[str] | None = None) -> None:
    [path] = argv if argv is not None else sys.argv[1:]
    spec = json.loads(Path(path).read_text(encoding="utf-8"))
    build_server(FixtureStore(spec["root"]), EvalCase.model_validate(spec["case"])).run("stdio")


if __name__ == "__main__":
    main()
