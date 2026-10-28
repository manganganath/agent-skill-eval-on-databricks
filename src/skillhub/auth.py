from __future__ import annotations

from functools import lru_cache

from databricks.sdk import WorkspaceClient


@lru_cache(maxsize=1)
def _default_client() -> WorkspaceClient:
    return WorkspaceClient()


def workspace_client() -> WorkspaceClient:
    return _default_client()


def auth_headers() -> dict[str, str]:
    """Headers that authenticate a request to the workspace, for raw HTTP clients (MCP, serving)."""
    return dict(_default_client().config.authenticate())


def user_workspace_client(host: str, token: str) -> WorkspaceClient:
    """A client acting as the signed-in user. Explicit auth_type stops env OAuth settings from conflicting."""
    return WorkspaceClient(host=host, token=token, auth_type="pat")
