import asyncio
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from mcp.types import CallToolResult, TextContent, Tool

from skillhub.gateway import skills_mcp
from skillhub.gateway.request_auth import HeaderAuth
from skillhub.gateway.skills_mcp import McpTool, SkillsMcpClient, build_url, filter_tools


def test_url_repeats_schema_parameter_once_per_schema():
    url = build_url("https://ws.example.test", ["skill_cat.dept_x", "skill_cat.common"])
    assert url == "https://ws.example.test/ai-gateway/skills/?schema=skill_cat.dept_x&schema=skill_cat.common"


def test_url_strips_trailing_slash_on_host():
    assert (
        build_url("https://ws.example.test/", ["a.b"])
        == "https://ws.example.test/ai-gateway/skills/?schema=a.b"
    )


def test_filter_drops_authoring_tools_only():
    tools = [
        McpTool(n, "d", {})
        for n in [
            "skill_cat.d.x",
            "load_skill",
            "get_skill_files",
            "list_skills",
            "create_skill",
            "update_skill",
            "delete_skill",
        ]
    ]
    kept = [t.name for t in filter_tools(tools)]
    assert kept == ["skill_cat.d.x", "load_skill", "get_skill_files", "list_skills"]


class FakeSession:
    def __init__(self, tools, is_error=False):
        self._tools = tools
        self._is_error = is_error
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def initialize(self):
        return SimpleNamespace(server_info=SimpleNamespace(name="skills-mcp", version="0.1.0"))

    async def list_tools(self):
        return SimpleNamespace(tools=[Tool(name=n, description=d, input_schema=s) for n, d, s in self._tools])

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return CallToolResult(content=[TextContent(type="text", text="SKILL BODY")], is_error=self._is_error)


class FakeCM:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *exc):
        return False


def _patch(monkeypatch, fake):
    opened = {}

    def fake_stream(url, http_client):
        opened["url"] = url
        return FakeCM((None, None))  # the SDK yields two streams

    def fake_http(**kwargs):
        opened.update(kwargs)
        return FakeCM("http")

    monkeypatch.setattr(skills_mcp, "streamable_http_client", fake_stream)
    monkeypatch.setattr(skills_mcp, "create_mcp_http_client", fake_http)
    monkeypatch.setattr(skills_mcp, "ClientSession", lambda r, w: fake)
    return opened


def _client():
    return SkillsMcpClient("https://ws.example.test", ["cat.d"], lambda: {"Authorization": "Bearer t"})


def test_list_tools_and_call_tool_with_stubbed_session(monkeypatch):
    fake = FakeSession(
        [
            ("skill_cat.d.x", "earnings", {"type": "object"}),
            ("create_skill", "cli", {}),
            ("update_skill", "cli", {}),
            ("delete_skill", "cli", {}),
        ]
    )
    opened = _patch(monkeypatch, fake)

    async def run():
        async with _client() as c:
            return await c.list_tools(), await c.call_tool("skill_cat.d.x", {})

    tools, text = asyncio.run(run())
    assert [t.name for t in tools] == ["skill_cat.d.x"]
    assert tools[0].input_schema == {"type": "object"} and tools[0].description == "earnings"
    assert text == "SKILL BODY"
    assert opened["url"] == "https://ws.example.test/ai-gateway/skills/?schema=cat.d"
    assert "headers" not in opened  # no credential fixed at open time
    assert isinstance(opened["auth"], HeaderAuth)
    assert fake.calls == [("skill_cat.d.x", {})]


def test_error_result_raises(monkeypatch):
    _patch(monkeypatch, FakeSession([], is_error=True))

    async def run():
        async with _client() as c:
            await c.call_tool("load_skill", {"full_name": "a.b.c"})

    with pytest.raises(RuntimeError, match="load_skill failed"):
        asyncio.run(run())


def test_authoring_tool_call_refused(monkeypatch):
    fake = FakeSession([])
    _patch(monkeypatch, fake)

    async def run():
        async with _client() as c:
            await c.call_tool("delete_skill", {})

    with pytest.raises(ValueError):
        asyncio.run(run())
    assert fake.calls == []


def test_url_encodes_hostile_schema_value():
    url = build_url("https://ws.example.test", ["a b&schema=evil", "cat.ok"])
    query = urlsplit(url).query
    assert parse_qs(query)["schema"] == ["a b&schema=evil", "cat.ok"]
    assert query.count("schema=") == 2 and query.count("&") == 1


class RecordingCM:
    def __init__(self, log, name, value=None, enter_error=None):
        self.log, self.name, self.value, self.enter_error = log, name, value, enter_error

    async def __aenter__(self):
        if self.enter_error:
            raise self.enter_error
        self.log.append(f"enter {self.name}")
        return self.value

    async def __aexit__(self, *exc):
        self.log.append(f"exit {self.name}")
        return False


def _patch_recording(monkeypatch, session, stream_error=None):
    log = []
    monkeypatch.setattr(skills_mcp, "create_mcp_http_client", lambda **kw: RecordingCM(log, "http", "h"))
    monkeypatch.setattr(
        skills_mcp,
        "streamable_http_client",
        lambda url, http_client: RecordingCM(log, "stream", (None, None), stream_error),
    )
    monkeypatch.setattr(skills_mcp, "ClientSession", lambda r, w: session)
    return log


def test_enter_closes_stack_when_stream_setup_fails(monkeypatch):
    log = _patch_recording(monkeypatch, FakeSession([]), stream_error=ConnectionError("boom"))

    async def run():
        async with _client():
            pass

    with pytest.raises(ConnectionError):
        asyncio.run(run())
    assert log == ["enter http", "exit http"]


def test_enter_closes_stack_when_initialize_fails(monkeypatch):
    class BadInit(FakeSession):
        async def initialize(self):
            raise RuntimeError("init failed")

    log = _patch_recording(monkeypatch, BadInit([]))

    async def run():
        async with _client():
            pass

    with pytest.raises(RuntimeError, match="init failed"):
        asyncio.run(run())
    assert log == ["enter http", "enter stream", "exit stream", "exit http"]


def test_exit_runs_when_body_raises(monkeypatch):
    log = _patch_recording(monkeypatch, FakeSession([]))
    client = _client()

    async def run():
        async with client:
            raise ValueError("body")

    with pytest.raises(ValueError, match="body"):
        asyncio.run(run())
    assert log == ["enter http", "enter stream", "exit stream", "exit http"]
    assert client._session is None


def test_use_outside_context_raises_runtime_error():
    with pytest.raises(RuntimeError, match="outside its async context"):
        asyncio.run(_client().list_tools())
    with pytest.raises(RuntimeError, match="outside its async context"):
        asyncio.run(_client().call_tool("load_skill", {}))


def test_session_reads_the_token_again_for_every_request(monkeypatch):
    """A child run that outlives its OAuth token keeps working, because each request asks again."""
    seen = []
    tokens = iter(["first", "second"])

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers.get("authorization"))
        return httpx2.Response(200, json={})

    captured = {}
    real = skills_mcp.create_mcp_http_client

    def http_with_mock_transport(**kwargs):
        client = real(**kwargs)
        client._transport = httpx2.MockTransport(handler)
        captured["client"] = client
        return client

    monkeypatch.setattr(skills_mcp, "create_mcp_http_client", http_with_mock_transport)
    monkeypatch.setattr(skills_mcp, "streamable_http_client", lambda url, http_client: FakeCM((None, None)))
    monkeypatch.setattr(skills_mcp, "ClientSession", lambda r, w: FakeSession([]))
    client = SkillsMcpClient(
        "https://ws.example.test", ["cat.d"], lambda: {"Authorization": f"Bearer {next(tokens)}"}
    )

    async def run():
        async with client:
            http = captured["client"]
            await http.get("https://ws.example.test/ai-gateway/skills/")
            await http.get("https://ws.example.test/ai-gateway/skills/")

    asyncio.run(run())
    assert seen == ["Bearer first", "Bearer second"]
