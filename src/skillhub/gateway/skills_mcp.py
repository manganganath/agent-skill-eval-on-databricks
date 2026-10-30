from __future__ import annotations

from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from urllib.parse import quote

from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from skillhub.gateway.request_auth import HeaderAuth

AUTHORING_TOOLS = frozenset({"create_skill", "update_skill", "delete_skill"})
# The gateway offers at most this many per-skill tools (skill_<catalog>.<schema>.<name>) per connection, counting
# the connection's schemas in URL order, and leaves out the rest without an error. Keep each schema at or below it.
MAX_SKILL_TOOLS = 20


class SkillToolError(RuntimeError):
    """The server flagged a tool result as an error (MCP is_error). Other failures are not this type."""


@dataclass
class McpTool:
    name: str
    description: str
    input_schema: dict


def build_url(host: str, schemas: list[str]) -> str:
    """https://<host>/ai-gateway/skills/?schema=a.b&schema=c.d. The trailing slash is required."""
    query = "&".join(f"schema={quote(s, safe='.')}" for s in schemas)
    return f"{host.rstrip('/')}/ai-gateway/skills/?{query}"


def is_authoring_tool(name: str) -> bool:
    return name in AUTHORING_TOOLS


def filter_tools(tools: list[McpTool]) -> list[McpTool]:
    return [t for t in tools if not is_authoring_tool(t.name)]


class SkillsMcpClient:
    def __init__(self, host: str, schemas: list[str], headers: Callable[[], dict]):
        self._url = build_url(host, schemas)
        self._headers = headers
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> SkillsMcpClient:
        self._stack = AsyncExitStack()
        try:
            # Credentials are set per request, so a session that outlives its OAuth token keeps working.
            http = await self._stack.enter_async_context(
                create_mcp_http_client(auth=HeaderAuth(self._headers))
            )
            read, write = await self._stack.enter_async_context(
                streamable_http_client(self._url, http_client=http)
            )
            self._session = await self._stack.enter_async_context(ClientSession(read, write))
            await self._session.initialize()
        except BaseException:
            await self._stack.aclose()
            self._stack = None
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._session = None

    def _require_session(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError("SkillsMcpClient used outside its async context")
        return self._session

    async def list_tools(self) -> list[McpTool]:
        session = self._require_session()
        res = await session.list_tools()
        tools = [McpTool(t.name, t.description or "", t.input_schema or {}) for t in res.tools]
        return filter_tools(tools)

    async def call_tool(self, name: str, arguments: dict) -> str:
        """Text of the tool result. Server errors raise (MCPError) or, if flagged on the result, SkillToolError."""
        session = self._require_session()
        if is_authoring_tool(name):
            raise ValueError(f"authoring tool not allowed: {name}")
        res = await session.call_tool(name, arguments)
        text = "".join(getattr(c, "text", "") or "" for c in res.content)
        if res.is_error:
            raise SkillToolError(f"{name} failed: {text}")
        return text
