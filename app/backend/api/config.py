from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from backend.authz import Reason
from backend.deps import get_cfg, get_user_token, problem, workspace_host
from skillhub.config import HubConfig

router = APIRouter(tags=["config"])


@router.get("/config", dependencies=[Depends(get_user_token)])
def app_config(cfg: Annotated[HubConfig, Depends(get_cfg)]) -> dict:
    # No secrets here: profile classes, department keys and the workspace URL used for links.
    try:
        host = workspace_host()
    except RuntimeError:
        raise problem(403, Reason.AUTHZ_CHECK_FAILED) from None
    return {
        "workspace_url": host,
        "models": {n: p.class_ for n, p in cfg.models.items()},
        "departments": list(cfg.departments),
    }
