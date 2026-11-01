"""The Skill Hub's eval contract, for an agent on Amazon Bedrock AgentCore. Standard library only, so that it is
tested without the agent's own dependencies.

- `Case.from_payload` checks the payload that the eval Job sends for one case.
- `safe_tool_name` gives a skill tool a name that Bedrock accepts (letters, digits, `_` and `-`, at most 64): the
  Skill Hub reads the agent's tool spans by these names.
- `FixtureTools` answers web_search, read_file and read_workbook from the payload's files, as the Skill Hub's own
  agent loop answers them from the case's fixtures.
- `check_skills_urls` refuses a skills MCP URL on any other host than the agent's workspace, so the agent's token
  is sent nowhere else.
- `M2MToken` gets the OAuth token of the agent's own Databricks principal, and a new one before it expires.
- `group_by_table` sorts finished spans by the trace table their case named.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

MODES = ("trigger", "with_skill", "without_skill")
MAX_TOOL_NAME = 64
NO_RESULTS = "No results."
MAX_RESULTS = 3
AUTHORING_TOOLS = ("create_skill", "update_skill", "delete_skill")  # never offered to the agent
UC_TABLE_ATTR = "skillhub.uc_table"  # this agent's own span attribute: where the case's spans go
_TABLE = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+\.[a-z0-9_]+")
_ATTR_VALUE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_PLAIN = re.compile(r"(?:^|[./])amazon\.")  # Amazon Nova models refuse a hyphen in a tool name


@dataclass(frozen=True)
class Case:
    prompt: str
    mode: str
    skills_mcp: tuple[str, ...]
    model: str
    max_turns: int
    trace: dict[str, str]
    uc_table: str
    files: dict[str, str] = field(default_factory=dict)
    search_index: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Any) -> Case:
        if not isinstance(payload, dict):
            raise ValueError("the payload is not a JSON object")
        prompt, mode, model = payload.get("prompt"), payload.get("mode"), payload.get("model")
        if not isinstance(prompt, str) or not prompt:
            raise ValueError("prompt: give the case's prompt")
        if mode not in MODES:
            raise ValueError(f"mode: use one of {', '.join(MODES)}")
        if not isinstance(model, str) or not model:
            raise ValueError("model: give the Bedrock model ID to call")
        urls = payload.get("skills_mcp") or []
        if mode == "without_skill" and urls:
            raise ValueError("skills_mcp: a without_skill case must not load skills")
        if not isinstance(urls, list) or not all(isinstance(u, str) for u in urls):
            raise ValueError("skills_mcp: give a list of URLs, or null")
        turns = payload.get("max_turns")
        if isinstance(turns, bool) or not isinstance(turns, int) or turns < 1:
            raise ValueError("max_turns: give a whole number of at least 1")
        trace = payload.get("trace")
        if not isinstance(trace, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and _ATTR_VALUE.fullmatch(v) for k, v in trace.items()
        ):
            raise ValueError("trace: give the span attributes as text values")
        table = (payload.get("otel") or {}).get("uc_table")
        if not isinstance(table, str) or not _TABLE.fullmatch(table):
            raise ValueError("otel.uc_table: give <catalog>.<schema>.<table>")
        files = payload.get("files") or {}
        index = payload.get("search_index") or {}
        if not isinstance(files, dict) or not all(isinstance(v, str) for v in files.values()):
            raise ValueError("files: give each file's text by its path")
        if not isinstance(index, dict) or not all(isinstance(v, list) for v in index.values()):
            raise ValueError("search_index: give each keyword's paths")
        return cls(prompt, mode, tuple(urls), model, turns, dict(trace), table, dict(files), dict(index))


def plain_names(model: str) -> bool:
    return _PLAIN.search(model) is not None


def safe_tool_name(name: str, *, plain: bool = False) -> str:
    """The tool name this agent gives the model: dots become underscores (and hyphens too for the models that refuse
    them), and a name over 64 characters becomes its first 55, an underscore and 8 digits of the SHA-1 of the name
    with underscores. The Skill Hub maps each of these forms back to the skill."""
    flat = name.replace(".", "_")
    short = flat.replace("-", "_") if plain else flat
    if len(short) <= MAX_TOOL_NAME:
        return short
    return f"{short[: MAX_TOOL_NAME - 9]}_{hashlib.sha1(flat.encode()).hexdigest()[:8]}"


class FixtureTools:
    """The case's files as the Skill Hub's fixture tools show them: a workbook as its cell-by-cell dump."""

    def __init__(self, files: dict[str, str], search_index: dict[str, list[str]]):
        self.files = files
        self.index = {k.lower(): v for k, v in search_index.items()}

    def descriptions(self) -> dict[str, str]:
        listed = ", ".join(sorted(self.files))
        return {
            "web_search": "Search the fixture document set by keyword.",
            "read_file": f"Read one of this case's files as text. Files: {listed}.",
            "read_workbook": f"Read one of this case's Excel workbooks cell by cell (formulas and values). Files: {listed}.",
        }

    def _declared(self, rel: str) -> str | None:
        if rel in self.files:
            return rel
        want = rel.strip().replace("\\", "/")
        while want.startswith("./"):
            want = want[2:]
        if want.lower().startswith("fixtures/"):
            want = want[len("fixtures/") :]
        by_lower = {f.lower(): f for f in self.files}
        if want.lower() in by_lower:
            return by_lower[want.lower()]
        if "/" not in want:
            hits = [f for f in self.files if f.rsplit("/", 1)[-1].lower() == want.lower()]
            if len(hits) == 1:
                return hits[0]
        return None

    def read_file(self, path: str) -> str:
        declared = self._declared(path)
        if declared is None:
            return f"Error: not a fixture of this case: {path}; this case's files: {', '.join(sorted(self.files))}"
        return self.files[declared]

    read_workbook = read_file

    def web_search(self, query: str) -> str:
        q = query.lower()
        matched: dict[str, set[str]] = {}
        for keyword, docs in self.index.items():
            if keyword in q:
                for doc in docs:
                    if doc in self.files:
                        matched.setdefault(doc, set()).add(keyword)
        ranked = sorted(matched, key=lambda d: (-len(matched[d]), d))[:MAX_RESULTS]
        if not ranked:
            return NO_RESULTS
        return "\n\n".join(f"=== {d} ===\n{self.files[d]}" for d in ranked)


def check_skills_urls(urls: Iterable[str], host: str) -> None:
    prefix = f"{host.rstrip('/')}/ai-gateway/skills/"
    bad = [u for u in urls if not u.startswith(prefix)]
    if bad:
        raise ValueError(f"skills_mcp: only the skills MCP of {host} is used, not {', '.join(bad)}")


def mcp_refusal(url: str, token: str, *, opener: Callable[..., Any] = urllib.request.urlopen) -> str | None:
    """Why a skills MCP refused the agent, in the server's own words: its HTTP status and message, such as a missing
    entitlement. The MCP client reports only that it could not start. None when the server answers, or when it
    cannot be reached at all, which the client's own error already says."""
    hello = {"jsonrpc": "2.0", "id": 0, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "skillhub-skill-agent", "version": "1"}}}  # fmt: skip
    request = urllib.request.Request(
        url, data=json.dumps(hello).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
    )  # fmt: skip
    try:
        with opener(request, timeout=15):
            return None
    except urllib.error.HTTPError as exc:
        text = exc.read(4000).decode(errors="replace")
        code = exc.code
    except (urllib.error.URLError, OSError):
        return None
    try:
        message = json.loads(text).get("message") or text
    except (ValueError, AttributeError):
        message = text
    return f"HTTP {code}: {' '.join(str(message).replace(token, '***').split())[:300]}"


class M2MToken:
    """The OAuth token of a Databricks service principal (client credentials), cached until a minute before it
    expires. `secret` is called only when a token is needed, and its value never appears in an error."""

    MARGIN_S = 60

    def __init__(
        self,
        host: str,
        client_id: str,
        secret: Callable[[], str],
        *,
        opener: Callable[..., Any] = urllib.request.urlopen,
        clock: Callable[[], float] = time.time,
    ):
        self._url = f"{host.rstrip('/')}/oidc/v1/token"
        self._client_id, self._secret, self._open, self._clock = client_id, secret, opener, clock
        self._token: str | None = None
        self._expires = 0.0
        self._lock = threading.Lock()

    def __call__(self) -> str:
        with self._lock:
            if self._token is None or self._clock() >= self._expires - self.MARGIN_S:
                self._token, self._expires = self._fetch()
            return self._token

    def _fetch(self) -> tuple[str, float]:
        basic = base64.b64encode(f"{self._client_id}:{self._secret()}".encode()).decode()
        body = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": "all-apis"}).encode()
        request = urllib.request.Request(
            self._url, data=body, method="POST",
            headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
        )  # fmt: skip
        try:
            with self._open(request, timeout=30) as response:
                data = json.loads(response.read())
        except Exception as exc:  # its text could quote the request
            raise RuntimeError(
                f"the agent's Databricks token could not be fetched ({type(exc).__name__})"
            ) from None
        return str(data["access_token"]), self._clock() + float(data.get("expires_in") or 3600)


def group_by_table(spans: Iterable[Any], attr: str = UC_TABLE_ATTR) -> dict[str, list[Any]]:
    """The spans of each trace table. A span without the attribute, or with something else than a table name, is
    left out: it is not part of a case."""
    out: dict[str, list[Any]] = {}
    for span in spans:
        table = (getattr(span, "attributes", None) or {}).get(attr)
        if isinstance(table, str) and _TABLE.fullmatch(table):
            out.setdefault(table, []).append(span)
    return out
