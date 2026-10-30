from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Annotated

from databricks.sdk import WorkspaceClient
from fastapi import Depends, Header, HTTPException

from backend.authz import Reason
from backend.data import UserSql
from skillhub.auth import user_workspace_client, workspace_client
from skillhub.config import HubConfig, load_config
from skillhub.sql import SqlClient

log = logging.getLogger("skillhub.app.deps")

TOKEN_HEADER = "x-forwarded-access-token"


def workspace_host() -> str:
    """The workspace URL with a scheme and no trailing slash. Databricks Apps sets DATABRICKS_HOST to the bare
    hostname, so every client and link the app builds takes its host from here, never from the raw config."""
    host = os.environ.get("DATABRICKS_HOST", "").strip().rstrip("/")
    if not host:
        raise RuntimeError("DATABRICKS_HOST is not set")
    return host if host.startswith("https://") else f"https://{host}"


def ci_eval_principal() -> str | None:
    """The application ID of the eval service principal, which runs the evals and their judges. Unset: None."""
    return os.environ.get("SKILLHUB_CI_EVAL_PRINCIPAL", "").strip() or None


def user_client(token: str) -> WorkspaceClient:
    # skillhub.auth sets auth_type="pat", so the SDK never mixes the forwarded token with the app's OAuth env vars.
    return user_workspace_client(workspace_host(), token)


@lru_cache(maxsize=1)
def app_client() -> WorkspaceClient:
    # Default env auth: the app service principal (DATABRICKS_CLIENT_ID / _SECRET set by Databricks Apps).
    return workspace_client()


def get_user_token(
    x_forwarded_access_token: str | None = Header(default=None, alias=TOKEN_HEADER),
) -> str:
    if not x_forwarded_access_token or not x_forwarded_access_token.strip():
        raise HTTPException(status_code=403, detail={"reason": Reason.NO_USER_TOKEN.value})
    return x_forwarded_access_token.strip()


def get_user_client(token: str = Depends(get_user_token)) -> WorkspaceClient:
    try:
        return user_client(token)
    except Exception as exc:  # unset host or unusable client: refuse, never a 500
        log.warning("cannot build user client: %s", type(exc).__name__)
        raise problem(403, Reason.AUTHZ_CHECK_FAILED) from None


def get_app_client() -> WorkspaceClient:
    return app_client()


@lru_cache(maxsize=1)
def _load(path: str) -> HubConfig:
    return load_config(path)


def get_cfg() -> HubConfig:
    return _load(os.environ.get("SKILLHUB_CONFIG", "config/hub.yaml"))


def problem(status: int, reason: Reason) -> HTTPException:
    return HTTPException(status_code=status, detail={"reason": reason.value})


def get_sql(
    w: Annotated[WorkspaceClient, Depends(get_user_client)], cfg: Annotated[HubConfig, Depends(get_cfg)]
) -> UserSql:
    # The user's client, so UC permissions apply per user. The timeout is shorter than SqlClient's 900 s default,
    # because this runs inside a web request.
    return UserSql(SqlClient(w, cfg.workspace.sql_warehouse_id, timeout_s=60))
