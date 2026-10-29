from __future__ import annotations

import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from databricks.sdk.errors import NotFound, PermissionDenied
from databricks.sdk.service.workspace import ObjectType

from skillhub import SkillHubError
from skillhub.bundle import SkillBundle, content_hash
from skillhub.config import HubConfig
from skillhub.lint import assert_clean
from skillhub.naming import candidates_root, live_fqn, run_schema, version_fqn
from skillhub.registry.journal import Journal
from skillhub.registry.uc_skills import UCSkills, _check_rel

_log = logging.getLogger(__name__)

RUN_SCHEMA_RE = re.compile(r"run_[a-z0-9_]+_(common|external|dept_[a-z0-9_]+)")


class StageError(SkillHubError):
    """Staging cannot produce a consistent run scope."""


@dataclass
class StagedSkill:
    name: str
    dept: str
    staged_fqn: str
    production_fqn: str
    content_hash: str
    etag: str
    origin: Literal["candidate", "live", "version"]


@dataclass
class RunScope:
    dept: str
    schema_fqn: str
    skills: list[StagedSkill]


def _stage_one(
    cfg: HubConfig,
    uc: UCSkills,
    schema: str,
    name: str,
    dept: str,
    files: dict[str, bytes],
    origin: Literal["candidate", "live", "version"],
) -> StagedSkill:
    fqn = f"{schema}.{name}"
    # evals and rubrics are never uploaded; every path is checked before anything is written
    files = {_check_rel(rel): data for rel, data in files.items() if not rel.startswith("evals/")}
    existing = uc.get(fqn)
    if existing is not None:  # a re-run with the same run key starts clean
        uc.delete(fqn, etag=existing.etag)
    uc.create(schema, name)
    for rel, data in sorted(files.items()):
        uc.upload_file(fqn, rel, data)
    info = uc.finalize(fqn)  # agents load un-finalized content, so finalize before the eval
    return StagedSkill(
        name=name,
        dept=dept,
        staged_fqn=fqn,
        production_fqn=live_fqn(cfg, dept, name),
        content_hash=content_hash(files),
        etag=info.etag,
        origin=origin,
    )


def _stage_version(
    cfg: HubConfig, uc: UCSkills, journal: Journal, schema: str, name: str, home: str, n: int
) -> StagedSkill:
    """Copy a registered version (<d>_versions.<skill>-v<N>) into the run schema."""
    vfqn = version_fqn(cfg, home, name, n)
    row = next(r for r in journal.history(home, name) if r.action == "published" and r.version == n)
    files = uc.read_files(vfqn)
    if content_hash(files) != row.content_hash:
        raise StageError(f"{vfqn} no longer matches its publish record")
    return _stage_one(cfg, uc, schema, name, home, files, "version")


def _pin(cfg: HubConfig, ref: str | tuple[str, str]) -> tuple[str, str]:
    """(dept, name) of a pin given as `dept/name` or a tuple. A bare name is refused: it could resolve to
    another department's version."""
    if isinstance(ref, tuple):
        dept, name = ref
    else:
        dept, sep, name = ref.partition("/")
        if not sep:
            raise StageError(f"pinned version {ref!r} must name its department: use dept/name")
    if dept not in cfg.departments:
        raise StageError(f"pinned version {dept}/{name}: unknown department {dept!r}")
    try:
        live_fqn(cfg, dept, name)  # validates the skill name
    except ValueError as e:
        raise StageError(str(e)) from None
    return dept, name


def stage(
    cfg: HubConfig,
    uc: UCSkills,
    journal: Journal,
    run_key: str,
    candidates: list[SkillBundle],
    *,
    scope_depts: list[str] | None = None,
    versions: Mapping[str | tuple[str, str], int] | None = None,
) -> list[RunScope]:
    """Create one run schema per department scope with the skills under test.

    `versions` pins `dept/name` (or `(dept, name)`) to a published version of that department only.
    Precedence for a skill name: candidate, then registered version, then live copy; within a scope the
    department's own copy comes before common's.
    """
    unknown = sorted(set(scope_depts or []) - set(cfg.departments))
    unknown += sorted({b.dept for b in candidates} - set(cfg.departments))
    if unknown:
        raise StageError(f"unknown department(s): {', '.join(unknown)}")
    for b in candidates:
        assert_clean(b)  # refuse before anything is written
    # Resolve every requested version before any write, in its own department only.
    pins: dict[tuple[str, str], int] = {}
    for ref, n in (versions or {}).items():
        dept, name = _pin(cfg, ref)
        if (dept, name) in pins and pins[(dept, name)] != n:
            raise StageError(f"{dept}/{name} is pinned to two versions")
        if not any(r.action == "published" and r.version == n for r in journal.history(dept, name)):
            raise StageError(f"{dept}/{name} version {n} is not a published version of {dept}")
        pins[(dept, name)] = n
    depts = sorted({b.dept for b in candidates} | set(scope_depts or []) | {d for d, _ in pins})
    scopes: list[RunScope] = []
    for dept in depts:
        visible = ["common"] if dept == "common" else [dept, "common"]
        schema = run_schema(cfg, run_key, dept)
        uc.ensure_schema(schema)
        skills: list[StagedSkill] = []
        seen: set[str] = set()
        for b in sorted((b for b in candidates if b.dept in visible), key=lambda b: (b.dept, b.name)):
            if b.name in seen:
                raise StageError(f"skill {b.name!r} is a candidate in more than one visible department")
            seen.add(b.name)
            skills.append(_stage_one(cfg, uc, schema, b.name, b.dept, b.files, "candidate"))
        for d in visible:  # the department's own pin before common's
            for (home, name), n in sorted(pins.items()):
                if home == d and name not in seen:
                    seen.add(name)
                    skills.append(_stage_version(cfg, uc, journal, schema, name, home, n))
        for d in visible:
            for name in journal.skills(d):
                state = journal.live_state(d, name)
                if name in seen or state is None or state.action != "switched":
                    continue
                files = uc.read_files(state.versioned_fqn)  # copied from the registered version
                if content_hash(files) != state.content_hash:
                    raise StageError(f"{state.versioned_fqn} no longer matches its publish record")
                seen.add(name)
                skills.append(_stage_one(cfg, uc, schema, name, d, files, "live"))
        scopes.append(RunScope(dept=dept, schema_fqn=schema, skills=skills))
    return scopes


CANDIDATE_MAX_AGE_HOURS = 168
# CI writes the upload time (Unix seconds) to <candidate>/.uploaded_at. The Workspace API gives directories no
# timestamps, so a folder is dated by this marker, else by the newest file in it.
CANDIDATE_MARKER = ".uploaded_at"
_CANDIDATE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_FUTURE_SLACK_MS = 24 * 3600 * 1000  # a marker further ahead than this is not trusted


def _drop_schema(cfg: HubConfig, w, full: str) -> None:
    """Delete one schema, and only if it is a run schema in the hub catalog. Never touches production."""
    cat, _, name = full.partition(".")
    if cat != cfg.catalog or not RUN_SCHEMA_RE.fullmatch(name):
        raise StageError(f"refusing to drop {full!r}: not a run schema of {cfg.catalog}")
    w.schemas.delete(full, force=True)


def drop_run_schemas(cfg: HubConfig, w, run_key: str) -> None:
    for dept in cfg.departments:
        try:
            _drop_schema(cfg, w, run_schema(cfg, run_key, dept))
        except NotFound:
            pass


def _marker_ms(w, folder: str, now_ms: float) -> int | None:
    """The upload time from the marker, in epoch ms. None when it is absent, unreadable or in the future."""
    try:
        text = w.workspace.download(f"{folder}/{CANDIDATE_MARKER}").read().decode("utf-8", "replace").strip()
    except NotFound:
        return None
    if not text.isdecimal() or len(text) > 12:
        _log.warning("ignoring the malformed upload marker in %s", folder)
        return None
    ms = int(text) * 1000
    return ms if ms <= now_ms + _FUTURE_SLACK_MS else None


def _newest_file_ms(w, folder: str) -> int | None:
    stamps = [e.modified_at for e in w.workspace.list(folder, recursive=True) if e.modified_at]
    return max(stamps) if stamps else None


def _uploaded_ms(w, folder: str, now_ms: float) -> int | None:
    return _marker_ms(w, folder, now_ms) or _newest_file_ms(w, folder)


def _stale_candidate_paths(cfg: HubConfig, w, max_age_hours: float) -> list[str]:
    """Direct child folders of this catalog's candidates root (naming.candidates_root) uploaded longer ago
    than the limit. Anything else is skipped."""
    now_ms = time.time() * 1000
    cutoff_ms = now_ms - max_age_hours * 3600 * 1000
    root = candidates_root(cfg)
    root_aliases = {root, root.removeprefix("/Workspace")}
    try:
        entries = list(w.workspace.list(root))
    except NotFound:
        return []
    out: list[str] = []
    for e in entries:
        parent, _, name = (e.path or "").rpartition("/")
        if e.object_type != ObjectType.DIRECTORY or parent not in root_aliases:
            continue  # files, repos, notebooks, and anything not directly under the prefix
        if not _CANDIDATE_NAME.fullmatch(name) or set(name) == {"."} or ".." in name:
            continue
        folder = f"{root}/{name}"
        stamp = _uploaded_ms(w, folder, now_ms)
        if stamp is None:
            _log.warning("kept %s: it has no upload marker and no dated file", folder)
            continue
        if stamp < cutoff_ms:
            out.append(folder)
    return out


def cleanup_stale(
    cfg: HubConfig,
    w,
    older_than_hours: int = 24,
    *,
    candidate_max_age_hours: int = CANDIDATE_MAX_AGE_HOURS,
) -> list[str]:
    """Drop run_* schemas older than `older_than_hours` and candidate folders older than 168 hours.

    Returns the schemas and folders removed. Only run schemas this principal owns and direct children of this
    catalog's candidates root are touched: a shared catalog may hold other schemas that happen to match the name
    (M3), and dev and prod keep their candidates apart.
    """
    cutoff_ms = time.time() * 1000 - older_than_hours * 3600 * 1000
    me = w.current_user.me().user_name
    dropped: list[str] = []
    for s in w.schemas.list(catalog_name=cfg.catalog):
        if not RUN_SCHEMA_RE.fullmatch(s.name or "") or getattr(s, "owner", None) != me:
            continue
        if s.created_at is None or s.created_at >= cutoff_ms:
            continue
        full = f"{cfg.catalog}.{s.name}"
        try:
            _drop_schema(cfg, w, full)
        except NotFound:
            continue  # someone else dropped it first: not an error, and the rest still run
        except PermissionDenied as e:
            _log.warning("could not drop %s (%s); the rest of the cleanup continues", full, e)
            continue
        dropped.append(full)
    for path in _stale_candidate_paths(cfg, w, candidate_max_age_hours):
        try:
            w.workspace.delete(path, recursive=True)
        except NotFound:
            continue
        dropped.append(path)
    return dropped
