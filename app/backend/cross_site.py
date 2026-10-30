"""Refuse a request from another site that would change something.

The browser's session with the app is the Databricks Apps proxy's cookie, which the browser may send with a form that
another site posts to the app. So a request with an unsafe method to /api/ is refused, with 403 `cross_site_request`,
when the browser says another site sent it: `Sec-Fetch-Site` is `cross-site` or `same-site` (another app on the same
apps domain is the same site). Browsers send that header, and when it is there it alone decides, so the app's own
pages (`same-origin`) never depend on the host the proxy forwards. A request without it is refused when it has an
`Origin` whose host is not the app's: the proxy's `X-Forwarded-Host`, or `Host` for a local run.

A request with neither header passes: curl, scripts and other clients that send a bearer token do not send them.
"""

from __future__ import annotations

import urllib.parse

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from backend.authz import Reason

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
OTHER_SITES = frozenset({"cross-site", "same-site"})
DEFAULT_PORTS = {"http": 80, "https": 443}


def _address(netloc: str, scheme: str) -> tuple[str, int] | None:
    """(host, port) of `host[:port]`, with the scheme's default port. None when it cannot be read."""
    try:
        parts = urllib.parse.urlsplit(f"//{netloc.strip()}")
        port = parts.port
    except ValueError:
        return None
    if not parts.hostname:
        return None
    return parts.hostname, port or DEFAULT_PORTS[scheme]


def _same_host(origin: str, headers: Headers) -> bool:
    """Whether the Origin names the host the app is served on: the proxy's X-Forwarded-Host, or Host."""
    try:
        parts = urllib.parse.urlsplit(origin.strip())
    except ValueError:
        return False
    if parts.scheme not in DEFAULT_PORTS:
        return False  # `null` (a sandboxed frame, a file) or anything else that is not a web origin
    sender = _address(parts.netloc, parts.scheme)
    # The first proxy's X-Forwarded-Host is the host the browser asked for.
    forwarded = headers.get("x-forwarded-host", "").split(",")[0]
    served = [h for h in (forwarded, headers.get("host", "")) if h.strip()]
    return sender is not None and any(_address(h, parts.scheme) == sender for h in served)


def is_cross_site(method: str, path: str, headers: Headers) -> bool:
    """Whether a request to the API that would change something came from another site."""
    if method.upper() not in UNSAFE_METHODS or not (path == "/api" or path.startswith("/api/")):
        return False
    site = headers.get("sec-fetch-site", "").strip().lower()
    if site:
        return site in OTHER_SITES
    origin = headers.get("origin")
    return origin is not None and not _same_host(origin, headers)


class CrossSiteGuard:
    """ASGI middleware: answers a cross-site API request with 403 `cross_site_request` before any route runs."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and is_cross_site(scope["method"], scope["path"], Headers(scope=scope)):
            refused = JSONResponse(
                status_code=403, content={"detail": {"reason": Reason.CROSS_SITE_REQUEST.value}}
            )
            await refused(scope, receive, send)
            return
        await self.app(scope, receive, send)
