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
