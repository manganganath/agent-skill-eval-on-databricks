from app_fakes import FakeApi, FakeWorkspace
from conftest import make_cfg


def test_me_without_token_is_403(make_client):
    r = make_client(cfg=make_cfg()).get("/api/me")
    assert r.status_code == 403 and r.json() == {"detail": {"reason": "no_user_token"}}


def test_me_lists_groups_and_non_reviewer(make_client):
    w = FakeWorkspace(
        api=FakeApi(
            {
                "/api/2.0/preview/scim/v2/Me": {
                    "userName": "ana@example.com",
                    "groups": [{"display": "dept-equity-research"}],
                }
            }
        )
    )
    body = make_client(user_w=w, cfg=make_cfg()).get("/api/me").json()
    assert body["groups"] == ["dept-equity-research"] and body["is_reviewer"] is False


def _effective(principal, schemas):
    eff = "/api/2.1/unity-catalog/effective-permissions/"
    holders = [
        {"principal": principal, "privileges": [{"privilege": "READ_VOLUME"}, {"privilege": "USE_SCHEMA"}]}
    ]
    responses = {f"{eff}schema/skill_hub.{s}": {"privilege_assignments": holders} for s in schemas}
    responses[f"{eff}catalog/skill_hub"] = {
        "privilege_assignments": [{"principal": principal, "privileges": [{"privilege": "USE_CATALOG"}]}]
    }
    return responses


def _user():
    return FakeWorkspace(
        api=FakeApi({"/api/2.0/preview/scim/v2/Me": {"userName": "ana@example.com", "groups": []}})
    )


def test_readable_departments_lists_only_checked_schemas(make_client):
    # No answer for equity_research or external (a KeyError in the fake): they fail closed.
    app = FakeWorkspace(api=FakeApi(_effective("ana@example.com", ["common"])))
    r = make_client(user_w=_user(), app_w=app, cfg=make_cfg()).get("/api/me/departments")
    assert r.status_code == 200 and r.json() == {"departments": ["common"]}


def test_readable_departments_asks_the_catalog_once(make_client):
    schemas = ["common", "dept_equity_research", "external"]
    app = FakeWorkspace(api=FakeApi(_effective("ana@example.com", schemas)))
    r = make_client(user_w=_user(), app_w=app, cfg=make_cfg()).get("/api/me/departments")
    assert r.json()["departments"] == ["common", "equity_research", "external"]
    catalog_calls = [c for c in app.api_client.calls if c[1].endswith("/catalog/skill_hub")]
    assert len(catalog_calls) == 1


def test_readable_departments_without_token_is_403(make_client):
    assert make_client(cfg=make_cfg()).get("/api/me/departments").status_code == 403
