from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from skillhub.bundle import BundleError, content_hash, parse_frontmatter
from skillhub.config import HubConfig
from skillhub.naming import live_fqn, live_schema
from skillhub.registry.journal import Journal
from skillhub.registry.uc_skills import UCSkills

IssueKind = Literal["hash_drift", "unfinished_switch", "unfinalized_change", "missing_live"]


@dataclass
class ReconcileIssue:
    """One finding. Callers filter on `kind`: switch re-creates a skill with `missing_live`, and
    retire treats `missing_live` as already gone, so both drop that kind from what blocks them."""

    dept: str
    skill: str
    kind: IssueKind
    detail: str


def _norm(text: str | None) -> str:
    return " ".join((text or "").split())


def reconcile_skill(
    cfg: HubConfig, uc: UCSkills, journal: Journal, dept: str, skill: str
) -> list[ReconcileIssue]:
    """Compare one live skill with its journal. Alerts only; never repairs.

    `missing_live` is always returned alone, so a caller can drop it without losing another finding.
    """
    out: list[ReconcileIssue] = []

    def add(kind: IssueKind, detail: str) -> None:
        out.append(ReconcileIssue(dept, skill, kind, detail))

    # The last row that starts or ends a switch decides it; published and drift rows are ignored.
    ends = ("switch_started", "switch_failed", "switched", "retired")
    history = journal.history(dept, skill)
    last_row = next((r for r in reversed(history) if r.action in ends), None)
    live_name = live_fqn(cfg, dept, skill)
    if last_row is not None and last_row.action in ("switch_started", "switch_failed"):
        # Re-running the switch repairs this; whole-bundle hash checks would only see a half-applied
        # state. A live file that matches neither the old nor the new version is still a hand edit.
        add("unfinished_switch", f"last switch row is {last_row.action}; re-running the switch repairs it")
        if uc.get(live_name) is not None:
            # Trusted: every switch target of the current unfinished chain (rows after the last
            # switched or retired row), plus the version live before it unless a retire intervened.
            idx = max((i for i, r in enumerate(history) if r.action in ("switched", "retired")), default=-1)
            fqns = {
                r.versioned_fqn for r in history[idx + 1 :] if r.action in ("switch_started", "switch_failed")
            }
            if idx >= 0 and history[idx].action == "switched":
                fqns.add(history[idx].versioned_fqn)
            known = [uc.read_files(f) for f in sorted(f for f in fqns if f)]
            if known:
                live_files = uc.read_files(live_name)
                stray = sorted(
                    p
                    for p, data in live_files.items()
                    if not p.startswith("evals/") and all(k.get(p) != data for k in known)
                )
                if stray:
                    add("hash_drift", f"live files match neither version of the unfinished switch: {stray}")
        return out
    live = uc.get(live_name)
    state = journal.live_state(dept, skill)  # Latest switched unless a later retired
    active = state is not None and state.action == "switched"
    if active and live is None:
        add("missing_live", "the journal says this skill is switched but the live skill is gone")
        return out
    if live is None:
        return out
    if not active:
        why = "the skill is retired" if state is not None else "the journal has no switch for it"
        add("hash_drift", f"a live copy exists but {why}")
        return out
    files = uc.read_files(live_name)
    actual = content_hash(files)
    if actual != state.content_hash:
        add(
            "hash_drift",
            f"live content {actual[:19]}... differs from the journal {(state.content_hash or '')[:19]}...",
        )
    if "SKILL.md" in files:
        try:
            description = parse_frontmatter(files["SKILL.md"]).get("description", "")
        except BundleError as e:
            add("unfinalized_change", f"SKILL.md cannot be read: {e}")
        else:
            if _norm(description) != _norm(live.description):
                add("unfinalized_change", "SKILL.md changed after the last finalize")
    return out


def reconcile(cfg: HubConfig, uc: UCSkills, journal: Journal) -> list[ReconcileIssue]:
    issues: list[ReconcileIssue] = []
    for dept in sorted(cfg.departments):
        names = {s.name for s in uc.list(live_schema(cfg, dept))} | set(journal.skills(dept))
        for skill in sorted(names):
            issues += reconcile_skill(cfg, uc, journal, dept, skill)
    return issues
