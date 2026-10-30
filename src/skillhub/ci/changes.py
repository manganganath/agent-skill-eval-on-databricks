"""Skills changed between two git refs, including deleted skills."""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal

import yaml

from skillhub import SkillHubError

Status = Literal["added", "modified", "deleted", "renamed"]
SKILL_ROOT = "skills"
CONFIG_REL = "config/hub.yaml"  # read at both refs; it names the quarantined departments
_TRUE = {"true", "yes", "on", "y", "t", "1"}  # what the config model accepts as True


@dataclass(frozen=True, order=True)
class ChangedSkill:
    dept: str
    skill: str
    status: Status
    needs_cases: bool = False  # no evals/evals.json with a trigger case at head
    needs_rubric: bool = False  # no evals/rubric.json at head
    # Its files come from a skill in a quarantined department, and it is not in one. CI refuses it.
    moved_from_quarantine: bool = False
    # Its department is quarantined. Pull requests evaluate it; the release never publishes or retires it.
    quarantined: bool = False

    @property
    def needs_generation(self) -> bool:
        return self.needs_cases or self.needs_rubric

    def as_dict(self) -> dict[str, str | bool]:
        return {**asdict(self), "needs_generation": self.needs_generation}


class GitRefError(SkillHubError):
    """A git ref could not be resolved, or git failed."""


def _git(repo: Path, *args: str) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
        ).stdout
    except subprocess.CalledProcessError as e:
        raise GitRefError(f"git {args[0]} failed: {(e.stderr or '').strip()}") from e


def _resolve(repo: Path, ref: str) -> str:
    """Resolve a ref to a commit SHA. Refs starting with `-` are never passed to git."""
    if ref.startswith("-"):
        raise GitRefError(f"invalid git ref {ref!r}: must not start with '-'")
    return _git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").strip()


def _exists(repo: Path, sha: str, rel: str) -> bool:
    proc = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{sha}:{rel}"], capture_output=True)
    return proc.returncode == 0


def _blob(repo: Path, sha: str, rel: str) -> str | None:
    """File text at `sha`, None when absent. A file that isn't valid UTF-8 reads as "" (present, unparseable)."""
    proc = subprocess.run(["git", "-C", str(repo), "show", f"{sha}:{rel}"], capture_output=True)
    if proc.returncode != 0:
        return None
    try:
        return proc.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def _needs(repo: Path, sha: str, dept: str, skill: str) -> tuple[bool, bool]:
    """(needs_cases, needs_rubric) at `sha`.

    Unparseable files don't count as missing: lint reports them, and `generate` must not overwrite them.
    """
    base = f"{SKILL_ROOT}/{dept}/{skill}/evals"
    needs_rubric = _blob(repo, sha, f"{base}/rubric.json") is None
    text = _blob(repo, sha, f"{base}/evals.json")
    if text is None:
        return True, needs_rubric
    try:
        cases = json.loads(text).get("evals") or []
        has_trigger = any(isinstance(c, dict) and c.get("kind", "trigger") == "trigger" for c in cases)
    except (ValueError, AttributeError):
        return False, needs_rubric
    return not has_trigger, needs_rubric


def _skill_of(path: str) -> tuple[str, str] | None:
    parts = path.split("/")
    return (parts[1], parts[2]) if len(parts) >= 4 and parts[0] == SKILL_ROOT else None


def _quarantined(repo: Path, sha: str) -> set[str]:
    """Departments with `quarantine: true` in config/hub.yaml at `sha`. A missing file names none."""
    text = _blob(repo, sha, CONFIG_REL)
    if text is None:
        return set()
    try:
        departments = (yaml.safe_load(text) or {}).get("departments") or {}
    except (yaml.YAMLError, AttributeError) as e:
        raise GitRefError(f"{CONFIG_REL} at {sha[:12]} is not a valid config: {type(e).__name__}") from None
    return {
        str(d)
        for d, v in departments.items()
        if isinstance(v, dict) and str(v.get("quarantine", "")).strip().lower() in _TRUE
    }


def _touched(name_status_z: str) -> tuple[set[tuple[str, str]], dict[tuple[str, str], set[tuple[str, str]]]]:
    """Skill folders touched, and for each new side of a rename the skills its files came from
    (`-z` output: code, path[, path] NUL-separated)."""
    touched: set[tuple[str, str]] = set()
    renamed_from: dict[tuple[str, str], set[tuple[str, str]]] = {}
    tokens = [t for t in name_status_z.split("\0")]
    i = 0
    while i < len(tokens) and tokens[i]:
        code = tokens[i]
        n = 2 if code[0] in "RC" else 1
        paths = tokens[i + 1 : i + 1 + n]
        i += 1 + n
        touched.update(s for s in (_skill_of(p) for p in paths) if s is not None)
        if code.startswith("R") and len(paths) == 2:
            old, new = _skill_of(paths[0]), _skill_of(paths[1])
            if old is not None and new is not None and old != new:
                renamed_from.setdefault(new, set()).add(old)
    return touched, renamed_from


def _empty_tree(repo: Path) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), "hash-object", "-t", "tree", "--stdin"],
            check=True, capture_output=True, input=b"",
        ).stdout.decode().strip()  # fmt: skip
    except subprocess.CalledProcessError as e:
        raise GitRefError(f"git hash-object failed: {e.stderr.decode(errors='replace').strip()}") from e


def changed_skills(base_ref: str | None, head_ref: str, repo: Path = Path(".")) -> list[ChangedSkill]:
    """Skill folders touched between base and head. Status comes from SKILL.md on each side.

    A base of None is the empty tree: every skill at head is added. The publish plan uses it before the first
    release, when nothing is released yet.

    A rename that git can't detect (heavily edited files) shows as deleted plus added, which is still safe.

    moved_from_quarantine marks a skill outside the quarantined departments whose files git sees as
    renamed from a skill in one, or that is added under the name of a skill deleted from one in the same diff.
    A department counts as quarantined when config/hub.yaml says so at either ref, so a change that also
    clears the flag is still caught. quarantined marks every skill in such a department.
    """
    head_sha = _resolve(repo, head_ref)
    if base_ref is None:
        fork = _empty_tree(repo)
    else:
        fork = _git(repo, "merge-base", _resolve(repo, base_ref), head_sha).strip()
    diff = _git(repo, "diff", "--name-status", "-z", "--find-renames", fork, head_sha, "--", SKILL_ROOT)
    touched, renamed_from = _touched(diff)
    quarantined = _quarantined(repo, fork) | _quarantined(repo, head_sha)
    result: list[ChangedSkill] = []
    for dept, skill in sorted(touched):
        rel = f"{SKILL_ROOT}/{dept}/{skill}/SKILL.md"
        at_head = _exists(repo, head_sha, rel)
        at_base = _exists(repo, fork, rel)
        if at_head and at_base:
            status: Status = "modified"
        elif at_head:
            status = "renamed" if (dept, skill) in renamed_from else "added"
        elif at_base:
            status = "deleted"
        else:
            continue  # touched, but never a skill on either side
        cases, rubric = (False, False) if status == "deleted" else _needs(repo, head_sha, dept, skill)
        result.append(ChangedSkill(dept, skill, status, cases, rubric))
    deleted_quarantined = {s.skill for s in result if s.status == "deleted" and s.dept in quarantined}
    out: list[ChangedSkill] = []
    for c in result:
        moved = (
            c.status != "deleted"
            and c.dept not in quarantined
            and (
                any(d in quarantined for d, _ in renamed_from.get((c.dept, c.skill), ()))
                or (c.status == "added" and c.skill in deleted_quarantined)
            )
        )
        out.append(replace(c, moved_from_quarantine=moved, quarantined=c.dept in quarantined))
    return out
