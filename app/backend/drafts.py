"""Skill Hub drafts: a skill uploaded in the Hub, to evaluate it before any pull request. A draft is never published:
only the merge eval releases content (skillhub.registry.versions.MERGE_TRIGGER_SOURCE).

An upload is a zip of the skill folder, or the folder's files. It needs SKILL.md with frontmatter (name, description)
and evals/evals.json; evals/rubric.json is optional, and without it a draft runs trigger mode only. A top-level
fixtures/ folder is stored beside the skill and replaces the default fixtures for the draft's evals.

Validation runs before anything is stored: safe relative paths only (no absolute paths, no `..`, no backslashes or
drive letters), no symbolic links or other special entries, at most MAX_FILES files and MAX_TOTAL_BYTES bytes once
unpacked (read with a bound, so a header that lies about its size cannot get past it). The skill is then checked as
`skillhub lint` checks a bundle (bundle loading, lint_bundle and thresholds that would loosen the gate).

Storage, as the app principal: `<workspace_root>/<catalog>/drafts/draft_<id>/` holds `.uploaded_at` (Unix seconds, so
the nightly cleanup removes the draft after 168 hours), `skills/<dept>/<skill>/...`, `fixtures/...` and, written last,
`draft.json` (who uploaded it). Bootstrap gives the app CAN_MANAGE on `drafts/` and nowhere else.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import logging
import re
import secrets
import stat
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import DatabricksError
from databricks.sdk.service.workspace import ImportFormat

from backend import candidates
from skillhub.bundle import BundleError, content_hash, is_ignored, load_bundle, parse_frontmatter
from skillhub.config import HubConfig
from skillhub.evalspec import EvalFile, Rubric, loosening_thresholds
from skillhub.lint import lint_bundle
from skillhub.registry.stage import CANDIDATE_MARKER

log = logging.getLogger("skillhub.app.drafts")

MAX_REQUEST_BYTES = 8 * 1024 * 1024  # the JSON body, base64 included
MAX_ARCHIVE_BYTES = 4 * 1024 * 1024  # the zip itself
MAX_FILES = 200
MAX_TOTAL_BYTES = (
    4 * 1024 * 1024
)  # every file once unpacked: the skill (lint allows 1 MiB), evals/ and fixtures/
MAX_PATH = 400
FIXTURES = "fixtures/"
_ARCHIVE_EXTRAS = ("__MACOSX/",)  # what macOS adds to a zip; never part of the skill
_UNSAFE = re.compile(r"[\\:\x00-\x1f\x7f]")


class DraftInvalid(Exception):
    """The upload is not a valid skill. `errors` says why, one plain sentence each."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


class DraftTooLarge(Exception):
    """More files or bytes than a draft may have."""


class DraftStoreFailed(Exception):
    """The draft could not be written to the drafts folder."""


@dataclass
class Draft:
    dept: str
    name: str
    description: str
    files: dict[str, bytes]  # the skill folder, evals/ included
    fixtures: dict[str, bytes] = field(default_factory=dict)

    @property
    def has_rubric(self) -> bool:
        return "evals/rubric.json" in self.files


def new_id(now: float | None = None) -> str:
    """8 hex digits of the upload time, so drafts sort by age, then 16 random hex digits, so none can be guessed."""
    return f"{int(time.time() if now is None else now) & 0xFFFFFFFF:08x}{secrets.token_hex(8)}"


def _check_path(path: object) -> str:
    if not isinstance(path, str) or not path or len(path) > MAX_PATH:
        raise ValueError("a path must be 1 to 400 characters")
    if path.startswith("/") or _UNSAFE.search(path):
        raise ValueError("a path must be relative, with / between names")
    if any(part in ("", ".", "..") for part in path.split("/")):
        raise ValueError("a path must not have empty, . or .. parts")
    return path


def _add(out: dict[str, bytes], errors: list[str], path: str, read, total: list[int]) -> None:
    """Check one file's path and read it within what is left of the size budget."""
    try:
        rel = _check_path(path)
    except ValueError as e:
        errors.append(f"{path!r}: {e}")
        return
    if is_ignored(rel):
        return
    if rel in out:
        errors.append(f"{rel}: listed twice")
        return
    data = read(MAX_TOTAL_BYTES - total[0] + 1)
    total[0] += len(data)
    if total[0] > MAX_TOTAL_BYTES:
        raise DraftTooLarge(f"more than {MAX_TOTAL_BYTES} bytes")
    out[rel] = data
    if len(out) > MAX_FILES:
        raise DraftTooLarge(f"more than {MAX_FILES} files")


def unzip(archive: bytes) -> dict[str, bytes]:
    """The archive's regular files by path. Refuses links, special entries, encrypted entries and unsafe paths."""
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise DraftTooLarge("the archive is too large")
    try:
        zf = zipfile.ZipFile(io.BytesIO(archive))
    except (zipfile.BadZipFile, ValueError):
        raise DraftInvalid(["the file is not a zip archive"]) from None
    out: dict[str, bytes] = {}
    errors: list[str] = []
    total = [0]
    with zf:
        infos = zf.infolist()
        if len(infos) > 4 * MAX_FILES:
            raise DraftTooLarge("too many archive entries")
        for info in infos:
            name = info.filename
            if name.startswith(_ARCHIVE_EXTRAS):
                continue
            kind = stat.S_IFMT(info.external_attr >> 16)
            if kind == stat.S_IFLNK:
                errors.append(f"{name}: symbolic links are not allowed")
                continue
            if info.is_dir():
                try:
                    _check_path(name.rstrip("/"))
                except ValueError as e:
                    errors.append(f"{name!r}: {e}")
                continue
            if kind not in (0, stat.S_IFREG):
                errors.append(f"{name}: only regular files are allowed")
                continue
            if info.flag_bits & 0x1:
                errors.append(f"{name}: encrypted files are not allowed")
                continue
            try:
                with zf.open(info) as fh:
                    _add(out, errors, name, fh.read, total)
            except (zipfile.BadZipFile, NotImplementedError, EOFError, OSError):
                errors.append(f"{name}: the archive entry cannot be read")
    if errors:
        raise DraftInvalid(errors)
    return out


def from_files(files: list[tuple[str, str]]) -> dict[str, bytes]:
    """Files sent one by one as (path, base64 content), for example a folder picked in the browser."""
    if len(files) > MAX_FILES:
        raise DraftTooLarge(f"more than {MAX_FILES} files")
    out: dict[str, bytes] = {}
    errors: list[str] = []
    total = [0]
    for path, content in files:
        try:
            data = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError):
            errors.append(f"{path!r}: the content is not base64")
            continue
        _add(out, errors, path, lambda n, data=data: data[:n], total)
    if errors:
        raise DraftInvalid(errors)
    return out


def _skill_root(files: dict[str, bytes]) -> tuple[str | None, dict[str, bytes]]:
    """(folder name, files relative to the skill folder): SKILL.md at the top, or under one top folder."""
    if "SKILL.md" in files:
        return None, files
    tops = {p.split("/", 1)[0] for p in files}
    if len(tops) == 1:
        top = tops.pop()
        if f"{top}/SKILL.md" in files and all("/" in p for p in files):
            return top, {p[len(top) + 1 :]: d for p, d in files.items()}
    raise DraftInvalid(["SKILL.md is missing: put it at the top of the archive, or of one folder in it"])


def build(cfg: HubConfig, dept: str, files: dict[str, bytes]) -> Draft:
    """Check the upload as `skillhub lint` checks a bundle. Raises DraftInvalid with every problem found."""
    if dept not in cfg.departments:
        raise DraftInvalid([f"unknown department {dept!r}"])
    folder_name, files = _skill_root(files)
    fixtures = {p[len(FIXTURES) :]: d for p, d in files.items() if p.startswith(FIXTURES)}
    skill = {p: d for p, d in files.items() if not p.startswith(FIXTURES)}
    if "evals/evals.json" not in skill:
        raise DraftInvalid(["evals/evals.json is missing: a draft needs its test cases"])
    try:
        frontmatter = parse_frontmatter(skill["SKILL.md"])
    except BundleError as e:
        raise DraftInvalid([f"SKILL.md: {e}"]) from None
    name = folder_name or frontmatter.get("name", "")
    if not candidates.valid_skill_name(name):
        raise DraftInvalid([f"the skill name {name!r} must match ^[a-z0-9]+(?:-[a-z0-9]+)*$"])
    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="skillhub-draft-") as tmp:
        root = Path(tmp) / "skills" / dept / name
        for rel, data in skill.items():  # every path was checked: relative, no . or .. parts
            target = (root / rel).resolve()
            if not target.is_relative_to(root.resolve()):
                raise DraftInvalid([f"{rel!r}: a path must stay inside the skill folder"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        try:
            bundle = load_bundle(root, dept)
        except BundleError as e:
            raise DraftInvalid([str(e).replace(f"{root}: ", "").replace(str(root), name)]) from None
        errors += [i.message for i in lint_bundle(bundle)]
    evals = rubric = None
    try:
        evals = EvalFile.model_validate_json(skill["evals/evals.json"])
        if "evals/rubric.json" in skill:
            rubric = Rubric.model_validate_json(skill["evals/rubric.json"])
    except ValueError:
        pass  # lint_bundle has reported it
    if evals is not None and not evals.evals:
        errors.append("evals/evals.json has no cases")
    if evals is not None:
        errors += [
            f"{msg}: a skill may only tighten the gate"
            for msg in loosening_thresholds(cfg.defaults, evals.criteria, rubric)
        ]
    if errors:
        raise DraftInvalid(errors)
    return Draft(dept, name, bundle.description, skill, fixtures)


def store(
    app_w: WorkspaceClient, cfg: HubConfig, draft: Draft, uploaded_by: str, pool: ThreadPoolExecutor
) -> candidates.CandidateKey:
    """Write the draft as the app principal. The marker goes first, so even a half-written draft is dated for the
    cleanup; draft.json goes last, so the Skill Hub lists only complete drafts. A failure removes what was written."""
    now = int(time.time())
    key = candidates.parse_key(f"draft_{new_id(now)}")
    assert key is not None
    base = candidates.folder(cfg, key)
    skill = f"skills/{draft.dept}/{draft.name}"
    files = {f"{skill}/{rel}": data for rel, data in draft.files.items()}
    files.update({f"fixtures/{rel}": data for rel, data in draft.fixtures.items()})
    meta = {
        "run_key": key.run_key,
        "dept": draft.dept,
        "skill": draft.name,
        "uploaded_by": uploaded_by,
        "uploaded_at": now,
        "description": draft.description,
        "has_rubric": draft.has_rubric,
        "has_fixtures": bool(draft.fixtures),
        "content_hash": content_hash(draft.files),
    }

    def put(rel: str, data: bytes) -> None:
        app_w.workspace.upload(f"{base}/{rel}", data, format=ImportFormat.RAW, overwrite=False)

    try:
        app_w.workspace.mkdirs(base)
        put(CANDIDATE_MARKER, f"{now}\n".encode())
        for folder in sorted({f"{base}/{rel.rsplit('/', 1)[0]}" for rel in files}):
            app_w.workspace.mkdirs(folder)
        for upload in [pool.submit(put, rel, data) for rel, data in sorted(files.items())]:
            upload.result()
        put(candidates.DRAFT_META, json.dumps(meta, indent=2).encode())
    except DatabricksError as exc:
        log.warning("could not store a draft: %s", type(exc).__name__)
        try:
            app_w.workspace.delete(base, recursive=True)
        except DatabricksError:
            pass  # the nightly cleanup removes it
        raise DraftStoreFailed(type(exc).__name__) from None
    return key
