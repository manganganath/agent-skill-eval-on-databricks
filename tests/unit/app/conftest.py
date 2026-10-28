import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
# app/ holds the `backend` package. Append (not insert) so the installed skillhub wins over any app/skillhub build copy.
APP = str(REPO / "app")
if APP not in sys.path:
    sys.path.append(APP)

from skillhub.config import HubConfig  # noqa: E402


def make_cfg() -> HubConfig:
    return HubConfig.model_validate(
        {
            "workspace": {"host": "https://example.test", "sql_warehouse_id": "wh-test"},
            "catalog": "skill_hub",
            "departments": {
                "common": {"readers": ["skillhub-users"]},
                "equity_research": {"readers": ["dept-equity-research"]},
                "external": {"readers": ["skillhub-reviewers"], "quarantine": True},
            },
            "roles": {"authors": "skillhub-authors", "reviewers": "skillhub-reviewers", "ci": "skillhub-ci"},
            "models": {
                "claude-sonnet": {"route": "serving", "endpoint": "ep-claude", "class": "frontier"},
                "gemini-flash": {"route": "serving", "endpoint": "ep-gemini", "class": "fast"},
            },
            "judges": ["claude-sonnet"],
            "judges_pr": ["claude-sonnet"],
            "generator": "claude-sonnet",
            "defaults": {"models": ["claude-sonnet"]},
            "import": {"source": "github.com/example/skills@abc123", "map": {"plugins/x": "common"}},
        }
    )


@pytest.fixture
def cfg() -> HubConfig:
    return make_cfg()


@pytest.fixture
def make_client():
    """Build a TestClient with dependencies overridden. Unset overrides keep the real dependency."""
    from backend import deps
    from backend.main import create_app
    from fastapi.testclient import TestClient

    def _make(*, user_w=None, app_w=None, cfg=None, sql=None, static_dir=None):
        app = create_app(static_dir or REPO / "does-not-exist")
        if user_w is not None:
            app.dependency_overrides[deps.get_user_client] = lambda: user_w
        if app_w is not None:
            app.dependency_overrides[deps.get_app_client] = lambda: app_w
        if cfg is not None:
            app.dependency_overrides[deps.get_cfg] = lambda: cfg
        if sql is not None:
            app.dependency_overrides[deps.get_sql] = lambda: sql
        return TestClient(app)

    return _make
