"""The guard against cross-site requests: a request that would change something, sent by another site with the
browser's session, is refused before any route runs. Clients that send neither Sec-Fetch-Site nor Origin pass."""

import pytest
from app_fakes import FakeApi, FakeWorkspace
from backend.cross_site import is_cross_site
from conftest import make_cfg
from starlette.datastructures import Headers

APP_HOST = "skill-hub-123.aws.databricksapps.com"  # the public host, as the Apps proxy forwards it
CROSS_SITE = {"detail": {"reason": "cross_site_request"}}

# Every route that changes something, with a body that would get past validation.
CHANGES = [
    ("POST", "/api/pulls/5/merge", None),
    ("POST", "/api/drafts/draft_0000000000000000000000ab/submit", None),
    ("POST", "/api/drafts", {"dept": "common", "files": []}),
    ("DELETE", "/api/drafts/draft_0000000000000000000000ab", None),
    (
        "POST",
        "/api/eval-runs",
        {"skills": ["skill_hub.common.x"], "models": ["claude-sonnet"], "modes": ["trigger"]},
    ),
    ("POST", "/api/skills/skill_hub.common.x/switch", {"to_version": 2}),
    ("POST", "/api/try", {"fqn": "skill_hub.common.x", "prompt": "hi", "models": ["claude-sonnet"]}),
    ("POST", "/api/candidates/pr5_abc1234_a1/common/x/try", {"prompt": "hi", "models": ["claude-sonnet"]}),
]


def recording_client(make_client):
    """A client whose user and app clients record every call, so a test can tell that no route ran."""
    user_w = FakeWorkspace(api=FakeApi())
    app_w = FakeWorkspace(api=FakeApi())
    return make_client(user_w=user_w, app_w=app_w, cfg=make_cfg()), user_w, app_w


def send(client, method, path, body=None, headers=None):
    return client.request(method, path, json=body, headers=headers or {})


@pytest.mark.parametrize(("method", "path", "body"), CHANGES)
@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_a_change_sent_by_another_site_is_refused_before_any_route_runs(
    make_client, method, path, body, site
):
    client, user_w, app_w = recording_client(make_client)
    r = send(client, method, path, body, {"sec-fetch-site": site, "x-forwarded-access-token": "tok"})
    assert r.status_code == 403 and r.json() == CROSS_SITE
    assert user_w.api_client.calls == [] and app_w.api_client.calls == []


@pytest.mark.parametrize(("method", "path", "body"), CHANGES)
def test_a_change_with_the_origin_of_another_host_is_refused(make_client, method, path, body):
    client, user_w, _ = recording_client(make_client)
    headers = {
        "origin": "https://evil.example",
        "x-forwarded-host": APP_HOST,
        "x-forwarded-access-token": "tok",
    }
    r = send(client, method, path, body, headers)
    assert r.status_code == 403 and r.json() == CROSS_SITE
    assert user_w.api_client.calls == []


@pytest.mark.parametrize(
    "origin",
    [
        "null",  # a sandboxed frame or a local file
        f"https://{APP_HOST}.evil.example",  # the app's host as part of another one
        f"https://{APP_HOST}:8443",  # another port of the same host
        f"http://{APP_HOST}:443",  # the same port with another scheme
        f"ftp://{APP_HOST}",
        "https://",
        "https://[not-a-host",
    ],
)
def test_an_origin_that_is_not_the_apps_own_is_refused(make_client, origin):
    client, _, _ = recording_client(make_client)
    r = send(client, "POST", "/api/pulls/5/merge", headers={"origin": origin, "x-forwarded-host": APP_HOST})
    assert r.status_code == 403 and r.json() == CROSS_SITE


def passed_the_guard(r) -> bool:
    """The merge route ran: without a user token it answers no_user_token, not cross_site_request."""
    return r.status_code == 403 and r.json() == {"detail": {"reason": "no_user_token"}}


@pytest.mark.parametrize(
    "headers",
    [
        {},  # curl, a script, a bearer-token client
        {"authorization": "Bearer tok"},
        # The app's own page behind the Apps proxy: the public host is in X-Forwarded-Host, Host is the app's own.
        {"sec-fetch-site": "same-origin", "origin": f"https://{APP_HOST}", "x-forwarded-host": APP_HOST},
        {"origin": f"https://{APP_HOST}:443", "x-forwarded-host": APP_HOST},  # the default port, named
        {"origin": f"https://{APP_HOST.upper()}", "x-forwarded-host": APP_HOST},
        # Two proxies: the first one's host is the one the browser asked for.
        {"origin": f"https://{APP_HOST}", "x-forwarded-host": f"{APP_HOST}, 10.0.0.7:8000"},
        # A proxy that passes the public host on in Host, and a local run (TestClient's host is `testserver`).
        {"origin": f"https://{APP_HOST}", "host": APP_HOST},
        {"sec-fetch-site": "same-origin", "origin": "http://testserver"},
        {"sec-fetch-site": "none"},  # the user's own action, such as a bookmark
        # The browser's word decides: a proxy that forwards some other host does not lock out the app's own pages.
        {"sec-fetch-site": "same-origin", "origin": f"https://{APP_HOST}", "host": "127.0.0.1:8000"},
    ],
)
def test_the_apps_own_pages_and_clients_without_the_headers_pass(make_client, headers):
    r = make_client().post("/api/pulls/5/merge", headers=headers)
    assert passed_the_guard(r), r.text


def test_a_read_is_never_refused(make_client):
    r = make_client().get(
        "/api/health", headers={"sec-fetch-site": "cross-site", "origin": "https://evil.example"}
    )
    assert r.status_code == 200 and r.json() == {"ok": True}


@pytest.mark.parametrize(
    ("method", "path", "refused"),
    [
        ("POST", "/api/try", True),
        ("PUT", "/api/anything", True),
        ("PATCH", "/api", True),
        ("delete", "/api/drafts/x", True),
        ("GET", "/api/skills", False),
        ("HEAD", "/api/skills", False),
        ("OPTIONS", "/api/try", False),
        ("POST", "/apix", False),  # not the API
        ("POST", "/assets/app.js", False),
    ],
)
def test_only_changes_to_the_api_are_guarded(method, path, refused):
    headers = Headers({"sec-fetch-site": "cross-site"})
    assert is_cross_site(method, path, headers) is refused
