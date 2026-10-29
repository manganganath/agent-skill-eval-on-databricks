from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated

from databricks.sdk import WorkspaceClient
from fastapi import APIRouter, Depends

from backend import authz
from backend.deps import get_app_client, get_cfg, get_user_client
from backend.schemas import MeOut, ReadableOut
from skillhub.config import HubConfig
from skillhub.naming import live_schema

log = logging.getLogger("skillhub.app.me")
router = APIRouter(tags=["me"])


@router.get("/me", response_model=MeOut)
def me(
    user_w: Annotated[WorkspaceClient, Depends(get_user_client)], cfg: Annotated[HubConfig, Depends(get_cfg)]
) -> MeOut:
    profile = authz.fetch_me(user_w)
    return MeOut(
        user=profile.user,
        groups=list(profile.groups),
        is_reviewer=cfg.roles.reviewers in profile.groups,
        is_author=cfg.roles.authors in profile.groups,
    )


@router.get("/me/departments", response_model=ReadableOut)
def readable_departments(
    user_w: Annotated[WorkspaceClient, Depends(get_user_client)],
    app_w: Annotated[WorkspaceClient, Depends(get_app_client)],
    cfg: Annotated[HubConfig, Depends(get_cfg)],
) -> ReadableOut:
    """The departments whose live skills the user can read, by the same per-schema check the catalog falls back
    on. One identity lookup and one permissions answer per schema (the catalog's is shared). Fails closed: a
    department that cannot be checked is not listed."""
    profile = authz.fetch_me(user_w)
    memo = authz.RequestMemo()

    def readable(dept: str) -> bool:
        try:
            return authz.effective_read_schema(
                app_w, profile.user, live_schema(cfg, dept), profile.groups, memo
            )
        except Exception as exc:
            log.warning("access check failed for department %s: %s", dept, type(exc).__name__)
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        flags = list(pool.map(readable, cfg.departments))
    return ReadableOut(departments=[d for d, ok in zip(cfg.departments, flags, strict=True) if ok])
