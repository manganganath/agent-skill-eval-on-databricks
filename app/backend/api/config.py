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
    d = cfg.defaults
    try:
        host = workspace_host()
    except RuntimeError:
        raise problem(403, Reason.AUTHZ_CHECK_FAILED) from None
    return {
        "workspace_url": host,
        "models": {n: p.class_ for n, p in cfg.models.items()},
        "departments": list(cfg.departments),
        # Group names for the Access page. Names only; membership is checked per user.
        "roles": {"authors": cfg.roles.authors, "reviewers": cfg.roles.reviewers},
        "department_access": {
            d: {"readers": list(v.readers), "quarantine": v.quarantine} for d, v in cfg.departments.items()
        },
        # The verdict rules, so the UI can explain each verdict with the numbers in force.
        "verdict_rules": {
            "fail_rate": d.fail_rate,
            "warn_rate": d.warn_rate,
            "max_error_rate": d.max_error_rate,
            "min_reviewed_cases": d.min_reviewed_cases,
            "pass_score": d.pass_score,
            "gating_modes": list(d.gating_modes),
        },
    }
