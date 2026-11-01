"""Release webhooks (skillhub.registry.webhooks): the event, the URLs and keys from secret scopes, the signature, and
delivery that retries and never raises. No network: `post` and `sleep` are stand-ins."""

import base64
import hashlib
import hmac
import json
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
