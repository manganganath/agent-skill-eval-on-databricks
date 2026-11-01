"""Release webhooks. After a publish, a switch or a retire, skillhub POSTs a JSON event to each URL in
hub.yaml `notifications.webhooks` that subscribes to the action.

The release has already happened, and its row is in `skill_versions`, so delivery never raises and never holds the
serialised skill-switch Job for long:
- each webhook is tried up to three times, within DEADLINE_S in all, in a daemon thread that the Job does not wait
  for past that time;
- only the response's status line is read, never its body;
- a failure comes back as one warning line for the run's output.

A message names only the URL's host: a URL from a secret scope can carry a token. With `signing_secret`, each event
carries `X-SkillHub-Signature-256: sha256=<hex HMAC-SHA256 of the body>`, as GitHub signs its webhooks, so a receiver
can check that it came from this hub. The event's `text` is a one-line summary, which Slack and Teams show.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import httpx

from skillhub.config import RELEASE_EVENTS, WEBHOOK_URL, HubConfig, SecretRef, Webhook
from skillhub.naming import live_fqn
from skillhub.registry.journal import JournalRow

ATTEMPTS = 3
TIMEOUT_S = 10.0  # each attempt's connect, write and read
BACKOFF_S = (1.0, 2.0)  # before the second and the third attempt
DEADLINE_S = 30.0  # all of one webhook's attempts, including reading its secrets
SecretGetter = Callable[[str, str], str | None]

# httpx logs each request's full URL at INFO, and a webhook URL can carry a token.
logging.getLogger("httpx").setLevel(logging.WARNING)


def complete(row: JournalRow, history: Iterable[JournalRow]) -> JournalRow:
    """The row with what its own action does not record, from the skill's history: a switch names the version it
    went to, and a retire the version it took down; the event carries that version's content hash, Git commit and
    eval run."""
    rows = list(history)
    if row.action == "retired" and row.from_version is None:
        before = [r for r in rows if r.action == "switched" and r.ts <= row.ts]
        if before:
            row = dataclasses.replace(row, from_version=before[-1].to_version)
    version = (
        row.to_version if row.action == "switched" else row.from_version if row.action == "retired" else None
    )
    if version is None:
        return row
    published = [r for r in rows if r.action == "published" and r.version == version]
    if not published:
        return row
    p = published[-1]
    return dataclasses.replace(
        row,
        version=row.version if row.version is not None else version,
        versioned_fqn=row.versioned_fqn or p.versioned_fqn,
        content_hash=row.content_hash or p.content_hash,
        git_sha=row.git_sha or p.git_sha,
        eval_run_id=row.eval_run_id or p.eval_run_id,
    )


def _summary(row: JournalRow) -> str:
    name = f"{row.dept}/{row.skill}"
    if row.action == "published":
        return f"Skill {name}: version {row.version} published by {row.actor}"
    if row.action == "switched":
        return (
            f"Skill {name}: live version switched from {row.from_version} to {row.to_version} by {row.actor}"
        )
    return f"Skill {name}: retired by {row.actor} (it was version {row.from_version})"


def event(cfg: HubConfig, row: JournalRow) -> dict[str, Any]:
    """The JSON event of a release row of the journal."""
    return {
        "id": str(uuid.uuid4()),
        "type": f"skill.{row.action}",
        "action": row.action,
        "text": _summary(row),
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


def http_post(url: str, *, content: bytes, headers: dict, timeout: float, follow_redirects: bool) -> Any:
    """POST and read only the status line and headers: a receiver cannot hold the Job with a slow or endless body."""
    with httpx.stream(
        "POST", url, content=content, headers=headers, timeout=timeout, follow_redirects=follow_redirects
    ) as response:
        return SimpleNamespace(status_code=response.status_code, headers=dict(response.headers))


def _host(url: str) -> str:
    try:
        return urlsplit(url).hostname or "an unnamed host"
    except ValueError:
        return "an unnamed host"


def _resolve(hook: Webhook, get: SecretGetter | None) -> tuple[str | None, bytes | None, str | None]:
    """The URL and the signing key, or a reason the webhook cannot be sent. The URL is returned whenever it is known,
    so a warning can name its host."""

    def read(ref: SecretRef, what: str) -> tuple[str | None, str | None]:
        value = get(ref.scope, ref.key) if get else None
        return (value, None) if value else (None, f"its {what} is not readable at {ref.scope}/{ref.key}")

    url, why = (hook.url, None) if hook.url else read(hook.url_secret, "URL")  # type: ignore[arg-type]
    if why:
        return None, None, why
    if not WEBHOOK_URL.fullmatch(str(url)):
        return None, None, "its URL in the secret scope is not an https:// URL"
    key = None
    if hook.signing_secret:
        secret, why = read(hook.signing_secret, "signing secret")
        if why:
            return url, None, why
        key = secret.encode()  # type: ignore[union-attr]
    return url, key, None


def _retryable(status: int) -> bool:
    return status in (408, 429) or status >= 500


def _retry_after(response: Any) -> float | None:
    try:
        value = float((getattr(response, "headers", None) or {}).get("retry-after", ""))
    except (TypeError, ValueError):
        return None
    return value if 0 <= value <= DEADLINE_S else None


def _send(hook: Webhook, payload: dict, body: bytes, get, post, sleep, clock) -> tuple[str, str | None]:
    """One webhook: (its label, None when delivered or why not). Never raises."""
    start = clock()
    label = "webhook"
    try:
        url, key, why = _resolve(hook, get)
        label = f"webhook ({_host(url)})" if url else label
        if why:
            return label, f"not sent: {why}"
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "skillhub-webhooks",
            "X-SkillHub-Event": payload["type"],
            "X-SkillHub-Delivery": payload["id"],
        }
        if key:
            headers["X-SkillHub-Signature-256"] = "sha256=" + hmac.new(key, body, hashlib.sha256).hexdigest()
        last, attempt, wait = "", 0, 0.0
        for attempt in range(1, ATTEMPTS + 1):
            if attempt > 1:
                pause = wait or BACKOFF_S[attempt - 2]
                if clock() - start + pause >= DEADLINE_S:
                    break
                sleep(pause)
            left = DEADLINE_S - (clock() - start)
            if left <= 0:
                break
            try:
                response = post(
                    url, content=body, headers=headers, timeout=min(TIMEOUT_S, left), follow_redirects=False
                )
            except httpx.HTTPError as exc:  # its text could quote the URL
                last, wait = type(exc).__name__, 0.0
                continue
            if 200 <= response.status_code < 300:
                return label, None
            last, wait = f"HTTP {response.status_code}", _retry_after(response) or 0.0
            if not _retryable(response.status_code):
                break
        return label, f"not delivered: {last or 'out of time'} after {attempt} attempt(s)"
    except Exception as exc:  # anything else, such as a URL that does not parse; its text could quote the URL
        return label, f"not delivered: {type(exc).__name__}"


def deliver(
    cfg: HubConfig,
    row: JournalRow,
    *,
    history: Iterable[JournalRow] = (),
    get_secret: SecretGetter | None = None,
    post: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> list[str]:
    """Send the row's event to each webhook that subscribes to its action. One warning per webhook not delivered.
    Each webhook runs in a daemon thread that is given up after DEADLINE_S, so a receiver can delay a release by at
    most that much, and never blocks the process from exiting."""
    hooks = [h for h in cfg.notifications.webhooks if row.action in RELEASE_EVENTS and row.action in h.events]
    if not hooks:
        return []
    post, sleep = post or http_post, sleep or time.sleep
    payload = event(cfg, complete(row, history))
    body = json.dumps(payload, sort_keys=True).encode()
    warnings: list[str] = []
    for i, hook in enumerate(hooks, 1):
        result: list[tuple[str, str | None]] = []
        worker = threading.Thread(
            target=lambda out, *args: out.append(_send(*args)),
            args=(result, hook, payload, body, get_secret, post, sleep, clock),
            daemon=True,
        )
        worker.start()
        worker.join(DEADLINE_S + 1)
        label, why = result[0] if result else ("webhook", f"not delivered within {DEADLINE_S:.0f} s")
        if why:
            warnings.append(f"{label.replace('webhook', f'webhook {i}', 1)} {why}")
    return warnings
