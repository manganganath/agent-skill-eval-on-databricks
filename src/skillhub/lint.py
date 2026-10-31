from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from skillhub import SkillHubError
from skillhub.bundle import BundleError, SkillBundle, load_bundle
from skillhub.bundle import skills_root as default_skills_root
from skillhub.evalspec import EFFECTIVENESS_FIRST_ID, load_evals, load_rubric, loosening_thresholds
from skillhub.gateway.skills_mcp import MAX_SKILL_TOOLS

NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_BUNDLE_BYTES = 1024 * 1024


class LintRefused(SkillHubError):
    """A bundle failed lint, so nothing is written to Unity Catalog."""


@dataclass(frozen=True)
class LintIssue:
    path: str
    message: str
    warning: bool = False  # reported by lint_repo, but never fails lint (lint_warnings)

    def __str__(self) -> str:
        return f"{self.path}: {'warning: ' if self.warning else ''}{self.message}"


def lint_bundle(b: SkillBundle) -> list[LintIssue]:
    path = b.root.as_posix()
    out: list[LintIssue] = []

    def bad(msg: str) -> None:
        out.append(LintIssue(path, msg))

    if not NAME_RE.fullmatch(b.name):
        bad(f"folder name {b.name!r} must match {NAME_RE.pattern}")
    fm_name = b.frontmatter.get("name", "")
    if not NAME_RE.fullmatch(fm_name):
        bad(f"frontmatter name {fm_name!r} must match {NAME_RE.pattern}")
    elif fm_name != b.name:
        bad(f"frontmatter name {fm_name!r} must equal folder name {b.name!r}")
    if not b.description.strip():
        bad("frontmatter description is empty")
    size = sum(len(d) for d in b.files.values())
    if size > MAX_BUNDLE_BYTES:
        bad(f"bundle is {size} bytes; the limit is {MAX_BUNDLE_BYTES}")

    evals = b.root / "evals" / "evals.json"
    if evals.is_file():
        try:
            ef = load_evals(evals)
        except ValueError as e:
            bad(f"evals/evals.json: {e}")
        else:
            if ef.skill_name != b.name:
                bad("evals/evals.json: skill_name must equal the folder name")
            for c in ef.evals:
                if c.kind == "trigger" and not 1 <= c.id < EFFECTIVENESS_FIRST_ID:
                    bad(
                        f"evals/evals.json: trigger case {c.id}: trigger ids are 1-{EFFECTIVENESS_FIRST_ID - 1}"
                    )
                elif c.kind == "effectiveness" and c.id < EFFECTIVENESS_FIRST_ID:
                    bad(f"evals/evals.json: effectiveness case {c.id}: ids start at {EFFECTIVENESS_FIRST_ID}")
    rubric = b.root / "evals" / "rubric.json"
    if rubric.is_file():
        try:
            r = load_rubric(rubric)
        except ValueError as e:
            bad(f"evals/rubric.json: {e}")
        else:
            if r.skill_name != b.name:
                bad("rubric skill_name must equal the folder name")
            ids = [c.id for c in r.criteria]
            if len(set(ids)) != len(ids):
                bad("rubric criterion ids must be unique")
    return out


def lint_warnings(b: SkillBundle) -> list[LintIssue]:
    """What lint reports without failing: a case that should not trigger the skill has no answer to judge, so
    its assertions are ignored. Only a trigger case that should fire is judged on its assertions."""
    evals = b.root / "evals" / "evals.json"
    if not evals.is_file():
        return []
    try:
        ef = load_evals(evals)
    except ValueError:
        return []  # already reported by lint_bundle
    return [
        LintIssue(
            b.root.as_posix(),
            f"evals/evals.json: case {c.id}: its assertions are ignored: a case with should_trigger "
            "false has no answer to judge",
            warning=True,
        )
        for c in ef.evals
        if c.kind == "trigger" and not c.should_trigger and c.assertions
    ]


def assert_clean(b: SkillBundle) -> None:
    issues = lint_bundle(b)
    if issues:
        raise LintRefused("; ".join(f"{i.path}: {i.message}" for i in issues))


def _near_miss_issues(b: SkillBundle, known: set[str]) -> list[LintIssue]:
    evals = b.root / "evals" / "evals.json"
    if not evals.is_file():
        return []
    try:
        ef = load_evals(evals)
    except ValueError:
        return []  # already reported by lint_bundle
    out: list[LintIssue] = []
    for c in ef.evals:
        if c.expected_skill is None:
            continue
        if c.expected_skill not in known:
            out.append(
                LintIssue(
                    b.root.as_posix(),
                    f"case {c.id}: expected_skill {c.expected_skill!r} is not a skill in the repo",
                )
            )
        elif not c.should_trigger and c.expected_skill == b.name:
            out.append(
                LintIssue(b.root.as_posix(), f"case {c.id}: a negative case cannot expect its own skill")
            )
    return out


def _select(
    bundles: list[SkillBundle], paths: Sequence[str], skills_root: Path
) -> tuple[list[SkillBundle], list[str], list[str]]:
    """Bundles that contain (or sit under) the given paths, plus given paths in the skills root that match none, plus given paths outside the root.

    Paths are resolved, so relative paths, `..` and symlinks compare correctly.
    """
    if not paths:
        return bundles, [], []
    root = skills_root.resolve()
    selected: dict[Path, SkillBundle] = {}
    unknown: list[str] = []
    outside: list[str] = []
    for raw in paths:
        p = Path(raw).resolve()
        if not p.is_relative_to(root):
            outside.append(raw)
            continue
        hits = [
            bd for bd in bundles if p.is_relative_to(bd.root.resolve()) or bd.root.resolve().is_relative_to(p)
        ]
        if not hits:
            unknown.append(raw)
        for bd in hits:
            selected[bd.root.resolve()] = bd
    return list(selected.values()), unknown, outside


def _threshold_issues(b: SkillBundle, defaults) -> list[LintIssue]:
    """Per-skill thresholds that would loosen hub.yaml `defaults`. The runner ignores them."""
    ev, rb = b.root / "evals" / "evals.json", b.root / "evals" / "rubric.json"
    try:
        criteria = load_evals(ev).criteria if ev.is_file() else None
        rubric = load_rubric(rb) if rb.is_file() else None
    except ValueError:
        return []  # already reported by lint_bundle
    return [
        LintIssue(b.root.as_posix(), f"{msg}: a skill may only tighten the gate")
        for msg in loosening_thresholds(defaults, criteria, rubric)
    ]


def _model_issues(b: SkillBundle, models: frozenset[str]) -> list[LintIssue]:
    """evals.json `models` that are not hub.yaml profiles. A skill's own models are its gating models, and only
    hub.yaml profiles gate: a model catalog profile is picked per run in the Skill Hub."""
    ev = b.root / "evals" / "evals.json"
    try:
        named = (load_evals(ev).models or []) if ev.is_file() else []
    except ValueError:
        return []  # already reported by lint_bundle
    return [
        LintIssue(
            b.root.as_posix(),
            f"evals.json models names {m!r}, which is not a hub.yaml model profile; catalog models are picked "
            "per run in the Skill Hub",
        )
        for m in named
        if m not in models
    ]


def lint_repo(
    skills_root: Path | None = None,
    paths: Sequence[str] = (),
    *,
    departments: Iterable[str] | None = None,
    defaults=None,
    models: Iterable[str] | None = None,
) -> list[LintIssue]:
    """Lint every bundle under skills_root, or only the bundles that contain the given paths.

    With `departments` (hub.yaml keys), a department folder that is not a key is an issue. With `defaults`
    (hub.yaml `defaults`), a per-skill threshold that would loosen the gate is an issue. With `models` (hub.yaml
    model profile names), an evals.json `models` entry that is not one of them is an issue. Issues with `warning`
    set (lint_warnings) are reported but do not fail lint.
    """
    skills_root = skills_root or default_skills_root()
    issues: list[LintIssue] = []
    bundles: list[SkillBundle] = []
    if not skills_root.is_dir():
        return [
            LintIssue(
                skills_root.as_posix(), "missing_root: skills root does not exist or is not a directory"
            )
        ]
    # Deliberately not discover_bundles: lint must tolerate a bad bundle and report it.
    known_depts = set(departments) if departments is not None else None
    if skills_root.is_dir():
        for dept_dir in sorted(p for p in skills_root.iterdir() if p.is_dir()):
            if known_depts is not None and dept_dir.name not in known_depts:
                issues.append(
                    LintIssue(
                        dept_dir.as_posix(), f"department folder {dept_dir.name!r} is not a key in hub.yaml"
                    )
                )
                continue
            skill_dirs = sorted(p for p in dept_dir.iterdir() if p.is_dir())
            if len(skill_dirs) > MAX_SKILL_TOOLS:  # a department's run schema is one skills server connection
                issues.append(
                    LintIssue(
                        dept_dir.as_posix(),
                        f"department folder {dept_dir.name!r} holds {len(skill_dirs)} skills; the limit is "
                        f"{MAX_SKILL_TOOLS}, the most the skills server offers per connection",
                    )
                )
            for skill_dir in skill_dirs:
                try:
                    bundles.append(load_bundle(skill_dir, dept_dir.name))
                except BundleError as e:
                    # Reported even when `paths` filters: a broken bundle cannot be matched to a path.
                    issues.append(LintIssue(skill_dir.as_posix(), str(e)))
    known = {b.name for b in bundles}
    depts_by_name: dict[str, set[str]] = {}
    for b in bundles:
        depts_by_name.setdefault(b.name, set()).add(b.dept)
    selected, unknown, outside = _select(bundles, paths, skills_root)
    issues += [
        LintIssue(p, f"outside_root: path is not under the skills root {skills_root.resolve().as_posix()}")
        for p in outside
    ]
    issues += [
        LintIssue(p, "unknown_path: not inside any skill bundle under the skills root") for p in unknown
    ]
    for b in selected:
        issues += lint_bundle(b)
        if len(depts_by_name[b.name]) > 1:
            issues.append(LintIssue(b.root.as_posix(), f"name {b.name!r} is also used in another department"))
        issues += _near_miss_issues(b, known)
        if defaults is not None:
            issues += _threshold_issues(b, defaults)
        if models is not None:
            issues += _model_issues(b, frozenset(models))
        issues += lint_warnings(b)
    return issues
