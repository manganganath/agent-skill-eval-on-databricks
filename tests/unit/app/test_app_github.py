"""The GitHub client: configuration, the token, typed errors and what is kept or sent. The client is exercised with a
replaced urlopen, so no request leaves the test."""

import base64
import io
import json
import logging
import urllib.error
from types import SimpleNamespace

import pytest
from backend import github
from databricks.sdk.errors import PermissionDenied

TOKEN = "ghp_TOKENVALUE123"
REPO = "acme/skills"
SECRET = "skillhub/github-token"


@pytest.fixture(autouse=True)
def _fresh_answers():
    github._cache.clear()
    yield
    github._cache.clear()


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv(github.REPO_ENV, REPO)
    monkeypatch.setenv(github.SECRET_ENV, SECRET)


def app_with_secret(value=None, raises=None):
    """The app's client, with a secret that holds `value` (base64 is applied here) or raises."""

    def get_secret(scope, key):
        if raises is not None:
            raise raises
        assert (scope, key) == ("skillhub", "github-token")
        return SimpleNamespace(value=base64.b64encode((value or TOKEN).encode()).decode())

    return SimpleNamespace(secrets=SimpleNamespace(get_secret=get_secret))


class Response:
    """What urlopen returns: a context manager with the status and the body."""

    def __init__(self, body: object, status: int = 200):
        self.status = status
        self._raw = json.dumps(body).encode() if body is not None else b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._raw


def http_error(code: int, body: bytes = b'{"message": "ghp_TOKENVALUE123 leaked"}'):
    return urllib.error.HTTPError("https://api.github.test", code, "msg", {}, io.BytesIO(body))


def recording_opener(answer):
    """An urlopen stand-in: returns `answer` as a 200 body, or raises it when it is an exception."""
    seen = []

    def opener(req, timeout=None):
        seen.append(req)
        if isinstance(answer, BaseException):
            raise answer
        return Response(answer)

    opener.seen = seen
    return opener


def test_connect_needs_a_repository_and_a_secret(monkeypatch):
    monkeypatch.delenv(github.REPO_ENV, raising=False)
    monkeypatch.delenv(github.SECRET_ENV, raising=False)
    with pytest.raises(github.GitHubNotConfigured):
        github.connect(app_with_secret())
    monkeypatch.setenv(github.REPO_ENV, "not a repository")
    monkeypatch.setenv(github.SECRET_ENV, SECRET)
    with pytest.raises(github.GitHubNotConfigured):
        github.connect(app_with_secret())
    monkeypatch.setenv(github.REPO_ENV, REPO)
    monkeypatch.setenv(github.SECRET_ENV, "no-slash-here")
    with pytest.raises(github.GitHubNotConfigured):
        github.connect(app_with_secret())


def test_the_token_is_read_from_the_secret_as_the_app_and_sent_as_a_bearer_token(configured, monkeypatch):
    opener = recording_opener({"ok": True})
    client = github.GitHubClient(REPO, TOKEN, opener=opener)
    client.get("/repos/acme/skills/pulls/5")
    req = opener.seen[0]
    assert req.get_header("Authorization") == f"Bearer {TOKEN}"
    assert req.get_header("X-github-api-version") == github.API_VERSION
    assert req.full_url == "https://api.github.com/repos/acme/skills/pulls/5"
    # connect itself reads the secret, through the app's client
    assert github.connect(app_with_secret())._token == TOKEN


@pytest.mark.parametrize(
    ("code", "kind"),
    [
        (404, github.GitHubNotFound),
        (401, github.GitHubDenied),
        (403, github.GitHubDenied),
        (405, github.GitHubRejected),
        (409, github.GitHubRejected),
        (422, github.GitHubRejected),
        (429, github.GitHubUnavailable),
        (500, github.GitHubUnavailable),
        (503, github.GitHubUnavailable),
    ],
)
def test_an_http_error_maps_to_its_typed_exception_with_the_status_only(code, kind):
    client = github.GitHubClient(REPO, TOKEN, opener=recording_opener(http_error(code)))
    with pytest.raises(kind) as info:
        client.get("/repos/acme/skills/pulls/5")
    assert info.value.status == code
    assert TOKEN not in str(info.value)
    assert "leaked" not in str(info.value)  # GitHub's message is never kept


def test_no_answer_is_unavailable_and_a_bad_body_too():
    unreachable = github.GitHubClient(REPO, TOKEN, opener=recording_opener(urllib.error.URLError("no route")))
    with pytest.raises(github.GitHubUnavailable) as info:
        unreachable.get("/repos/acme/skills/pulls/5")
    assert info.value.status == 0

    class Garbage(Response):
        def read(self):
            return b"<html>"

    def opener(req, timeout=None):
        return Garbage(None)

    with pytest.raises(github.GitHubUnavailable):
        github.GitHubClient(REPO, TOKEN, opener=opener).get("/repos/acme/skills/pulls/5")


def test_an_unreadable_secret_is_not_configured_and_its_message_is_not_logged(configured, caplog):
    with caplog.at_level(logging.WARNING, logger="skillhub.app.github"):
        with pytest.raises(github.GitHubNotConfigured):
            github.connect(app_with_secret(raises=PermissionDenied(f"denied for {TOKEN}")))
    assert "PermissionDenied" in caplog.text
    assert TOKEN not in caplog.text


def test_an_empty_secret_is_not_configured(configured):
    with pytest.raises(github.GitHubNotConfigured):
        github.connect(app_with_secret(value="   "))


def test_answers_are_reused_for_a_few_seconds_unless_asked_fresh(configured):
    opener = recording_opener({"number": 5})
    cached = github.GitHubClient(REPO, TOKEN, opener=opener)
    cached.get("/repos/acme/skills/pulls/5")
    cached.get("/repos/acme/skills/pulls/5")
    assert len(opener.seen) == 1
    fresh = github.GitHubClient(REPO, TOKEN, cached=False, opener=opener)
    fresh.get("/repos/acme/skills/pulls/5")
    fresh.get("/repos/acme/skills/pulls/5")
    assert len(opener.seen) == 3


def test_a_failed_answer_is_not_kept(configured):
    client = github.GitHubClient(REPO, TOKEN, opener=recording_opener(http_error(503)))
    for _ in range(2):
        with pytest.raises(github.GitHubUnavailable):
            client.get("/repos/acme/skills/pulls/5")
    assert not github._cache


def test_check_states_keep_the_newest_run_of_each_name():
    answer = {
        "check_runs": [
            {"id": 1, "name": "skills-pr", "status": "completed", "conclusion": "failure"},
            {"id": 2, "name": "skills-pr", "status": "completed", "conclusion": "success"},
            {"id": 3, "name": "platform", "status": "in_progress", "conclusion": None},
            {"id": 4, "name": "docs", "status": "completed", "conclusion": "skipped"},
        ]
    }
    client = github.GitHubClient(REPO, TOKEN, opener=recording_opener(answer))
    assert client.check_states("c7ecd3e") == {"skills-pr": "passed", "platform": "pending", "docs": "failed"}


def test_publish_run_reports_a_job_waiting_for_approval():
    runs = {
        "workflow_runs": [
            {
                "id": 9,
                "status": "in_progress",
                "conclusion": None,
                "html_url": "https://run",
                "created_at": "2026-10-29T12:01:00Z",
            }
        ]
    }
    jobs = {"jobs": [{"name": "publish and switch (production)", "status": "waiting"}]}

    def opener(req, timeout=None):
        return Response(jobs if "/jobs" in req.full_url else runs)

    run = github.GitHubClient(REPO, TOKEN, opener=opener).publish_run("M5", "skills-publish.yml")
    assert run == github.Run(url="https://run", status="in_progress", conclusion=None, waiting=True)


def test_publish_run_is_none_when_the_merge_has_no_run():
    client = github.GitHubClient(REPO, TOKEN, opener=recording_opener({"workflow_runs": []}))
    assert client.publish_run("M5", "skills-publish.yml") is None
