"""A small client for the GitHub REST API, for the Skill Hub's pull request flow: open a pull request from a draft, read
its gate and release, merge it. Standard library only. The rules of the flow are in backend.release.

The token belongs to the app. It is read at runtime from the Databricks secret named by SKILLHUB_GITHUB_TOKEN_SECRET
(`<scope>/<key>`), as the app principal, and kept for a minute. It is never logged and never returned. Errors carry
the kind of failure and the HTTP status, never GitHub's message and never the request.

Configuration: SKILLHUB_GITHUB_REPO (`owner/name`) and SKILLHUB_GITHUB_TOKEN_SECRET. When either is missing, or the
token cannot be read, `connect` raises GitHubNotConfigured, and the API answers 503 `github_not_configured`.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from databricks.sdk import WorkspaceClient

log = logging.getLogger("skillhub.app.github")

API = "https://api.github.com"
API_VERSION = "2022-11-28"
TIMEOUT_S = 10.0
ANSWER_S = 5.0  # a GET answer is reused this long; the release page refreshes every 20 to 30 seconds
TOKEN_S = 60.0  # the token is read again after this long, so a rotated secret is picked up
REPO_ENV = "SKILLHUB_GITHUB_REPO"
SECRET_ENV = "SKILLHUB_GITHUB_TOKEN_SECRET"
PAGE = 100  # the largest page GitHub lists
MAX_PAGES = 10  # a pull request with more files than this is refused, not read part way
MAX_CACHED = 512

_REPO = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
_SECRET = re.compile(r"[A-Za-z0-9_.-]{1,128}/[A-Za-z0-9_.-]{1,128}")

_cache: dict[tuple, tuple[float, Any]] = {}  # answers and the token, each with its expiry (monotonic seconds)
_lock = threading.Lock()


class GitHubNotConfigured(Exception):
    """The repository or the token is not set, or the token cannot be read. The API answers 503."""


class GitHubError(Exception):
    """GitHub refused or failed a request. `status` is the HTTP status, 0 when GitHub did not answer."""

    def __init__(self, status: int):
        self.status = status
        super().__init__(f"GitHub answered {status}" if status else "GitHub did not answer")


class GitHubNotFound(GitHubError):
    """404: the pull request, branch or commit is not there."""


class GitHubDenied(GitHubError):
    """401 or 403: the token may not do this, or the rate limit is spent."""


class GitHubRejected(GitHubError):
    """405, 409 or 422: GitHub would not make this change, for example a branch that exists or a merge it refuses."""


class GitHubUnavailable(GitHubError):
    """5xx, 429 or no answer at all."""


def _error(status: int) -> GitHubError:
    if status == 404:
        return GitHubNotFound(status)
    if status in (401, 403):
        return GitHubDenied(status)
    if status in (405, 409, 422):
        return GitHubRejected(status)
    return GitHubUnavailable(status)


def _cached(key: tuple, ttl: float, compute: Callable[[], Any]) -> Any:
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
    value = compute()  # outside the lock: a slow answer does not hold up the others
    with _lock:
        if len(_cache) >= MAX_CACHED:
            _cache.clear()
        _cache[key] = (time.monotonic() + ttl, value)
    return value


def _read_token(app_w: WorkspaceClient, scope: str, key: str) -> str:
    try:
        raw = app_w.secrets.get_secret(scope, key).value or ""
        token = base64.b64decode(raw, validate=True).decode("utf-8").strip()
    except Exception as exc:  # any failure means no token; the message of the failure is not kept
        log.warning("could not read the GitHub token secret: %s", type(exc).__name__)
        raise GitHubNotConfigured() from None
    if not token:
        raise GitHubNotConfigured()
    return token


def connect(app_w: WorkspaceClient, *, cached: bool = True) -> GitHubClient:
    """The client for the configured repository, with the app's token. `cached=False` reads every answer fresh, for
    the decisions that write: a commit must start from main's head as it is now, and a merge checks the gate as it is
    now."""
    repo = os.environ.get(REPO_ENV, "").strip()
    secret = os.environ.get(SECRET_ENV, "").strip()
    if not _REPO.fullmatch(repo) or not _SECRET.fullmatch(secret):
        raise GitHubNotConfigured()
    scope, key = secret.split("/", 1)
    token = _cached(("token", scope, key), TOKEN_S, lambda: _read_token(app_w, scope, key))
    return GitHubClient(repo, token, cached=cached)


@dataclass(frozen=True)
class Pull:
    number: int
    url: str
    state: str  # open or closed
    merged: bool
    merged_at: str | None
    merge_sha: str | None
    head_sha: str
    head_branch: str
    body: str
    mergeable_state: str | None
    title: str = ""
    author: str = ""  # the login of whoever opened it
    merged_by: str | None = None  # the login of whoever merged it; GitHub reports it on a single read only


@dataclass(frozen=True)
class Run:
    """A skills-publish run of a merge commit. `waiting` is set while a job waits for the production approval."""

    url: str
    status: str
    conclusion: str | None
    waiting: bool


@dataclass(frozen=True)
class WaitingRun:
    """A skills-publish run that waits for the production approval. `updated_at` is when it last changed, which for a
    waiting run is when it began to wait; `created_at` is when the merge started it."""

    id: int
    url: str
    number: int
    sha: str
    created_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True)
class Step:
    name: str
    status: str
    conclusion: str | None


@dataclass(frozen=True)
class Job:
    """A job of a workflow run, with its steps. `started_at` of a job that waits for an approval is when it began to
    wait."""

    name: str
    status: str
    conclusion: str | None
    created_at: datetime | None
    started_at: datetime | None
    steps: tuple[Step, ...]


def _time(value: object) -> datetime | None:
    """A GitHub timestamp as an aware datetime, or None."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _login(user: object) -> str | None:
    login = user.get("login") if isinstance(user, dict) else None
    return login if isinstance(login, str) and login else None


def _pull(raw: dict) -> Pull:
    head = raw.get("head") or {}
    return Pull(
        number=int(raw["number"]),
        url=str(raw.get("html_url") or ""),
        state=str(raw.get("state") or ""),
        merged=raw.get("merged") is True,
        merged_at=raw.get("merged_at"),
        merge_sha=raw.get("merge_commit_sha"),
        head_sha=str(head.get("sha") or ""),
        head_branch=str(head.get("ref") or ""),
        body=raw.get("body") or "",
        mergeable_state=raw.get("mergeable_state"),
        title=str(raw.get("title") or ""),
        author=_login(raw.get("user")) or "",
        merged_by=_login(raw.get("merged_by")),
    )


def _check_state(run: dict) -> str:
    if run.get("status") != "completed":
        return "pending"
    if run.get("conclusion") == "cancelled":  # a newer run replaced it, or someone cancelled it: no verdict
        return "cancelled"
    return "passed" if run.get("conclusion") == "success" else "failed"


class GitHubClient:
    """The repository's REST API. Each method returns only the fields the Skill Hub uses."""

    def __init__(
        self,
        repo: str,
        token: str,
        *,
        cached: bool = True,
        opener: Callable[..., Any] | None = None,
    ):
        self._repo = repo
        self._owner = repo.split("/", 1)[0]
        self._token = token
        self._cached_answers = cached
        self._opener = opener or urllib.request.urlopen  # looked up now, so a test can replace it
        self._root = f"/repos/{repo}"

    def _request(self, method: str, path: str, body: object | None = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "skillhub-app",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(API + path, data=data, method=method, headers=headers)
        # The raise happens outside the except blocks, so no exception keeps the request (and its token) as context.
        status = 0
        raw = b""
        try:
            with self._opener(req, timeout=TIMEOUT_S) as res:
                raw = res.read()
                status = res.status or 200
        except urllib.error.HTTPError as exc:
            status = exc.code
        except (urllib.error.URLError, OSError, ValueError):
            status = 0
        if status == 0 or status >= 300:
            raise _error(status)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            raise GitHubUnavailable(status) from None

    def get(self, path: str, query: dict[str, Any] | None = None) -> Any:
        full = path + ("?" + urllib.parse.urlencode(query) if query else "")
        if not self._cached_answers:
            return self._request("GET", full)
        return _cached(("GET", self._repo, full), ANSWER_S, lambda: self._request("GET", full))

    # --- branches and commits: the Git Data API -----------------------------------------------------------------

    def ref_sha(self, branch: str) -> str:
        return str(self.get(f"{self._root}/git/ref/heads/{branch}")["object"]["sha"])

    def commit_tree(self, commit: str) -> str:
        return str(self.get(f"{self._root}/git/commits/{commit}")["tree"]["sha"])

    def tree(self, sha: str, *, recursive: bool = False) -> list[dict[str, str]]:
        raw = self.get(f"{self._root}/git/trees/{sha}", {"recursive": 1} if recursive else None)
        if raw.get("truncated"):
            raise GitHubRejected(422)  # too large to read in one answer; a skill folder never is
        return [{"path": e["path"], "type": e["type"], "sha": e.get("sha")} for e in raw.get("tree", [])]

    def create_blob(self, data: bytes) -> str:
        body = {"content": base64.b64encode(data).decode(), "encoding": "base64"}
        return str(self._request("POST", f"{self._root}/git/blobs", body)["sha"])

    def create_tree(self, base_tree: str, entries: list[dict[str, Any]]) -> str:
        body = {"base_tree": base_tree, "tree": entries}
        return str(self._request("POST", f"{self._root}/git/trees", body)["sha"])

    def create_commit(self, message: str, tree: str, parent: str) -> str:
        body = {"message": message, "tree": tree, "parents": [parent]}
        return str(self._request("POST", f"{self._root}/git/commits", body)["sha"])

    def create_ref(self, branch: str, sha: str) -> None:
        self._request("POST", f"{self._root}/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha})

    def update_ref(self, branch: str, sha: str) -> None:
        self._request("PATCH", f"{self._root}/git/refs/heads/{branch}", {"sha": sha, "force": True})

    # --- pull requests ------------------------------------------------------------------------------------------

    def create_pull(self, title: str, head: str, base: str, body: str) -> Pull:
        raw = self._request(
            "POST", f"{self._root}/pulls", {"title": title, "head": head, "base": base, "body": body}
        )
        return _pull(raw)

    def pull(self, number: int) -> Pull:
        return _pull(self.get(f"{self._root}/pulls/{number}"))

    def pulls_for_branch(self, branch: str) -> list[Pull]:
        raw = self.get(
            f"{self._root}/pulls", {"head": f"{self._owner}:{branch}", "state": "all", "per_page": 5}
        )
        return [_pull(p) for p in raw]

    def pull_paths(self, number: int) -> list[str]:
        paths: list[str] = []
        for page in range(1, MAX_PAGES + 1):
            files = self.get(f"{self._root}/pulls/{number}/files", {"per_page": PAGE, "page": page})
            paths += [str(f["filename"]) for f in files]
            if len(files) < PAGE:
                return paths
        raise GitHubRejected(422)  # more files than the Hub reads; no skill pull request is that large

    def merge(self, number: int, head_sha: str) -> str:
        """A merge commit. `head_sha` makes GitHub refuse the merge when the branch has moved since it was read."""
        raw = self._request(
            "PUT", f"{self._root}/pulls/{number}/merge", {"merge_method": "merge", "sha": head_sha}
        )
        return str(raw["sha"])

    # --- checks and workflow runs -------------------------------------------------------------------------------

    def check_states(self, sha: str) -> dict[str, str]:
        """{check name: pending, passed, failed or cancelled} for the newest run of each check on the commit."""
        raw = self.get(f"{self._root}/commits/{sha}/check-runs", {"per_page": PAGE})
        newest: dict[str, dict] = {}
        for run in raw.get("check_runs") or []:
            name = run.get("name")
            if isinstance(name, str) and run.get("id", 0) >= newest.get(name, {}).get("id", 0):
                newest[name] = run
        return {name: _check_state(run) for name, run in newest.items()}

    def publish_run(self, sha: str, workflow: str) -> Run | None:
        """The newest run of a workflow for a commit, or None. Waiting means a job awaits an approval."""
        raw = self.get(f"{self._root}/actions/workflows/{workflow}/runs", {"head_sha": sha, "per_page": 5})
        runs = raw.get("workflow_runs") or []
        if not runs:
            return None
        run = max(runs, key=lambda r: (r.get("created_at") or "", r.get("id") or 0))
        status = str(run.get("status") or "")
        waiting = status == "waiting"
        if status not in ("completed", "waiting"):
            jobs = (
                self.get(f"{self._root}/actions/runs/{run['id']}/jobs", {"per_page": PAGE}).get("jobs") or []
            )
            waiting = any(job.get("status") == "waiting" for job in jobs)
        conclusion = run.get("conclusion")
        return Run(
            url=str(run.get("html_url") or ""),
            status=status,
            conclusion=str(conclusion) if conclusion else None,
            waiting=waiting,
        )

    def waiting_runs(self, workflow: str, limit: int) -> list[WaitingRun]:
        """The newest runs of a workflow that wait for an approval, at most `limit`. Read only: whoever approves does
        it in GitHub, as themselves."""
        raw = self.get(
            f"{self._root}/actions/workflows/{workflow}/runs", {"status": "waiting", "per_page": limit}
        )
        return [
            WaitingRun(
                id=int(run.get("id") or 0),
                url=str(run.get("html_url") or ""),
                number=int(run.get("run_number") or 0),
                sha=str(run.get("head_sha") or ""),
                created_at=_time(run.get("created_at")),
                updated_at=_time(run.get("updated_at")),
            )
            for run in raw.get("workflow_runs") or []
        ]

    def merged_pull_number(self, sha: str) -> int | None:
        """The number of the pull request whose merge commit is `sha`, or None: a commit pushed straight to main has
        none."""
        raw = self.get(f"{self._root}/commits/{sha}/pulls", {"per_page": 5})
        return next((int(p["number"]) for p in raw if p.get("merge_commit_sha") == sha), None)

    def run_jobs(self, run_id: int) -> list[Job]:
        """The jobs of a workflow run with their steps (one read)."""
        raw = self.get(f"{self._root}/actions/runs/{run_id}/jobs", {"per_page": PAGE})
        return [
            Job(
                name=str(job.get("name") or ""),
                status=str(job.get("status") or ""),
                conclusion=job.get("conclusion") or None,
                created_at=_time(job.get("created_at")),
                started_at=_time(job.get("started_at")),
                steps=tuple(
                    Step(str(st.get("name") or ""), str(st.get("status") or ""), st.get("conclusion") or None)
                    for st in job.get("steps") or []
                ),
            )
            for job in raw.get("jobs") or []
        ]
