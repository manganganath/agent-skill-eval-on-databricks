"""Whether a release reuses the pull request gate's eval instead of evaluating the merged content again.

The release reuses the gate only when the merge brought exactly the pull request head that the gate evaluated,
the pull request changed nothing but skills (so the gate ran the reviewed code that is now on main), and no gating
mode uses LLM judges (pull request runs use the single `judges_pr` judge, merge runs the panel). The gate's rows
must then pass the merge gate's own check against the merge plan. In every other case, and on any error, the
release evaluates the merged content as before.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from skillhub.ci.changes import SKILL_ROOT, _git, _resolve
from skillhub.naming import evals_schema
from skillhub.registry.versions import PR_TRIGGER_SOURCE

# Trigger verdicts come from a fixed scorer, never a judge, so the pull request run's judge does not matter.
REUSABLE_GATING_MODES = ("trigger",)
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")  # a full SHA-1 or SHA-256 commit ID
_SHOWN = 5  # paths named in a reason


@dataclass(frozen=True)
class MergeFacts:
    """What git says about the pushed commit."""

    parents: tuple[str, ...]
    same_tree: bool = False  # the merge's tree is the second parent's: the merge brought nothing else
    changed: tuple[str, ...] = ()  # paths the first and second parent differ in, both sides of a rename

    @property
    def head(self) -> str | None:
        """The pull request head: the second parent of a two-parent merge, else None."""
        return self.parents[1] if len(self.parents) == 2 else None


@dataclass(frozen=True)
class Reuse:
    reuse: bool
    run_key: str = ""  # the pull request gate's run key, when reused
    reason: str = ""  # why the merged content is evaluated again


def merge_facts(merge_ref: str, repo: Path = Path(".")) -> MergeFacts:
    """Parents, tree equality and the pull request's changed paths for the pushed commit. GitRefError when git
    fails (for example a parent missing from a shallow clone)."""
    sha = _resolve(repo, merge_ref)
    parents = tuple(_git(repo, "rev-list", "--parents", "-n", "1", sha).split()[1:])
    if len(parents) != 2:
        return MergeFacts(parents)
    trees = _git(repo, "rev-parse", f"{sha}^{{tree}}", f"{parents[1]}^{{tree}}").split()
    # Plumbing with renames off: a file moved into skills/ also lists the path it left.
    diff = _git(repo, "diff-tree", "-r", "--name-only", "--no-renames", "-z", parents[0], parents[1])
    changed = tuple(p for p in diff.split("\0") if p)
    return MergeFacts(parents, len(trees) == 2 and trees[0] == trees[1], changed)


def decide(
    facts: MergeFacts,
    gating_modes: Iterable[str],
    gate_run_keys: Callable[[str], Sequence[str]],
    gate_failures: Callable[[str], Sequence[str]],
) -> Reuse:
    """The release rule, checked in order. `gate_run_keys(head)` lists the pull request gate's run keys for the
    head commit, newest first; `gate_failures(run_key)` is the merge gate's check of that run against the merge
    plan ([] when it passes). The newest run that passes is reused."""
    head = facts.head
    if head is None:
        return Reuse(False, reason=f"the pushed commit has {len(facts.parents)} parent(s); it is not a merge")
    if not facts.same_tree:
        return Reuse(False, reason="the merge brought changes beyond the pull request head (trees differ)")
    if not facts.changed:
        return Reuse(False, reason="the pull request changed no files")
    outside = [p for p in facts.changed if not p.startswith(f"{SKILL_ROOT}/")]
    if outside:
        more = f" and {len(outside) - _SHOWN} more" if len(outside) > _SHOWN else ""
        shown = ", ".join(repr(p) for p in outside[:_SHOWN])  # repr: a path can hold a newline
        return Reuse(False, reason=f"the pull request changed files outside {SKILL_ROOT}/: {shown}{more}")
    modes = list(gating_modes)
    if modes != list(REUSABLE_GATING_MODES):
        return Reuse(
            False,
            reason=f"defaults.gating_modes is {modes}, not {list(REUSABLE_GATING_MODES)}: an effectiveness "
            "verdict of the pull request gate used one judge, the merge eval's uses the panel",
        )
    keys = list(gate_run_keys(head))
    if not keys:
        return Reuse(False, reason=f"no trusted pull request gate run for {head[:12]}")
    newest = ""
    for key in keys:
        failures = list(gate_failures(key))
        if not failures:
            return Reuse(True, run_key=key)
        newest = newest or f"{key}: {failures[0]}"
    return Reuse(False, reason=f"no pull request gate run for {head[:12]} passes the merge gate ({newest})")


def _gate_runs_sql(cfg, n_authors: int) -> str:
    """Run keys with a child row of the pull request gate for one commit, by trusted authors, newest first. Every
    department's evals schema holds an eval_runs table, so read all of them."""
    authors = ", ".join(f":author_{i}" for i in range(n_authors))
    parts = [
        f"SELECT run_key, started_at FROM {evals_schema(cfg, dept)}.eval_runs "
        f"WHERE trigger_source = :trigger_source AND git_sha = :git_sha AND parent_run_id IS NOT NULL "
        f"AND run_author IN ({authors})"
        for dept in cfg.departments
    ]
    return (
        f"SELECT run_key, MAX(started_at) AS newest FROM ({' UNION ALL '.join(parts)}) AS gate_runs "
        "GROUP BY run_key ORDER BY newest DESC, run_key DESC"
    )


def gate_run_keys(
    fetch: Callable[[str, dict[str, str]], list[dict[str, Any]]],
    cfg,
    git_sha: str,
    *,
    trusted: Iterable[str],
) -> list[str]:
    """The pull request gate's run keys for `git_sha`, newest first. Only keys skills-pr.yml forms for that commit
    count: its label `pr<N>-<sha7>-a<attempt>` as a run key. ValueError without trusted authors or a full SHA."""
    authors = sorted(set(trusted))
    if not authors:
        raise ValueError("no trusted run authors: set SKILLHUB_CI_EVAL_PRINCIPAL or run as ci_eval")
    if not _SHA.fullmatch(git_sha):
        raise ValueError(f"not a full commit SHA: {git_sha!r}")
    params = {"trigger_source": PR_TRIGGER_SOURCE, "git_sha": git_sha}
    params |= {f"author_{i}": a for i, a in enumerate(authors)}
    gate_key = re.compile(rf"pr[0-9]+_{git_sha[:7]}_a[0-9]+")
    keys = [r.get("run_key") for r in fetch(_gate_runs_sql(cfg, len(authors)), params)]
    return [k for k in keys if isinstance(k, str) and gate_key.fullmatch(k)]
