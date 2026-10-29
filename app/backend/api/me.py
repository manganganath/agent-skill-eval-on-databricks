from __future__ import annotations

from typing import Annotated

from databricks.sdk import WorkspaceClient
from fastapi import APIRouter, Depends

from backend import authz
from backend.deps import get_cfg, get_user_client
from backend.schemas import MeOut
from skillhub.config import HubConfig

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
