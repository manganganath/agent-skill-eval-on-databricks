from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from skillhub import SkillHubError


class BundleError(SkillHubError):
    """A skill folder is missing SKILL.md or its frontmatter cannot be read."""


_IGNORED = {".DS_Store", "__pycache__"}
_FM = re.compile(r"\A---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)


def is_ignored(rel: str) -> bool:
    return any(part in _IGNORED or part.endswith(".pyc") for part in rel.split("/"))


def skills_root() -> Path:
    """The skills directory: $SKILLHUB_SKILLS, else ./skills."""
    return Path(os.environ.get("SKILLHUB_SKILLS", "skills"))


_default_skills_root = skills_root  # alias: discover_bundles' parameter shadows the function name


@dataclass(frozen=True)
class SkillBundle:
    dept: str
    name: str
    root: Path
    files: dict[str, bytes]  # rel posix path -> bytes, EXCLUDING evals/**
    frontmatter: dict[str, str]

    @property
    def description(self) -> str:
        return self.frontmatter.get("description", "")


def parse_frontmatter(data: bytes) -> dict[str, str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise BundleError(f"SKILL.md is not valid UTF-8: {e}") from e
    m = _FM.match(text)
    if not m:
        raise BundleError("SKILL.md has no YAML frontmatter")
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise BundleError(f"SKILL.md frontmatter is not valid YAML: {e}") from e
    if not isinstance(meta, dict):
        raise BundleError("SKILL.md frontmatter must be a mapping")
    return {str(k): "" if v is None else str(v) for k, v in meta.items()}


def load_bundle(root: Path, dept: str) -> SkillBundle:
    if not (root / "SKILL.md").is_file():
        raise BundleError(f"{root}: missing SKILL.md")
    files: dict[str, bytes] = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_symlink() and not is_ignored(rel):
            raise BundleError(f"{root}: {rel} is a symlink; bundles may not link outside themselves")
        if not p.is_file():
            continue
        if rel.startswith("evals/") or is_ignored(rel):
            continue
        files[rel] = p.read_bytes()
    return SkillBundle(
        dept=dept,
        name=root.name,
        root=root,
        files=files,
        frontmatter=parse_frontmatter(files["SKILL.md"]),
    )


def check_names(dept: str, name: str) -> None:
    """A department or skill folder name that is unsafe in SQL, a REST path or a file path is refused
    . load_bundle does not check, so lint can report every problem in one pass."""
    from skillhub.naming import check_dept, check_skill_name

    try:
        check_dept(dept)
        check_skill_name(name)
    except ValueError as e:
        raise BundleError(f"{dept}/{name}: {e}") from None


def discover_bundles(skills_root: Path | None = None) -> list[SkillBundle]:
    """All bundles under skills_root; default is the module-level skills_root(). Refuses unsafe names."""
    skills_root = skills_root or _default_skills_root()
    if not skills_root.is_dir():
        return []
    found: list[SkillBundle] = []
    for dept_dir in sorted(p for p in skills_root.iterdir() if p.is_dir()):
        for skill_dir in sorted(p for p in dept_dir.iterdir() if p.is_dir() and (p / "SKILL.md").is_file()):
            check_names(dept_dir.name, skill_dir.name)
            found.append(load_bundle(skill_dir, dept_dir.name))
    return sorted(found, key=lambda b: (b.dept, b.name))


class SkillRefError(BundleError):
    """A skill reference names no skill, or a bare name that more than one department has."""


def find_bundle(bundles: list[SkillBundle], ref: str) -> SkillBundle:
    """The bundle a `dept/name` or bare `name` reference names."""
    dept, _, name = ref.strip().rpartition("/")
    found = [b for b in bundles if b.name == name and (not dept or b.dept == dept)]
    if not found:
        raise SkillRefError(f"no skill named {ref!r}")
    if len(found) > 1:
        depts = ", ".join(sorted(b.dept for b in found))
        raise SkillRefError(f"{ref!r} exists in several departments ({depts}); use dept/name")
    return found[0]


def select_bundles(bundles: list[SkillBundle], refs: list[str]) -> list[SkillBundle]:
    """Resolve references in order, without repeats. The CLI and the eval runner both use this."""
    chosen: dict[tuple[str, str], SkillBundle] = {}
    for ref in refs:
        b = find_bundle(bundles, ref)
        chosen.setdefault((b.dept, b.name), b)
    return list(chosen.values())


def content_hash(files: Mapping[str, bytes]) -> str:
    h = hashlib.sha256()
    for path in sorted(p for p in files if not p.startswith("evals/")):
        data = files[path]
        h.update(path.encode("utf-8") + b"\0" + len(data).to_bytes(8, "big") + data)
    return "sha256:" + h.hexdigest()
