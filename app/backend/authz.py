"""Fail-closed authorisation for the Skill Hub.

The app acts for the signed-in user. Every check runs with the user's forwarded token, and any check
that cannot be completed refuses the request. Only "not found" from Unity Catalog under the user token is
a definitive "no", and so is a plain PERMISSION_DENIED. A PERMISSION_DENIED whose message names a missing OAuth
scope, and any other failure, goes to the fallback: a user token without an OAuth scope for the skills API is
answered with PERMISSION_DENIED too. The fallback asks Unity Catalog for the user's effective privileges,
using the app service principal, and allows the read only if that shows USE CATALOG, USE SCHEMA and READ
VOLUME (direct or inherited). If the fallback fails, a single skill request is refused; the catalog skips
the department.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, TypeVar
from urllib.parse import quote

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound, PermissionDenied

from skillhub.config import HubConfig

log = logging.getLogger("skillhub.app.authz")


class _UnavailableUCSkills:
    """Stand-in used only when the UC skills client cannot be imported. Every use fails, so checks refuse."""

    def __init__(self, w: WorkspaceClient):
        raise NotImplementedError("UCSkills is not available")


try:
    from skillhub.registry.uc_skills import SkillInfo, UCSkills
except ImportError:  # the UC skills client is not installed: every check below fails closed
    from skillhub.registry.uc_skills import SkillInfo

    UCSkills = _UnavailableUCSkills  # type: ignore[misc,assignment]
    log.warning("UCSkills is unavailable; all skill authorisation checks will refuse")


class Reason(StrEnum):
    NO_USER_TOKEN = "no_user_token"
    SKILL_NOT_READABLE = "skill_not_readable"
    SKILL_NOT_FOUND = "skill_not_found"
    NOT_REVIEWER = "not_reviewer"
    AUTHZ_CHECK_FAILED = "authz_check_failed"
    EVALS_NOT_READABLE = "evals_not_readable"
    JOB_FORBIDDEN = "job_forbidden"
    JOB_NOT_FOUND = "job_not_found"
    JOB_CONFLICT = "job_conflict"
    JOB_FAILED = "job_failed"
    RUN_NOT_FOUND = "run_not_found"
    SWITCH_IN_PROGRESS = "switch_in_progress"
    ALREADY_LIVE = "already_live"
    FINISH_OR_REVERT_SWITCH = "finish_or_revert_switch"
    VERSION_NOT_FOUND = "version_not_found"
    UNKNOWN_MODEL = "unknown_model"
    VERSION_NOT_REQUESTED = "version_not_requested"
    DEPARTMENT_NOT_FOUND = "department_not_found"
    NO_ACCESS = "no_access"
    RESULTS_UNAVAILABLE = "results_unavailable"
    INVALID_RUN_ID = "invalid_run_id"
    SQL_FAILED = "sql_failed"
    UC_FAILED = "uc_failed"
    TRY_UNAVAILABLE = "try_unavailable"
    TRY_FAILED = "try_failed"
    TOO_MANY_MODELS = "too_many_models"
    TRY_BUSY = "try_busy"
    TRY_TIMEOUT = "try_timeout"
    PROMPT_TOO_LONG = "prompt_too_long"


class Forbidden(Exception):
    """Raised by any check that refuses the request. Mapped to HTTP 403 with a reason code."""

    def __init__(self, reason: Reason):
        self.reason = reason
        super().__init__(reason.value)


# READ VOLUME is the privilege that lets a principal read a skill. ALL_PRIVILEGES includes it.
READ_PRIVILEGES = frozenset({"READ_VOLUME", "ALL_PRIVILEGES"})
USE_SCHEMA_PRIVILEGES = frozenset({"USE_SCHEMA", "ALL_PRIVILEGES"})
USE_CATALOG_PRIVILEGES = frozenset({"USE_CATALOG", "ALL_PRIVILEGES"})
Decision = Literal["yes", "no", "unknown"]
T = TypeVar("T")


@dataclass(frozen=True)
class MeProfile:
    user: str
    groups: tuple[str, ...]


def fetch_me(user_w: WorkspaceClient) -> MeProfile:
    """Identity and groups of the signed-in user, from SCIM Me under the user token."""
    try:
        payload = user_w.api_client.do("GET", "/api/2.0/preview/scim/v2/Me")
        user = payload["userName"]
        if not isinstance(user, str) or not user.strip():
            raise ValueError("missing userName")
        raw_groups = payload["groups"] if "groups" in payload else []
        if not isinstance(raw_groups, list):
            raise ValueError("malformed groups")
        groups = tuple(sorted(g["display"] for g in raw_groups if g.get("display")))
    except Exception as exc:  # fail closed on 401, permission errors, bad payloads; never log the token
        log.warning("identity check failed: %s", type(exc).__name__)
        raise Forbidden(Reason.AUTHZ_CHECK_FAILED) from None
    return MeProfile(user=user, groups=groups)


def has_reviewer_role(me: MeProfile, cfg: HubConfig) -> bool:
    return cfg.roles.reviewers in me.groups


def reviewer_status(user_w: WorkspaceClient, cfg: HubConfig) -> Decision:
    try:
        me = fetch_me(user_w)
    except Forbidden:
        return "unknown"
    return "yes" if has_reviewer_role(me, cfg) else "no"


def is_reviewer(user_w: WorkspaceClient, cfg: HubConfig) -> bool:
    return reviewer_status(user_w, cfg) == "yes"


def require_reviewer(user_w: WorkspaceClient, cfg: HubConfig) -> str:
    me = fetch_me(user_w)
    if not has_reviewer_role(me, cfg):
        raise Forbidden(Reason.NOT_REVIEWER)
    return me.user


_PART = r"[A-Za-z0-9_-]+"
_SKILL_FQN = re.compile(rf"{_PART}\.{_PART}\.{_PART}")
_SCHEMA_FQN = re.compile(rf"{_PART}\.{_PART}")


def schema_of(fqn: str) -> str:
    return fqn.rsplit(".", 1)[0]


def _require_skill_fqn(fqn: str) -> None:
    if not isinstance(fqn, str) or not _SKILL_FQN.fullmatch(fqn):
        raise Forbidden(Reason.SKILL_NOT_READABLE)


def _require_schema_fqn(schema_fqn: str) -> None:
    if not isinstance(schema_fqn, str) or not _SCHEMA_FQN.fullmatch(schema_fqn):
        raise Forbidden(Reason.SKILL_NOT_READABLE)


def _assigned(payload: dict, names: set[str], wanted: frozenset[str]) -> bool:
    for assignment in payload.get("privilege_assignments") or []:
        holder = assignment.get("principal")
        if not isinstance(holder, str) or holder.casefold() not in names:
            continue
        for entry in assignment.get("privileges") or []:
            if entry.get("privilege") in wanted:
                return True
    return False


class RequestMemo:
    """Answers worked out at most once per key in one request, also when several threads ask at the same time.

    A failure is remembered too. It holds one user's answers, so it lives for one request and is never shared.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._answers: dict[object, Future] = {}

    def get(self, key: object, compute: Callable[[], T]) -> T:
        with self._lock:
            answer = self._answers.get(key)
            first = answer is None
            if first:
                answer = self._answers[key] = Future()
        if first:  # outside the lock, so other keys are not held up while this one is fetched
            try:
                answer.set_result(compute())
            except BaseException as exc:  # never leave a waiting thread without an answer
                answer.set_exception(exc)
        return answer.result()


def effective_read_schema(
    app_w: WorkspaceClient,
    principal: str,
    schema_fqn: str,
    groups: tuple[str, ...] = (),
    memo: RequestMemo | None = None,
) -> bool:
    """True when the user may read skills in the schema: USE CATALOG, USE SCHEMA and READ VOLUME, each direct
    or inherited (ALL PRIVILEGES counts for each).

    Called as the app service principal. Only assignments naming the user (case-insensitive) or one of the
    user's groups count; the server's filtering is not trusted. With a per-request `memo`, schemas that share
    a catalog ask for its permissions once.
    """
    _require_schema_fqn(schema_fqn)
    names = {principal.casefold(), *(g.casefold() for g in groups)}

    def effective(kind: str, name: str) -> dict:
        path = f"/api/2.1/unity-catalog/effective-permissions/{kind}/" + ".".join(
            quote(part, safe="") for part in name.split(".")
        )

        def fetch() -> dict:
            return app_w.api_client.do("GET", path, query={"principal": principal})

        return memo.get(("effective", path, principal), fetch) if memo is not None else fetch()

    schema = effective("schema", schema_fqn)  # one answer holds both READ VOLUME and USE SCHEMA
    return (
        _assigned(schema, names, READ_PRIVILEGES)
        and _assigned(schema, names, USE_SCHEMA_PRIVILEGES)
        and _assigned(effective("catalog", schema_fqn.split(".")[0]), names, USE_CATALOG_PRIVILEGES)
    )


def _is_scope_error(exc: Exception) -> bool:
    """True when the user-token call failed for a reason the fallback can still settle.

    PERMISSION_DENIED from Unity Catalog is a definite "no" unless its message names a missing OAuth scope: a
    user token without the skills API scope gets the same status. Any other error type (transport, 5xx) is
    not a decision either, so the fallback runs for it.
    """
    if isinstance(exc, PermissionDenied):
        return "scope" in str(exc).lower()
    return True


class CachedMe:
    """The signed-in user's identity, fetched at most once per request (a failure is remembered too).

    Safe to share between the threads of one request.
    """

    def __init__(self, user_w: WorkspaceClient):
        self._user_w = user_w
        self._memo = RequestMemo()

    def __call__(self) -> MeProfile:
        return self._memo.get("me", lambda: fetch_me(self._user_w))


def _fallback_read(user_w: WorkspaceClient, app_w: WorkspaceClient, fqn: str) -> SkillInfo:
    """The user token could not check the skill, so the user's effective permissions decide."""
    try:
        me = fetch_me(user_w)
        allowed = effective_read_schema(app_w, me.user, schema_of(fqn), me.groups)
        info = UCSkills(app_w).get(fqn) if allowed else None
    except Forbidden:
        raise
    except Exception as exc:
        log.warning("effective-permissions check failed for a skill: %s", type(exc).__name__)
        raise Forbidden(Reason.AUTHZ_CHECK_FAILED) from None
    if not allowed or info is None:
        raise Forbidden(Reason.SKILL_NOT_READABLE)
    return info


def authorize_read_as(
    user_w: WorkspaceClient, app_w: WorkspaceClient, fqn: str
) -> tuple[SkillInfo, WorkspaceClient]:
    """The skill, and the client that may read its files, when the user may read it. Fails closed.

    The client is the user's own, or the app's when the effective-permissions fallback allowed the read.
    """
    _require_skill_fqn(fqn)
    try:
        info = UCSkills(user_w).get(fqn)
    except NotFound:
        raise Forbidden(Reason.SKILL_NOT_READABLE) from None
    except Exception as exc:
        if not _is_scope_error(exc):  # a plain PERMISSION_DENIED is a definite "no"
            raise Forbidden(Reason.SKILL_NOT_READABLE) from None
        log.warning(
            "user-token skill check unavailable (%s); checking effective permissions", type(exc).__name__
        )
        return _fallback_read(user_w, app_w, fqn), app_w
    if info is None:
        raise Forbidden(Reason.SKILL_NOT_READABLE)
    return info, user_w


def authorize_read(user_w: WorkspaceClient, app_w: WorkspaceClient, fqn: str) -> SkillInfo:
    """Return the skill when the user may read it. Otherwise raise Forbidden. Fails closed."""
    return authorize_read_as(user_w, app_w, fqn)[0]


def list_visible_skills(
    user_w: WorkspaceClient,
    app_w: WorkspaceClient,
    schema_fqn: str,
    me: Callable[[], MeProfile] | None = None,
    memo: RequestMemo | None = None,
) -> list[SkillInfo]:
    """The skills the user can see in one schema. Uses the user's view; falls back to effective permissions.

    `me` is a per-request identity getter (`CachedMe`) and `memo` a per-request `RequestMemo`, so a catalog
    load resolves the user and the catalog's permissions once, not once per department.
    """
    _require_schema_fqn(schema_fqn)
    try:
        return UCSkills(user_w).list(schema_fqn)
    except NotFound:
        return []
    except Exception as exc:
        if not _is_scope_error(exc):  # a plain PERMISSION_DENIED: the user cannot read this schema
            return []
        log.warning("user-token listing unavailable (%s); checking effective permissions", type(exc).__name__)
    try:
        profile = me() if me is not None else fetch_me(user_w)
        if not effective_read_schema(app_w, profile.user, schema_fqn, profile.groups, memo):
            return []
        return UCSkills(app_w).list(schema_fqn)
    except Forbidden:
        raise
    except Exception:
        raise Forbidden(Reason.AUTHZ_CHECK_FAILED) from None
