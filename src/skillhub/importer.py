from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from skillhub import SkillHubError
from skillhub.bundle import BundleError, is_ignored, parse_frontmatter, skills_root
from skillhub.config import ImportSpec


class ImportFailed(SkillHubError):
    """The import would drop, duplicate or mis-place a skill, so nothing is written."""


_SHA = re.compile(r"^[0-9a-f]{40}$")
_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def parse_source(source: str) -> tuple[str, str]:
    if "@" not in source:
        raise ImportFailed("import.source must be github.com/<owner>/<repo>@<40-char sha>")
    repo, sha = source.rsplit("@", 1)
    if not _SHA.match(sha):
        raise ImportFailed(f"import.source must pin a full 40-character commit SHA, got {sha!r}")
    return repo, sha


def _matches(pattern: str, rel_dir: str) -> bool:
    """Segment-wise glob: 'plugins/partner-built/*' matches any path under one partner folder."""
    pat = pattern.strip("/").split("/")
    parts = rel_dir.split("/")
    return len(parts) >= len(pat) and all(fnmatch.fnmatchcase(p, q) for p, q in zip(parts, pat, strict=False))


def _rank(mapping: dict[str, str], rel_dir: str) -> tuple[int, str] | None:
    """(index, department) of the first map entry matching rel_dir, in config order."""
    for i, (pattern, dept) in enumerate(mapping.items()):
        if _matches(pattern, rel_dir):
            return i, dept
    return None


def department_for(mapping: dict[str, str], rel_dir: str) -> str | None:
    """First match in config order (dicts keep insertion order)."""
    hit = _rank(mapping, rel_dir)
    return hit[1] if hit else None


Files = dict[str, bytes]


def _collect(source: Path) -> dict[str, list[tuple[str, Files]]]:
    copies: dict[str, list[tuple[str, Files]]] = defaultdict(list)
    plugins = source / "plugins"
    if not plugins.is_dir():
        return copies
    for md in sorted(plugins.rglob("SKILL.md")):
        d = md.parent
        files: Files = {}
        if md.is_symlink():
            raise ImportFailed(f"{md.relative_to(source).as_posix()} is a symlink; symlinks are not imported")
        for p in sorted(d.rglob("*")):
            if p.is_symlink():
                raise ImportFailed(
                    f"{p.relative_to(source).as_posix()} is a symlink; symlinks are not imported"
                )
            if p.is_file():
                rel = p.relative_to(d).as_posix()
                if not is_ignored(rel):
                    files[rel] = p.read_bytes()
        rel_dir = d.relative_to(source).as_posix()
        try:
            name = parse_frontmatter(files["SKILL.md"]).get("name", "").strip()
        except BundleError as e:
            raise ImportFailed(f"{rel_dir}: {e}") from e
        if not name:
            raise ImportFailed(f"{rel_dir}: SKILL.md frontmatter has no name")
        copies[name].append((rel_dir, files))
    return copies


@dataclass
class ImportReport:
    source: str
    skills: dict[str, str] = field(default_factory=dict)  # name -> department
    origin: dict[str, str] = field(default_factory=dict)  # name -> source path it was copied from
    excluded: list[str] = field(default_factory=list)
    renamed: list[dict[str, str]] = field(default_factory=list)  # upstream folder differs from name
    license_text: str = ""
    notice_text: str = ""

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for dept in self.skills.values():
            out[dept] = out.get(dept, 0) + 1
        return dict(sorted(out.items()))

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "total": len(self.skills),
            "by_department": self.counts(),
            "excluded": sorted(self.excluded),
            "renamed": sorted(self.renamed, key=lambda r: r["to"]),
        }


def _license_id(text: str) -> str | None:
    """The SPDX id of a well-known licence text, else None: the notice never names a licence it did not see."""
    head = " ".join(text.split())[:2000]
    if "Apache License" in head and "Version 2.0" in head:
        return "Apache-2.0"
    if head.startswith("MIT License") or "Permission is hereby granted, free of charge" in head:
        return "MIT"
    return None


NOTICE_START = "<!-- skillhub-import:start -->"
NOTICE_END = "<!-- skillhub-import:end -->"


def write_notices(path: Path, spec: ImportSpec, report: ImportReport) -> None:
    """Rewrite only the generated block; text outside the markers is kept."""
    repo, sha = parse_source(spec.source)
    spdx = _license_id(report.license_text)
    lines = [
        NOTICE_START,
        "Imported skills are derived from " + spec.source + (f" ({spdx})." if spdx else "."),
        f"Source: https://{repo}",
        f"Commit: {sha}",
        "",
        "Skills imported:",
    ]
    for name in sorted(report.skills):
        lines.append(f"- {report.skills[name]}/{name} (from {report.origin[name]})")
    if report.renamed:
        lines += ["", "Imported under its declared name (upstream folder differs):"]
        for r in sorted(report.renamed, key=lambda r: r["to"]):
            lines.append(f"- {r['from']} -> {r['to']} (from {r['path']})")
    lines += ["", "Source LICENSE:", "", report.license_text.rstrip()]
    if report.notice_text.strip():
        lines += ["", "Source NOTICE:", "", report.notice_text.rstrip()]
    lines.append(NOTICE_END)
    block = "\n".join(lines)
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    i, j = old.find(NOTICE_START), old.find(NOTICE_END)
    if i != -1 and j > i:
        new = old[:i] + block + old[j + len(NOTICE_END) :]
    elif old.strip():
        new = old.rstrip("\n") + "\n\n" + block + "\n"
    else:
        new = "THIRD-PARTY NOTICES\n\n" + block + "\n"
    path.write_text(new, encoding="utf-8")


def _read_first(source_dir: Path, names: tuple[str, ...]) -> str | None:
    for n in names:
        p = source_dir / n
        if p.is_file() and not p.is_symlink():
            return p.read_text(encoding="utf-8")
    return None


def _write_skill(target: Path, files: Files) -> None:
    """Replace source-owned files only; anything under evals/ is local work and is never touched."""
    if target.exists():
        for p in sorted(target.rglob("*"), reverse=True):
            rel = p.relative_to(target).as_posix()
            if rel == "evals" or rel.startswith("evals/"):
                continue
            if p.is_file() or p.is_symlink():
                if rel not in files:
                    p.unlink()
            elif p.is_dir() and not any(p.iterdir()):
                p.rmdir()
    for rel, data in files.items():
        p = target / rel
        if (rel == "evals" or rel.startswith("evals/")) and p.exists():
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def import_skills(
    spec: ImportSpec,
    source_dir: Path,
    dest: Path | None = None,
    departments: Iterable[str] | None = None,
) -> ImportReport:
    dest = dest or skills_root()
    allowed = set(departments) if departments is not None else None
    parse_source(spec.source)
    # Pass 1: resolve and check everything. Nothing is written if any check fails.
    if allowed is not None:
        for pattern, dept in spec.map.items():
            if dept not in allowed:
                raise ImportFailed(f"import.map {pattern!r} names unknown department {dept!r}")
    license_text = _read_first(source_dir, ("LICENSE", "LICENSE.md", "LICENSE.txt"))
    if license_text is None:
        raise ImportFailed("the source tree has no LICENSE file; it is needed for THIRD_PARTY_NOTICES")
    notice_text = _read_first(source_dir, ("NOTICE", "NOTICE.md", "NOTICE.txt")) or ""
    copies = _collect(source_dir)
    report = ImportReport(source=spec.source, license_text=license_text, notice_text=notice_text)
    plan: list[tuple[Path, Files]] = []
    for name in sorted(copies):
        # exclude matches either the declared name or the upstream folder name
        folders = [r.rsplit("/", 1)[-1] for r, _ in copies[name]]
        hit = [x for x in [name, *folders] if x in spec.exclude]
        entries = [
            (r, f)
            for r, f in copies[name]
            if name not in spec.exclude and r.rsplit("/", 1)[-1] not in spec.exclude
        ]
        if not entries:
            report.excluded.append(hit[0])
            continue
        if not _NAME.match(name):
            raise ImportFailed(f"{name!r} is not a valid skill name (lowercase letters, digits, hyphens)")
        first_rel, first_files = entries[0]
        for rel, files in entries[1:]:
            if files != first_files:
                raise ImportFailed(f"{name}: copies at {first_rel} and {rel} are not byte-identical")
        # Copies are identical, so the copy matching the earliest map entry decides the department.
        ranked = [(hit[0], hit[1], rel) for rel, _ in entries if (hit := _rank(spec.map, rel))]
        if not ranked:
            paths = ", ".join(rel for rel, _ in entries)
            raise ImportFailed(f"{name}: found only under paths the import map does not cover ({paths})")
        _, dept, src_rel = min(ranked, key=lambda r: (r[0], r[2]))
        plan.append((dest / dept / name, next(f for rel, f in entries if rel == src_rel)))
        report.skills[name] = dept
        report.origin[name] = src_rel
        folder = src_rel.rsplit("/", 1)[-1]
        if folder != name:
            report.renamed.append({"from": folder, "to": name, "path": src_rel})
    # Pass 2: write.
    for target, files in plan:
        _write_skill(target, files)
    write_notices(dest.parent / "THIRD_PARTY_NOTICES", spec, report)
    return report


_GIT = ["git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.ext.allow=never"]


def _git(*args: str, cwd: Path | None = None) -> str:
    cmd = [*_GIT, *(["-C", str(cwd)] if cwd else []), *args]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        out = subprocess.run(cmd, check=True, capture_output=True, text=True, env=env)
    except FileNotFoundError as e:
        raise ImportFailed(f"git is not available: {e}") from e
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or "").strip().replace("\n", " ") or f"exit {e.returncode}"
        raise ImportFailed(f"git {args[0]} failed: {detail}") from e
    return out.stdout.strip()


@contextmanager
def clone_source(source: str, workdir: Path | None = None) -> Iterator[Path]:
    """Fetch exactly the pinned commit (depth 1), verify its SHA, yield the tree, then delete it."""
    repo, sha = parse_source(source)
    own = workdir is None
    workdir = workdir or Path(tempfile.mkdtemp(prefix="skillhub-src-"))
    try:
        _git("init", "-q", str(workdir))
        _git("remote", "add", "origin", f"https://{repo}.git", cwd=workdir)
        _git("fetch", "--depth", "1", "--no-recurse-submodules", "origin", sha, cwd=workdir)
        _git("checkout", "-q", "--no-recurse-submodules", "FETCH_HEAD", cwd=workdir)
        head = _git("rev-parse", "HEAD", cwd=workdir)
        if head != sha:
            raise ImportFailed(f"fetched {head}, expected pinned commit {sha}")
        yield workdir
    finally:
        if own:
            shutil.rmtree(workdir, ignore_errors=True)
