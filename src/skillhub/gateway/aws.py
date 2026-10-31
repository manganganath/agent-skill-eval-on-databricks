"""AWS credentials for Amazon Bedrock, read from a Databricks secret scope.

The keys of an IAM user live in the scope that hub.yaml names in `external.bedrock.secret_scope` (default
`skillhub-aws`): `aws-access-key-id`, `aws-secret-access-key` and, for temporary keys, `aws-session-token`. Only
skillhub-ci-eval may read the scope, so only the eval and probe Jobs can call Bedrock. The Skill Hub app never does.

No value ever reaches a log, an error, a trace or a run parameter: the errors name the scope and the key only, the
keys print as `AwsKeys(...)`, and `redact` removes the values from any text that leaves this package.
"""

from __future__ import annotations

import base64
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from databricks.sdk.errors import NotFound, PermissionDenied

from skillhub.config import ConfigError, HubConfig

log = logging.getLogger(__name__)

KEY_ID = "aws-access-key-id"
SECRET_KEY = "aws-secret-access-key"
SESSION_TOKEN = "aws-session-token"
GUIDE = "see Amazon Bedrock in the operations guide"

SecretGetter = Callable[
    [str, str], str | None
]  # (scope, key) -> the value, or None when there is no such key


class AwsCredentialsError(ConfigError):
    """The AWS keys for Bedrock cannot be read from the secret scope. The message names the scope and the key,
    never a value. A deployment setting, so a ConfigError: the runner records the profile as one it cannot serve."""


@dataclass(frozen=True)
class AwsKeys:
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    session_token: str | None = field(default=None, repr=False)

    def values(self) -> tuple[str, ...]:
        return tuple(v for v in (self.access_key_id, self.secret_access_key, self.session_token) if v)


def workspace_secret_getter(w: Any) -> SecretGetter:
    """Read secrets with a WorkspaceClient as the identity it runs as. The API returns each value base64-encoded."""

    def get(scope: str, key: str) -> str | None:
        try:
            raw = w.secrets.get_secret(scope, key).value or ""
        except NotFound:  # no such key, or no such scope
            return None
        except PermissionDenied:
            raise AwsCredentialsError(
                f"this identity may not read the secret scope {scope!r}; give the eval service principal READ "
                f"on it ({GUIDE})"
            ) from None
        except Exception as exc:  # the message of the failure is not kept: it could quote the request
            raise AwsCredentialsError(
                f"the secret scope {scope!r} could not be read ({type(exc).__name__})"
            ) from None
        try:
            return base64.b64decode(raw, validate=True).decode("utf-8").strip() or None
        except ValueError:  # binascii.Error and UnicodeDecodeError are both ValueErrors
            raise AwsCredentialsError(f"the key {key!r} in the secret scope {scope!r} is not text") from None

    return get


def read_keys(get: SecretGetter, scope: str) -> AwsKeys:
    """The keys in `scope`. AwsCredentialsError names each required key that is missing."""
    key_id, secret = get(scope, KEY_ID), get(scope, SECRET_KEY)
    missing = [name for name, value in ((KEY_ID, key_id), (SECRET_KEY, secret)) if not value]
    if missing:
        raise AwsCredentialsError(
            f"the secret scope {scope!r} has no {' or '.join(missing)} key, or the scope does not exist ({GUIDE})"
        )
    return AwsKeys(key_id, secret, get(scope, SESSION_TOKEN))  # type: ignore[arg-type]


_cache: dict[str, AwsKeys] = {}
_lock = threading.Lock()


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def bedrock_keys(cfg: HubConfig, *, getter: SecretGetter | None = None) -> AwsKeys:
    """The Bedrock keys of this deployment, read once per process: a run builds one client per skill and model."""
    scope = cfg.external.bedrock.secret_scope
    with _lock:
        hit = _cache.get(scope)
    if hit is not None:
        return hit
    if getter is None:
        from skillhub.auth import workspace_client

        getter = workspace_secret_getter(workspace_client())
    keys = read_keys(getter, scope)
    with _lock:
        _cache[scope] = keys
    return keys


def session_for(keys: AwsKeys, region: str, *, factory: Callable[..., Any] | None = None) -> Any:
    """A boto3 Session with the keys, in `region`. `factory` is a test seam that stands in for boto3.Session."""
    if factory is None:
        import boto3

        factory = boto3.Session
    return factory(
        aws_access_key_id=keys.access_key_id,
        aws_secret_access_key=keys.secret_access_key,
        aws_session_token=keys.session_token,
        region_name=region,
    )


def redact(text: str, keys: AwsKeys | None) -> str:
    """`text` with every key value replaced by ***."""
    for value in keys.values() if keys is not None else ():
        text = text.replace(value, "***")
    return text
