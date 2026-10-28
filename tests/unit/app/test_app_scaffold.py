import pytest
from backend import deps
from backend.authz import Reason
from fastapi import HTTPException


@pytest.fixture
def static_dir(tmp_path):
    (tmp_path / "index.html").write_text("<html>skillhub-index</html>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log(1)")
    return tmp_path


def test_health_is_public(make_client, static_dir):
    c = make_client(static_dir=static_dir)
    assert c.get("/api/health").json() == {"ok": True}


def test_root_and_deep_links_serve_index(make_client, static_dir):
    c = make_client(static_dir=static_dir)
    assert "skillhub-index" in c.get("/").text
    assert "skillhub-index" in c.get("/skills/main.common.x").text


def test_static_asset_served(make_client, static_dir):
    c = make_client(static_dir=static_dir)
    assert c.get("/assets/app.js").status_code == 200


def test_unknown_api_path_is_404_not_index(make_client, static_dir):
    c = make_client(static_dir=static_dir)
    r = c.get("/api/nope")
    assert r.status_code == 404
    assert "skillhub-index" not in r.text


def test_missing_user_token_is_403_with_reason():
    with pytest.raises(HTTPException) as exc:
        deps.get_user_token(None)
    assert exc.value.status_code == 403
    assert exc.value.detail == {"reason": Reason.NO_USER_TOKEN.value}


def test_blank_user_token_is_403():
    with pytest.raises(HTTPException) as exc:
        deps.get_user_token("   ")
    assert exc.value.status_code == 403


def test_user_client_is_pat_with_forwarded_token(monkeypatch):
    seen = {}

    class FakeWorkspaceClient:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setenv("DATABRICKS_HOST", "example.test.cloud")
    monkeypatch.setattr("skillhub.auth.WorkspaceClient", FakeWorkspaceClient)
    deps.user_client("user-token-abc")
    assert seen == {"host": "https://example.test.cloud", "token": "user-token-abc", "auth_type": "pat"}


@pytest.mark.parametrize(
    "raw",
    [
        "example.test.cloud",
        "https://example.test.cloud",
        "https://example.test.cloud/",
        " example.test.cloud/ ",
    ],
)
def test_workspace_host_has_a_scheme_and_no_trailing_slash(monkeypatch, raw):
    monkeypatch.setenv("DATABRICKS_HOST", raw)
    assert deps.workspace_host() == "https://example.test.cloud"
