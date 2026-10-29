#!/usr/bin/env python3
"""Mark generated test cases, or a rubric, as human-reviewed.

Examples:
  uv run python scripts/mark_reviewed.py --skill earnings-preview --ids 1,2,3
  uv run python scripts/mark_reviewed.py --skill earnings-preview --rubric

Only the listed cases change. Every other case and field stays as it is.
Cases are written before the rubric, so a rubric failure can follow a saved --ids update.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from skillhub.evalspec import load_evals, load_rubric, save_evals, save_rubric


def skill_dir(skill: str, skills_root: Path = Path("skills")) -> Path:
    found = sorted(skills_root.glob(f"*/{skill}/SKILL.md"))
    if len(found) != 1:
        raise SystemExit(f"expected one skills/<dept>/{skill}/SKILL.md, found {len(found)}")
    return found[0].parent


def mark_cases(evals_path: Path, ids: list[int]) -> list[int]:
    """Set reviewed=True on the listed case ids. Return the ids that changed."""
    ef = load_evals(evals_path)
    known = {c.id for c in ef.evals}
    unknown = sorted(set(ids) - known)
    if unknown:
        raise ValueError(f"unknown case ids in {evals_path}: {unknown}")
    changed: list[int] = []
    for case in ef.evals:
        if case.id in ids and not case.reviewed:
            case.reviewed = True
            changed.append(case.id)
    if changed:
        save_evals(evals_path, ef)
    return changed


def mark_rubric(rubric_path: Path) -> bool:
    """Set reviewed=True on a rubric. Return True when it changed."""
    rubric = load_rubric(rubric_path)
    if rubric.reviewed:
        return False
    rubric.reviewed = True
    save_rubric(rubric_path, rubric)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--skill", required=True)
    parser.add_argument("--ids", default="", help="comma-separated case ids")
    parser.add_argument("--rubric", action="store_true", help="also mark rubric.json reviewed")
    args = parser.parse_args(argv)
    try:
        ids = [int(x) for x in args.ids.split(",") if x.strip()]
    except ValueError:
        parser.error("--ids must be comma-separated integers")
    if not ids and not args.rubric:
        parser.error("give --ids and/or --rubric")
    evals_dir = skill_dir(args.skill) / "evals"
    try:
        if ids:
            changed = mark_cases(evals_dir / "evals.json", ids)
            print(f"{args.skill}: reviewed cases now marked {changed}")
        if args.rubric:
            print(f"{args.skill}: rubric newly marked {mark_rubric(evals_dir / 'rubric.json')}")
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
