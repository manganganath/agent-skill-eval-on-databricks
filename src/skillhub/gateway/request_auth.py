"""Per-request workspace authentication for the raw HTTP clients (serving endpoints and the skills MCP server).

Credentials are set in the HTTP transport, never in call arguments: `mlflow.openai.autolog()` records the
keyword arguments of every `create()` call (including `extra_headers`) in the trace, and department readers
can read eval traces. The headers are read again for every request, so a refreshed OAuth token is used by a
long-lived client.
"""

from __future__ import annotations

import typing
from collections.abc import Callable, Mapping

import anyio.to_thread
import httpx2

AUTHORIZATION = "Authorization"


class HeaderAuth(httpx2.Auth):
    """Sets the headers that `headers()` returns on each request.

    Any `Authorization` header already on the request (the openai SDK adds one from its placeholder
    `api_key`) is removed first, so only the provider's credential is sent."""

    def __init__(self, headers: Callable[[], Mapping[str, str]]):
        self._headers = headers

    @staticmethod
    def _apply(request: httpx2.Request, values: Mapping[str, str]) -> httpx2.Request:
        request.headers.pop(AUTHORIZATION, None)
        for name, value in values.items():
            request.headers[name] = value
        return request

    def auth_flow(self, request: httpx2.Request) -> typing.Generator[httpx2.Request, httpx2.Response, None]:
        yield self._apply(request, self._headers())

    async def async_auth_flow(
        self, request: httpx2.Request
    ) -> typing.AsyncGenerator[httpx2.Request, httpx2.Response]:
        # A token refresh can block on network I/O: keep it off the event loop.
        values = await anyio.to_thread.run_sync(self._headers)
        yield self._apply(request, values)
