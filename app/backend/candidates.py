"""Candidate skills: skill content under review in a pull request, shown in the Skill Hub before it is released.

Where they come from. Before the PR eval starts, the PR workflow uploads the pull request's checkout to
`<workspace_root>/<catalog>/candidates/<run key>/`: `skills/<dept>/<skill>/...`, `fixtures/` and an `.uploaded_at`
marker (Unix seconds). The run key is `make_run_key("pr<N>-<sha7>-a<attempt>")`, for example `pr5_c7ecd3e_a1`. The
workflow uploads the whole `skills/` tree, so the skills under review are the ones the PR eval evaluates: the
`skills` parameter of the skill-eval Job run started with that run key, and every skill with eval rows for the key.
The nightly cleanup deletes a candidate folder 168 hours after its upload (`skillhub.registry.stage`).

Who sees a candidate. Every candidate route applies this one rule:

1. Only members of `roles.authors` or `roles.reviewers` (from the user's SCIM groups, read with the user's token)
   see candidates. For anyone else the listing is empty and the other routes answer 403 `not_author`, before the
   app reads anything as its service principal.
2. A reviewer sees the candidates of every configured department.
3. An author sees a candidate only when they may read the live skills of its department: Unity Catalog's
   effective permissions for the user and the user's groups on the department's live schema show USE CATALOG,
   USE SCHEMA and READ VOLUME (`authz.effective_read_schema`, the test the read fallback uses). Any failure of that
   check hides the department (fail closed). A hidden candidate answers 404, the same as a missing one.

Only after these checks does the app principal read the folder. It inherits CAN_EDIT on `candidates/` from the
`<workspace_root>/<catalog>` folder ACL (`skillhub.results.experiments.root_acl`), which is enough to list and read.
Eval rows are always read with the user's own SQL grants.
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from collections.abc import Callable, Collection, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import islice

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import DatabricksError, NotFound
from databricks.sdk.service.workspace import ObjectType

from backend import authz, jobs, skill_ref
from backend.authz import MeProfile, Reason, RequestMemo
from backend.deps import problem
from skillhub.bundle import BundleError, is_ignored, parse_frontmatter
from skillhub.config import HubConfig
from skillhub.lint import MAX_BUNDLE_BYTES, NAME_RE
from skillhub.naming import candidates_root, live_schema, make_run_key
from skillhub.registry.stage import CANDIDATE_MARKER, CANDIDATE_MAX_AGE_HOURS

log = logging.getLogger("skillhub.app.candidates")

# pr<N>_<sha7>, with the workflow's _a<attempt> suffix. Used with fullmatch.
PR_KEY = re.compile(r"pr([1-9][0-9]{0,8})_([0-9a-f]{7})(?:_a([0-9]{1,4}))?")
# A Hub run of a candidate is keyed <candidate key>_h<8 hex>, so it never collides with the PR gate's key and its
# rows can still be found from the candidate. The runner applies make_run_key, which cuts a key at 40 characters.
HUB_RUN = "_h"
_HUB_TAIL = re.compile(r"[0-9a-f]{8}")
RUN_KEY_MAX = 40
# Bounds on what one request reads.
MAX_ENTRIES = 1000  # entries of the candidates folder looked at
MAX_KEYS = 100  # candidate folders (pull request runs) looked at
MAX_RUNS = 200  # skill-eval Job runs looked at
MAX_CANDIDATES = 200  # candidate skills listed
MAX_FILES = 200  # files of one candidate skill
MAX_BYTES = 4 * MAX_BUNDLE_BYTES  # all files of one candidate skill, evals/ included
MAX_SKILL_MD = MAX_BUNDLE_BYTES
# A candidate folder lives at most 168 hours (plus a nightly cleanup cycle), so older Job runs name none.
RUNS_WINDOW_HOURS = CANDIDATE_MAX_AGE_HOURS + 24


class CandidateTooLarge(Exception):
    """A candidate skill has more files or bytes than the Skill Hub shows."""


@dataclass(frozen=True)
class CandidateKey:
    run_key: str
    pr_number: int
    sha7: str
    attempt: int | None


@dataclass
class StartedEval:
    """What the PR eval of one run key was started for: (dept, skill) pairs and the full commit, if known."""

    skills: set[tuple[str, str]] = field(default_factory=set)
    git_sha: str | None = None


def parse_key(value: object) -> CandidateKey | None:
    """The pull request run a candidate folder name stands for, or None for any other name."""
    m = PR_KEY.fullmatch(value) if isinstance(value, str) else None
    if m is None:
        return None
    return CandidateKey(value, int(m[1]), m[2], int(m[3]) if m[3] else None)


def hub_run_key(key: str) -> str:
    """A fresh run key for one Hub run of the candidate: never the PR gate's key, never the same twice."""
    out = f"{key}{HUB_RUN}{secrets.token_hex(4)}"
    if len(out) > RUN_KEY_MAX:
        raise ValueError(f"candidate key {key!r} is too long for a Hub run key")
    return out


def owns_run_key(key: str, run_key: str) -> bool:
    """True for the candidate's own PR eval key and for the keys of the Hub runs started for it."""
    if run_key == key:
        return True
    prefix = key + HUB_RUN
    return run_key.startswith(prefix) and bool(_HUB_TAIL.fullmatch(run_key[len(prefix) :]))


def valid_skill_name(name: object) -> bool:
    return isinstance(name, str) and bool(NAME_RE.fullmatch(name))


# --- who sees candidates ----------------------------------------------------------------------------------


def may_see(me: MeProfile, cfg: HubConfig) -> bool:
    """Rule 1: authors and reviewers only."""
    return cfg.roles.authors in me.groups or authz.has_reviewer_role(me, cfg)


def _reads_live(
    app_w: WorkspaceClient, cfg: HubConfig, me: MeProfile, dept: str, memo: RequestMemo | None
) -> bool:
    """Rule 3: the user's effective READ VOLUME, USE SCHEMA and USE CATALOG on the live schema. Fails closed."""
    try:
        return authz.effective_read_schema(app_w, me.user, live_schema(cfg, dept), me.groups, memo)
    except Exception as exc:  # any failure hides the department
        log.warning("live read check failed for department %s: %s", dept, type(exc).__name__)
        return False


def visible_departments(
    app_w: WorkspaceClient, cfg: HubConfig, me: MeProfile, pool: ThreadPoolExecutor, memo: RequestMemo
) -> set[str]:
    """The departments whose candidates the user may see (rules 2 and 3). Call it for an author or a reviewer only,
    from the request thread: it waits on the pool."""
    if authz.has_reviewer_role(me, cfg):
        return set(cfg.departments)
    checks = {d: pool.submit(_reads_live, app_w, cfg, me, d, memo) for d in cfg.departments}
    return {d for d, check in checks.items() if check.result()}


def require_visible(
    app_w: WorkspaceClient,
    cfg: HubConfig,
    me: MeProfile,
    depts: Iterable[str],
    memo: RequestMemo | None = None,
) -> None:
    """403 `not_author` for a user who is neither author nor reviewer; 404 `candidate_not_found` for a department
    the user may not see. Nothing has been read as the app principal before this returns."""
    if not may_see(me, cfg):
        raise problem(403, Reason.NOT_AUTHOR)
    if authz.has_reviewer_role(me, cfg):
        return
    for dept in sorted(set(depts)):
        if not _reads_live(app_w, cfg, me, dept, memo):
            raise problem(404, Reason.CANDIDATE_NOT_FOUND)


# --- the candidate folders, read as the app principal --------------------------------------------------------


def folder(cfg: HubConfig, key: str) -> str:
    return f"{candidates_root(cfg)}/{key}"


def skill_dir(cfg: HubConfig, key: str, dept: str, name: str) -> str:
    return f"{folder(cfg, key)}/skills/{dept}/{name}"


def _aliases(path: str) -> tuple[str, str]:
    """A workspace path with and without the /Workspace prefix: the API answers without it."""
    return path, path.removeprefix("/Workspace")


def list_keys(app_w: WorkspaceClient, cfg: HubConfig) -> list[CandidateKey]:
    """The pull request runs that have a folder directly under the candidates root, newest pull request first."""
    root = candidates_root(cfg)
    try:
        entries = list(islice(app_w.workspace.list(root), MAX_ENTRIES))
    except NotFound:
        return []
    parents = set(_aliases(root))
    keys: list[CandidateKey] = []
    for e in entries:
        parent, _, name = (e.path or "").rpartition("/")
        key = parse_key(name)
        if e.object_type == ObjectType.DIRECTORY and parent in parents and key is not None:
            keys.append(key)
    keys.sort(key=lambda k: (k.pr_number, k.attempt or 0, k.run_key), reverse=True)
    return keys[:MAX_KEYS]


def _download(app_w: WorkspaceClient, path: str, limit: int) -> bytes | None:
    """The file's bytes, at most `limit` + 1 of them. None when it is gone."""
    try:
        return app_w.workspace.download(path).read(limit + 1)
    except NotFound:
        return None


def uploaded_at(app_w: WorkspaceClient, cfg: HubConfig, key: str) -> datetime | None:
    """The upload time from the folder's .uploaded_at marker. None when it is absent or unreadable."""
    try:
        data = _download(app_w, f"{folder(cfg, key)}/{CANDIDATE_MARKER}", 64)
    except DatabricksError as exc:
        log.warning("could not read the upload marker of %s: %s", key, type(exc).__name__)
        return None
    text = (data or b"").decode("utf-8", "replace").strip()
    if not text.isdecimal() or len(text) > 12:
        return None
    return datetime.fromtimestamp(int(text), UTC)


def newest_per_pr(
    keys: Iterable[CandidateKey], stamps: Mapping[str, datetime | None]
) -> list[tuple[CandidateKey, datetime | None]]:
    """The latest upload of each pull request: an earlier push or attempt is superseded by the newer one."""

    def order(k: CandidateKey) -> tuple[float, int, str]:
        stamp = stamps.get(k.run_key)
        return (stamp.timestamp() if stamp else 0.0, k.attempt or 0, k.run_key)

    out: dict[int, tuple[CandidateKey, datetime | None]] = {}
    for k in sorted(keys, key=order, reverse=True):
        out.setdefault(k.pr_number, (k, stamps.get(k.run_key)))
    return list(out.values())


def read_skill_md(app_w: WorkspaceClient, cfg: HubConfig, key: str, dept: str, name: str) -> bytes | None:
    return _download(app_w, f"{skill_dir(cfg, key, dept, name)}/SKILL.md", MAX_SKILL_MD)


def description(skill_md: bytes) -> str | None:
    try:
        return parse_frontmatter(skill_md).get("description") or None
    except BundleError:
        return None


def exists(app_w: WorkspaceClient, path: str) -> bool:
    try:
        app_w.workspace.get_status(path)
    except NotFound:
        return False
    return True


def read_skill_files(
    app_w: WorkspaceClient, cfg: HubConfig, key: str, dept: str, name: str, pool: ThreadPoolExecutor
) -> dict[str, bytes]:
    """Every file of one candidate skill, evals/ included, by path relative to the skill folder. {} when the folder
    is gone. Files the bundle loader ignores are left out, so the content hash matches the eval's."""
    base = skill_dir(cfg, key, dept, name)
    try:
        entries = list(islice(app_w.workspace.list(base, recursive=True), MAX_FILES + 1))
    except NotFound:
        return {}
    if len(entries) > MAX_FILES:
        raise CandidateTooLarge(f"more than {MAX_FILES} files")
    prefixes = tuple(p + "/" for p in _aliases(base))
    paths: dict[str, str] = {}
    for e in entries:
        path = e.path or ""
        rel = next((path[len(p) :] for p in prefixes if path.startswith(p)), "")
        if e.object_type != ObjectType.FILE or not rel or is_ignored(rel):
            continue
        if any(part in ("", ".", "..") for part in rel.split("/")):
            continue
        paths[rel] = path
    reads = {rel: pool.submit(_download, app_w, path, MAX_BYTES) for rel, path in sorted(paths.items())}
    files: dict[str, bytes] = {}
    total = 0
    for rel, read in reads.items():
        data = read.result()
        if data is None:
            continue  # removed while it was read
        total += len(data)
        if total > MAX_BYTES:
            raise CandidateTooLarge(f"more than {MAX_BYTES} bytes")
        files[rel] = data
    return files


def fixtures_root(app_w: WorkspaceClient, cfg: HubConfig, key: str) -> str:
    """The upload's fixtures folder, or "" (the Job's default fixtures) when the upload has none."""
    path = f"{folder(cfg, key)}/fixtures"
    return path if exists(app_w, path) else ""


# --- what each run key evaluates --------------------------------------------------------------------------------

_SHA = re.compile(r"[0-9a-f]{7,40}")


def tested_skills(app_w: WorkspaceClient, keys: Collection[str]) -> dict[str, StartedEval]:
    """{run key: StartedEval} from the skill-eval Job runs started with these keys in the last days.

    Read as the app principal, which may manage the Job's runs. A failure only loses this hint: the eval rows still
    name the skills that have results."""
    wanted = set(keys)
    if not wanted:
        return {}
    since = int((time.time() - RUNS_WINDOW_HOURS * 3600) * 1000)
    out: dict[str, StartedEval] = {}
    try:
        runs = app_w.jobs.list_runs(job_id=jobs.job_id(app_w, "skill-eval"), start_time_from=since)
        for run in islice(runs, MAX_RUNS):
            params = {p.name: p.value for p in (getattr(run, "job_parameters", None) or [])}
            try:
                key = make_run_key(params.get("run_key") or "")
            except ValueError:
                continue
            if key not in wanted:
                continue
            tested = out.setdefault(key, StartedEval())
            for ref in (params.get("skills") or "").split(","):
                dept, sep, name = ref.strip().partition("/")
                if sep:
                    tested.skills.add((dept, name))
            source = params.get("source") or ""
            sha = source.removeprefix("git:") if source.startswith("git:") else ""
            if tested.git_sha is None and _SHA.fullmatch(sha):
                tested.git_sha = sha
    except (jobs.JobNotFound, DatabricksError) as exc:
        log.warning("could not list skill-eval runs for the candidates: %s", type(exc).__name__)
    return out


# --- eval rows ----------------------------------------------------------------------------------------------------


def _stamp(value: object) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value or "")


def owned_rows(rows: Iterable[Mapping[str, object]], key: str) -> list[dict]:
    """The rows of the candidate's own run keys, newest first. One row per child run."""
    owned = [dict(r) for r in rows if owns_run_key(key, str(r.get("run_key") or ""))]
    owned.sort(key=lambda r: (_stamp(r.get("started_at")), str(r.get("run_id") or "")), reverse=True)
    seen: set[object] = set()
    out = []
    for r in owned:
        run_id = r.get("run_id")
        if run_id is not None and run_id in seen:
            continue  # the same child run synced twice
        seen.add(run_id)
        out.append(r)
    return out


def latest_rows(rows: Iterable[Mapping[str, object]], key: str) -> list[dict]:
    """The newest of the candidate's rows for each (skill, model, mode)."""
    cells: dict[tuple[object, object, object], dict] = {}
    for r in owned_rows(rows, key):
        cells.setdefault((r.get("skill"), r.get("model_profile"), r.get("mode")), r)
    return list(cells.values())


def verdicts(latest: Iterable[Mapping[str, object]]) -> dict[str, str]:
    """{model profile: worst verdict across modes} for one skill. The baseline (without_skill) never counts."""
    by_model: dict[str, list[str | None]] = {}
    for r in latest:
        if r.get("mode") == skill_ref.BASELINE_MODE:
            continue
        by_model.setdefault(str(r.get("model_profile")), []).append(r.get("verdict"))  # type: ignore[arg-type]
    return {m: skill_ref.worst_verdict(v) for m, v in sorted(by_model.items())}


def commit(key: CandidateKey, rows: Iterable[Mapping[str, object]], tested: StartedEval | None = None) -> str:
    """The candidate's commit: the full sha from its PR eval when one names it, else the key's short sha."""
    for r in owned_rows(rows, key.run_key):
        sha = str(r.get("git_sha") or "")
        if r.get("run_key") == key.run_key and sha.startswith(key.sha7) and _SHA.fullmatch(sha):
            return sha
    if tested is not None and tested.git_sha and tested.git_sha.startswith(key.sha7):
        return tested.git_sha
    return key.sha7


def content_hash(latest: Iterable[Mapping[str, object]]) -> str | None:
    return next((str(r["content_hash"]) for r in latest if r.get("content_hash")), None)


def workspace_call(fn: Callable[[], object]):
    """Run one Workspace read; a failure other than "not found" is 502 `candidate_unreadable`."""
    try:
        return fn()
    except CandidateTooLarge:
        raise problem(502, Reason.CANDIDATE_UNREADABLE) from None
    except DatabricksError as exc:
        log.warning("candidate folder read failed: %s", type(exc).__name__)
        raise problem(502, Reason.CANDIDATE_UNREADABLE) from None
