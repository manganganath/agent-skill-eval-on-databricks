"""Release webhooks. After a publish, a switch or a retire, skillhub POSTs a JSON event to each URL in
hub.yaml `notifications.webhooks` that subscribes to the action.

The release has already happened, and its row is in `skill_versions`, so delivery never raises. Each webhook is
tried up to three times, and a failure comes back as one warning line for the run's output. A message names only the
URL's host: a URL from a secret scope can carry a token. With `signing_secret`, each event carries
`X-SkillHub-Signature-256: sha256=<hex HMAC-SHA256 of the body>`, as GitHub signs its webhooks, so a receiver can
check that it came from this hub.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from skillhub.config import RELEASE_EVENTS, HubConfig, SecretRef, Webhook
from skillhub.naming import live_fqn
from skillhub.registry.journal import JournalRow

ATTEMPTS = 3
TIMEOUT_S = 10.0
BACKOFF_S = (1.0, 2.0)  # before the second and the third attempt
SecretGetter = Callable[[str, str], str | None]


def event(cfg: HubConfig, row: JournalRow) -> dict[str, Any]:
    """The JSON event of a release row of the journal."""
    return {
        "id": str(uuid.uuid4()),
        "type": f"skill.{row.action}",
        "action": row.action,
        "skill_fqn": live_fqn(cfg, row.dept, row.skill),
        "dept": row.dept,
        "skill": row.skill,
        "version": row.version,
        "from_version": row.from_version,
        "to_version": row.to_version,
        "versioned_fqn": row.versioned_fqn,
        "content_hash": row.content_hash,
        "git_sha": row.git_sha,
        "eval_run_id": row.eval_run_id,
        "actor": row.actor,
        "job_run_id": row.job_run_id,
        "ts": row.ts.isoformat(),
    }


def workspace_secrets(w: Any) -> SecretGetter:
    """Read a secret with a WorkspaceClient, as the identity it runs as. None when it cannot be read: the caller
    reports the webhook as not delivered."""

    def get(scope: str, key: str) -> str | None:
        try:
            raw = w.secrets.get_secret(scope, key).value or ""
            return base64.b64decode(raw, validate=True).decode("utf-8").strip() or None
        except Exception:  # the failure's text is not kept: it could quote the request
            return None

    return get


def _host(url: str) -> str:
    return urlsplit(url).hostname or "an unnamed host"


def _resolve(hook: Webhook, get: SecretGetter | None) -> tuple[str | None, bytes | None, str | None]:
    """The URL and the signing key, or a reason the webhook cannot be sent."""

    def read(ref: SecretRef, what: str) -> tuple[str | None, str | None]:
        value = get(ref.scope, ref.key) if get else None
        return (value, None) if value else (None, f"its {what} is not readable at {ref.scope}/{ref.key}")

    url, why = (hook.url, None) if hook.url else read(hook.url_secret, "URL")  # type: ignore[arg-type]
    if why:
        return None, None, why
    if not str(url).startswith("https://"):
        return None, None, "its URL in the secret scope is not an https:// URL"
    key = None
    if hook.signing_secret:
        secret, why = read(hook.signing_secret, "signing secret")
        if why:
            return url, None, why  # the URL is known, so the warning can name its host
        key = secret.encode()  # type: ignore[union-attr]
    return url, key, None


def _retryable(status: int) -> bool:
    return status in (408, 429) or status >= 500


def deliver(
    cfg: HubConfig,
    row: JournalRow,
    *,
    get_secret: SecretGetter | None = None,
    post: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> list[str]:
    """Send the row's event to each webhook that subscribes to its action. One warning per webhook not delivered."""
    post, sleep = post or httpx.post, sleep or time.sleep
    hooks = [h for h in cfg.notifications.webhooks if row.action in RELEASE_EVENTS and row.action in h.events]
    if not hooks:
        return []
    payload = event(cfg, row)
    body = json.dumps(payload, sort_keys=True).encode()
    warnings: list[str] = []
    for i, hook in enumerate(hooks):
        url, key, why = _resolve(hook, get_secret)
        label = f"webhook {i + 1}" + (f" ({_host(url)})" if url else "")
        if why:
            warnings.append(f"{label} not sent: {why}")
            continue
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "skillhub-webhooks",
            "X-SkillHub-Event": payload["type"],
            "X-SkillHub-Delivery": payload["id"],
        }
        if key:
            headers["X-SkillHub-Signature-256"] = "sha256=" + hmac.new(key, body, hashlib.sha256).hexdigest()
        last = ""
        for attempt in range(ATTEMPTS):
            if attempt:
                sleep(BACKOFF_S[attempt - 1])
            try:
                response = post(url, content=body, headers=headers, timeout=TIMEOUT_S, follow_redirects=False)
            except httpx.HTTPError as exc:  # its text could quote the URL
                last = type(exc).__name__
                continue
            if 200 <= response.status_code < 300:
                last = ""
                break
            last = f"HTTP {response.status_code}"
            if not _retryable(response.status_code):
                break
        if last:
            warnings.append(f"{label} not delivered: {last} after {attempt + 1} attempt(s)")
    return warnings
