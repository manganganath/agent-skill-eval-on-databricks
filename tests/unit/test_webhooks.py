"""Release webhooks (skillhub.registry.webhooks): the event, the URLs and keys from secret scopes, the signature, and
delivery that retries and never raises. No network: `post` and `sleep` are stand-ins."""

import base64
import hashlib
import hmac
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from skillhub.config import Notifications, Webhook
from skillhub.registry import webhooks
from skillhub.registry.journal import new_row
from tests.fakes import make_cfg

SLACK_PATH = "/services/T000/B000/" + "x" * 24  # a token in the path, as Slack and Teams URLs carry


def cfg_with(*hooks):
    cfg = make_cfg()
    cfg.notifications = Notifications(webhooks=[Webhook(**h) for h in hooks])
    return cfg


def published():
    return new_row("common", "audit-xls", "published", actor="ci", job_run_id="7", version=3,
                   versioned_fqn="skill_hub.common_versions.audit-xls-v3", content_hash="sha256:" + "a" * 64,
                   git_sha="b" * 40, eval_run_id="run-1")  # fmt: skip


class Poster:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, url, *, content, headers, timeout, follow_redirects):
        self.calls.append(SimpleNamespace(url=url, body=content, headers=headers, timeout=timeout))
        answer = self.answers.pop(0) if self.answers else 200
        if isinstance(answer, Exception):
            raise answer
        return SimpleNamespace(status_code=answer)


def secrets(values):
    return lambda scope, key: values.get(f"{scope}/{key}")


def test_the_event_names_the_skill_the_versions_and_the_evidence():
    event = webhooks.event(make_cfg(), published())
    assert event["type"] == "skill.published" and event["skill_fqn"] == "skill_hub.common.audit-xls"
    assert {k: event[k] for k in ("version", "versioned_fqn", "git_sha", "eval_run_id", "actor", "job_run_id")} == {
        "version": 3, "versioned_fqn": "skill_hub.common_versions.audit-xls-v3", "git_sha": "b" * 40,
        "eval_run_id": "run-1", "actor": "ci", "job_run_id": "7",
    }  # fmt: skip
    assert event["ts"].endswith("+00:00") and len(event["id"]) == 36


def test_each_subscribed_webhook_gets_one_json_post_and_others_none():
    cfg = cfg_with(
        {"url": "https://hooks.example.com/a"}, {"url": "https://other.example.com/b", "events": ["switched"]}
    )
    post = Poster()
    assert webhooks.deliver(cfg, published(), post=post, sleep=lambda s: None) == []
    (call,) = post.calls
    assert call.url == "https://hooks.example.com/a" and call.timeout == webhooks.TIMEOUT_S
    assert json.loads(call.body)["action"] == "published"
    assert (
        call.headers["X-SkillHub-Event"] == "skill.published"
        and "X-SkillHub-Signature-256" not in call.headers
    )


def test_a_signed_webhook_carries_the_hmac_of_its_body():
    cfg = cfg_with(
        {"url": "https://hooks.example.com/a", "signing_secret": {"scope": "skillhub-webhooks", "key": "k"}}
    )
    post = Poster()
    webhooks.deliver(cfg, published(), get_secret=secrets({"skillhub-webhooks/k": "s3cret"}), post=post)
    (call,) = post.calls
    want = "sha256=" + hmac.new(b"s3cret", call.body, hashlib.sha256).hexdigest()
    assert call.headers["X-SkillHub-Signature-256"] == want


def test_a_url_from_a_secret_scope_is_used_and_never_printed():
    cfg = cfg_with({"url_secret": {"scope": "skillhub-webhooks", "key": "slack"}})
    url = "https://hooks.slack.example.com" + SLACK_PATH
    post = Poster(500, 500, 500)
    out = webhooks.deliver(cfg, published(), get_secret=secrets({"skillhub-webhooks/slack": url}), post=post,
                           sleep=lambda s: None)  # fmt: skip
    assert [c.url for c in post.calls] == [url] * 3
    assert out == ["webhook 1 (hooks.slack.example.com) not delivered: HTTP 500 after 3 attempt(s)"]
    assert SLACK_PATH not in out[0]


@pytest.mark.parametrize(
    ("values", "says"),
    [
        ({}, "not sent: its URL is not readable at skillhub-webhooks/slack"),
        (
            {"skillhub-webhooks/slack": "http://plain.example.com/x"},
            "not sent: its URL in the secret scope is not an https:// URL",
        ),
    ],
)
def test_a_url_that_cannot_be_read_or_is_not_https_is_not_sent(values, says):
    cfg = cfg_with({"url_secret": {"scope": "skillhub-webhooks", "key": "slack"}})
    post = Poster()
    out = webhooks.deliver(cfg, published(), get_secret=secrets(values), post=post)
    assert post.calls == [] and out == [f"webhook 1 {says}"]


def test_a_missing_signing_secret_stops_the_webhook_rather_than_sending_it_unsigned():
    cfg = cfg_with(
        {"url": "https://hooks.example.com/a", "signing_secret": {"scope": "skillhub-webhooks", "key": "k"}}
    )
    post = Poster()
    out = webhooks.deliver(cfg, published(), get_secret=secrets({}), post=post)
    assert post.calls == [] and out == [
        "webhook 1 (hooks.example.com) not sent: its signing secret is not readable at skillhub-webhooks/k"
    ]


def test_a_server_error_is_retried_with_backoff_and_a_success_ends_it():
    waits = []
    post = Poster(503, 429, 204)
    assert webhooks.deliver(cfg_with({"url": "https://hooks.example.com/a"}), published(), post=post,
                            sleep=waits.append) == []  # fmt: skip
    assert len(post.calls) == 3 and waits == list(webhooks.BACKOFF_S)


def test_a_client_error_is_not_retried_and_a_transport_error_is():
    post = Poster(404)
    out = webhooks.deliver(cfg_with({"url": "https://hooks.example.com/a"}), published(), post=post)
    assert len(post.calls) == 1 and out == [
        "webhook 1 (hooks.example.com) not delivered: HTTP 404 after 1 attempt(s)"
    ]
    boom = httpx.ConnectError("refused https://hooks.example.com/a?token=t")
    post = Poster(boom, boom, boom)
    out = webhooks.deliver(cfg_with({"url": "https://hooks.example.com/a"}), published(), post=post,
                           sleep=lambda s: None)  # fmt: skip
    assert out == ["webhook 1 (hooks.example.com) not delivered: ConnectError after 3 attempt(s)"]


def test_only_release_actions_are_sent():
    post = Poster()
    refused = new_row("common", "audit-xls", "publish_refused", actor="ci")
    assert webhooks.deliver(cfg_with({"url": "https://hooks.example.com/a"}), refused, post=post) == []
    assert post.calls == []


def test_secrets_are_read_with_the_workspace_client_and_a_failure_reads_as_none():
    class Secrets:
        def get_secret(self, scope, key):
            if key == "gone":
                raise RuntimeError("not found")
            return SimpleNamespace(value=base64.b64encode(b"https://hooks.example.com/a\n").decode())

    get = webhooks.workspace_secrets(SimpleNamespace(secrets=Secrets()))
    assert get("s", "url") == "https://hooks.example.com/a" and get("s", "gone") is None


@pytest.mark.parametrize(
    ("hook", "says"),
    [
        ({}, "exactly one of url and url_secret"),
        (
            {"url": "https://a.example.com", "url_secret": {"scope": "s", "key": "k"}},
            "exactly one of url and url_secret",
        ),
        ({"url": "http://a.example.com/x"}, "must be an https:// URL"),
        ({"url_secret": {"scope": "bad scope!", "key": "k"}}, "is not a secret scope name"),
        ({"url": "https://a.example.com", "events": []}, "at least 1 item"),
        ({"url": "https://a.example.com", "events": ["deleted"]}, "Input should be"),
    ],
)
def test_a_bad_webhook_setting_is_refused(hook, says):
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match=says):
        Webhook(**hook)


# --- the review's findings ------------------------------------------------------------------------------------------


def _history():
    from datetime import UTC, datetime, timedelta

    from skillhub.registry.journal import JournalRow

    t = datetime(2026, 11, 2, tzinfo=UTC)

    def row(action, minutes, **kw):
        base = dict(dept="common", skill="audit-xls", action=action, version=None, from_version=None, to_version=None,
                    versioned_fqn=None, content_hash=None, git_sha=None, eval_run_id=None, actor="ci", job_run_id=None)  # fmt: skip
        return JournalRow(**{**base, **kw, "ts": t + timedelta(minutes=minutes)})

    v4 = row("published", 1, version=4, versioned_fqn="skill_hub.common_versions.audit-xls-v4",
             content_hash="sha256:" + "4" * 64, git_sha="g4", eval_run_id="run-4")  # fmt: skip
    switched = row("switched", 2, from_version=3, to_version=4)
    retired = row("retired", 3)
    return [v4, switched, retired]


def test_switch_and_retire_events_carry_the_version_its_hash_commit_and_eval_run():
    history = _history()
    switched = webhooks.event(make_cfg(), webhooks.complete(history[1], history))
    assert (switched["version"], switched["from_version"], switched["to_version"]) == (4, 3, 4)
    assert (switched["content_hash"], switched["git_sha"], switched["eval_run_id"]) == (
        "sha256:" + "4" * 64,
        "g4",
        "run-4",
    )
    assert switched["text"] == "Skill common/audit-xls: live version switched from 3 to 4 by ci"
    retired = webhooks.event(make_cfg(), webhooks.complete(history[2], history))
    assert (retired["from_version"], retired["version"], retired["eval_run_id"]) == (4, 4, "run-4")
    assert retired["text"] == "Skill common/audit-xls: retired by ci (it was version 4)"


def test_a_published_event_has_a_summary_line_for_slack_and_teams():
    assert (
        webhooks.event(make_cfg(), published())["text"] == "Skill common/audit-xls: version 3 published by ci"
    )


def test_retry_after_is_honoured_within_the_deadline():
    waits = []

    class Limited(Poster):
        def __call__(self, url, **kw):
            super().__call__(url, **kw)
            return SimpleNamespace(
                status_code=429 if len(self.calls) == 1 else 200, headers={"retry-after": "4"}
            )

    assert webhooks.deliver(cfg_with({"url": "https://hooks.example.com/a"}), published(), post=Limited(),
                            sleep=waits.append) == []  # fmt: skip
    assert waits == [4.0]


def test_a_receiver_that_never_answers_is_given_up_after_the_deadline(monkeypatch):
    import threading
    import time

    monkeypatch.setattr(webhooks, "DEADLINE_S", 0.3)
    stuck = threading.Event()

    def hang(url, **kw):
        stuck.wait(5)
        return SimpleNamespace(status_code=200)

    t0 = time.monotonic()
    out = webhooks.deliver(cfg_with({"url": "https://hooks.example.com/a"}), published(), post=hang)
    stuck.set()
    assert time.monotonic() - t0 < 2 and out == ["webhook 1 not delivered within 0 s"]


def test_an_unexpected_error_stays_with_its_own_webhook():
    def post(url, **kw):
        if "first" in url:
            raise ValueError("bad https://first.example.com/x?token=t")
        return SimpleNamespace(status_code=200)

    cfg = cfg_with({"url": "https://first.example.com/x"}, {"url": "https://second.example.com/y"})
    assert webhooks.deliver(cfg, published(), post=post) == [
        "webhook 1 (first.example.com) not delivered: ValueError"
    ]


def test_a_secret_url_with_a_space_or_a_newline_is_not_sent():
    cfg = cfg_with({"url_secret": {"scope": "skillhub-webhooks", "key": "slack"}})
    out = webhooks.deliver(cfg, published(), get_secret=secrets({"skillhub-webhooks/slack": "https://a.example.com/x\ny"}),
                           post=Poster())  # fmt: skip
    assert out == ["webhook 1 not sent: its URL in the secret scope is not an https:// URL"]


def test_http_post_reads_only_the_status_and_headers():
    import respx

    with respx.mock:
        route = respx.post("https://hooks.example.com/a").respond(
            202, headers={"Retry-After": "1"}, content=b"x" * 10
        )
        got = webhooks.http_post(
            "https://hooks.example.com/a", content=b"{}", headers={}, timeout=5, follow_redirects=False
        )
    assert route.called and got.status_code == 202 and got.headers["retry-after"] == "1"


def test_a_misspelt_webhook_key_is_refused():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Webhook(url="https://a.example.com/x", **{"signing-secret": {"scope": "s", "key": "k"}})


def test_a_config_error_never_quotes_the_value_it_read(tmp_path):
    from skillhub.config import ConfigError, load_config

    token = "T0/B0/" + "S" * 24
    text = (Path(__file__).resolve().parents[2] / "config" / "hub.yaml").read_text(encoding="utf-8")
    text += f"\nnotifications:\n  webhooks:\n    - url: http://hooks.slack.example.com/services/{token}\n"
    path = tmp_path / "hub.yaml"
    path.write_text(text, encoding="utf-8")
    import os
    from unittest.mock import patch

    env = {
        "DATABRICKS_HOST": "https://example.invalid",
        "SQL_WAREHOUSE_ID": "w",
        "SKILLHUB_CATALOG": "skill_hub",
    }
    with patch.dict(os.environ, env), pytest.raises(ConfigError) as err:
        load_config(path)
    assert "must be an https:// URL" in str(err.value) and "S" * 10 not in str(err.value)
