import base64
import logging
from types import SimpleNamespace

import pytest
from databricks.sdk.errors import InternalError, PermissionDenied, ResourceDoesNotExist

from skillhub.gateway import aws
from skillhub.gateway.aws import AwsCredentialsError, AwsKeys
from tests.fakes import make_cfg

KEY_ID, SECRET, TOKEN = "test-key-id-0001", "test-secret-value-0001", "test-session-token-0001"
VALUES = (KEY_ID, SECRET, TOKEN)


@pytest.fixture(autouse=True)
def _fresh_cache():
    aws.clear_cache()
    yield
    aws.clear_cache()


def scope(**keys):
    """A secrets API with these keys in the skillhub-aws scope, base64-encoded as the API returns them."""
    calls = []

    def get_secret(scope_name, key):
        calls.append((scope_name, key))
        if scope_name != "skillhub-aws" or key not in keys:
            raise ResourceDoesNotExist(f"Secret does not exist with scope: {scope_name} and key: {key}")
        if isinstance(keys[key], Exception):
            raise keys[key]
        return SimpleNamespace(key=key, value=base64.b64encode(keys[key].encode()).decode())

    return SimpleNamespace(secrets=SimpleNamespace(get_secret=get_secret)), calls


def test_the_keys_are_read_and_the_session_token_is_optional():
    w, _ = scope(**{"aws-access-key-id": KEY_ID, "aws-secret-access-key": SECRET})
    keys = aws.read_keys(aws.workspace_secret_getter(w), "skillhub-aws")
    assert (keys.access_key_id, keys.secret_access_key, keys.session_token) == (KEY_ID, SECRET, None)
    w, _ = scope(**{"aws-access-key-id": KEY_ID, "aws-secret-access-key": SECRET, "aws-session-token": TOKEN})
    assert aws.read_keys(aws.workspace_secret_getter(w), "skillhub-aws").session_token == TOKEN


@pytest.mark.parametrize(
    ("keys", "scope_name", "says"),
    [
        ({"aws-access-key-id": KEY_ID}, "skillhub-aws", "has no aws-secret-access-key key"),
        ({}, "skillhub-aws", "has no aws-access-key-id or aws-secret-access-key key"),
        (
            {"aws-access-key-id": KEY_ID, "aws-secret-access-key": SECRET},
            "other-scope",
            "or the scope does not exist",
        ),
    ],
)
def test_a_missing_scope_or_key_is_named_in_plain_words(keys, scope_name, says):
    w, _ = scope(**keys)
    with pytest.raises(AwsCredentialsError, match=says) as e:
        aws.read_keys(aws.workspace_secret_getter(w), scope_name)
    assert "operations guide" in str(e.value)


def test_an_identity_without_read_on_the_scope_is_told_what_to_grant():
    w, _ = scope(**{"aws-access-key-id": PermissionDenied("User does not have READ permission on scope")})
    with pytest.raises(AwsCredentialsError, match="give the eval service principal READ"):
        aws.read_keys(aws.workspace_secret_getter(w), "skillhub-aws")


def test_no_key_value_reaches_an_error_a_log_or_a_repr(caplog):
    caplog.set_level(logging.DEBUG)
    keys = AwsKeys(KEY_ID, SECRET, TOKEN)
    assert not any(v in repr(keys) or v in str(keys) for v in VALUES)
    failing = {
        "aws-access-key-id": KEY_ID,
        "aws-secret-access-key": InternalError(f"backend echoed {SECRET}"),
    }
    w, _ = scope(**failing)
    with pytest.raises(AwsCredentialsError) as e:
        aws.read_keys(aws.workspace_secret_getter(w), "skillhub-aws")
    assert not any(v in str(e.value) for v in VALUES)
    assert e.value.__cause__ is None and e.value.__suppress_context__
    assert not any(v in caplog.text for v in VALUES)
    assert (
        aws.redact(f"signature for {KEY_ID} and {TOKEN} failed", keys) == "signature for *** and *** failed"
    )


def test_a_value_that_is_not_base64_text_is_an_error_that_names_the_key():
    w = SimpleNamespace(
        secrets=SimpleNamespace(get_secret=lambda s, k: SimpleNamespace(value="%%%not-base64"))
    )
    with pytest.raises(
        AwsCredentialsError, match="'aws-access-key-id' in the secret scope 'skillhub-aws' is not text"
    ):
        aws.read_keys(aws.workspace_secret_getter(w), "skillhub-aws")


def test_the_scope_comes_from_hub_yaml_and_is_read_once_per_process():
    w, calls = scope(**{"aws-access-key-id": KEY_ID, "aws-secret-access-key": SECRET})
    cfg = make_cfg()
    getter = aws.workspace_secret_getter(w)
    first = aws.bedrock_keys(cfg, getter=getter)
    assert aws.bedrock_keys(cfg, getter=getter) is first
    assert {s for s, _ in calls} == {"skillhub-aws"}
    assert len(calls) == 3  # the two keys and the optional session token, once


def test_a_session_gets_the_keys_and_the_region():
    seen = {}
    aws.session_for(AwsKeys(KEY_ID, SECRET), "us-east-1", factory=lambda **kw: seen.update(kw) or "session")
    assert seen == {
        "aws_access_key_id": KEY_ID,
        "aws_secret_access_key": SECRET,
        "aws_session_token": None,
        "region_name": "us-east-1",
    }
