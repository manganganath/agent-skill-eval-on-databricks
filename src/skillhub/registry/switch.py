from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from skillhub import SkillHubError
from skillhub.bundle import content_hash
from skillhub.config import HubConfig
from skillhub.naming import live_fqn, live_schema
from skillhub.registry.journal import Action, Journal, JournalRow, new_row
from skillhub.registry.reconcile import ReconcileIssue, reconcile_skill
from skillhub.registry.uc_skills import UCSkills

MAX_ATTEMPTS = 4  # the first attempt plus up to 3 repeats of steps 3 to 6

SwitchStatus = Literal["switched", "noop", "drift", "switch_failed"]


class SwitchError(SkillHubError):
    """The requested switch cannot happen, for example because the version was never published."""


class DriftError(SkillHubError):
    """The live skill was changed outside the pipeline, so the change is refused."""


@dataclass
class SwitchResult:
    status: SwitchStatus
    from_version: int | None
    to_version: int
    detail: str = ""


def _published(history: list[JournalRow], dept: str, skill: str, version: int) -> JournalRow:
    for row in history:
        if row.action == "published" and row.version == version:
            return row
    raise SwitchError(f"{dept}/{skill} version {version} was never published")


def _live_state(history: list[JournalRow]) -> JournalRow | None:
    """The last switched or retired row."""
    return next((r for r in reversed(history) if r.action in ("switched", "retired")), None)


def _split(issues: list[ReconcileIssue]) -> tuple[bool, list[ReconcileIssue]]:
    """(unfinished switch?, blocking issues). An unfinished switch is repaired by re-running it, and
    missing_live is not blocking: switch re-creates the skill and retire treats it as gone."""
    interrupted = any(i.kind == "unfinished_switch" for i in issues)
    return interrupted, [i for i in issues if i.kind not in ("unfinished_switch", "missing_live")]


def _apply_version(uc: UCSkills, live_name: str, target_files: dict[str, bytes]) -> None:
    """Steps 3 to 6. Each step is idempotent, so a re-run converges."""
    current = uc.read_files(live_name)
    for rel in sorted(target_files):
        if rel != "SKILL.md" and current.get(rel) != target_files[rel]:
            uc.upload_file(live_name, rel, target_files[rel])
    uc.upload_file(live_name, "SKILL.md", target_files["SKILL.md"])  # from here the new version is live
    for rel in sorted(set(current) - set(target_files)):
        uc.delete_file(live_name, rel)
    uc.finalize(live_name)


def _record_drift(
    uc: UCSkills,
    journal: Journal,
    live_name: str,
    dept: str,
    skill: str,
    actor: str,
    job_run_id: str | None,
) -> None:
    live = uc.get(live_name)
    journal.append(
        new_row(
            dept,
            skill,
            "drift",
            actor=actor,
            job_run_id=job_run_id,
            content_hash=content_hash(uc.read_files(live_name)) if live else None,
        )
    )


def switch(
    cfg: HubConfig,
    uc: UCSkills,
    journal: Journal,
    dept: str,
    skill: str,
    to_version: int,
    *,
    actor: str,
    job_run_id: str | None,
) -> SwitchResult:
    """Switch the live skill to a published version, in the order.

    Run only through the skill-switch Job (`max_concurrent_runs: 1`), which serialises changes.
    """
    history = journal.history(dept, skill)
    target = _published(history, dept, skill, to_version)
    stored = uc.read_files(target.versioned_fqn)
    if content_hash(stored) != target.content_hash:
        raise SwitchError(f"{target.versioned_fqn} no longer matches its publish record")
    target_files = {p: d for p, d in stored.items() if not p.startswith("evals/")}
    live_name = live_fqn(cfg, dept, skill)
    state = _live_state(history)
    active = state is not None and state.action == "switched"
    from_version = state.to_version if active else None

    def _row(action: Action):
        return new_row(
            dept,
            skill,
            action,
            actor=actor,
            job_run_id=job_run_id,
            from_version=from_version,
            to_version=to_version,
            versioned_fqn=target.versioned_fqn,
            content_hash=target.content_hash,
        )

    # Step 1: drift check, before anything is written.
    interrupted, issues = _split(reconcile_skill(cfg, uc, journal, dept, skill))
    if issues:
        _record_drift(uc, journal, live_name, dept, skill, actor, job_run_id)
        return SwitchResult("drift", from_version, to_version, "; ".join(i.detail for i in issues))

    live = uc.get(live_name)
    if active and from_version == to_version and live is not None and not interrupted:
        if content_hash(uc.read_files(live_name)) == target.content_hash:
            return SwitchResult("noop", from_version, to_version)

    # Step 2: journal the start before any file is touched.
    journal.append(_row("switch_started"))
    if live is None:
        uc.create(live_schema(cfg, dept), skill)

    # Steps 3 to 6, with a read-back check after each finalize.
    for _ in range(MAX_ATTEMPTS):
        _apply_version(uc, live_name, target_files)
        if content_hash(uc.read_files(live_name)) == target.content_hash:
            break
    else:
        journal.append(_row("switch_failed"))
        return SwitchResult(
            "switch_failed",
            from_version,
            to_version,
            f"read-back still differs after {MAX_ATTEMPTS} attempts",
        )

    # Step 7: tags, then the ending row.
    uc.set_tag(live_name, "version", str(to_version))
    uc.set_tag(live_name, "content_hash", target.content_hash)
    journal.append(_row("switched"))
    return SwitchResult("switched", from_version, to_version)


def retire(
    cfg: HubConfig,
    uc: UCSkills,
    journal: Journal,
    dept: str,
    skill: str,
    *,
    actor: str,
    job_run_id: str | None,
) -> Literal["retired", "nothing_to_retire"]:
    """Delete the live skill and journal `retired`. Published versions are kept.

    Run only through the skill-switch Job (`max_concurrent_runs: 1`), which serialises changes.
    """
    live_name = live_fqn(cfg, dept, skill)
    interrupted, issues = _split(reconcile_skill(cfg, uc, journal, dept, skill))
    if issues:
        _record_drift(uc, journal, live_name, dept, skill, actor, job_run_id)
        raise DriftError("; ".join(i.detail for i in issues))
    live = uc.get(live_name)
    if live is None:
        state = _live_state(journal.history(dept, skill))
        # Deleted by hand, or an unfinished switch never created it: close the journal.
        if interrupted or (state is not None and state.action == "switched"):
            journal.append(new_row(dept, skill, "retired", actor=actor, job_run_id=job_run_id))
            return "retired"
        return "nothing_to_retire"
    uc.delete(live_name, etag=live.etag)  # the etag was read just before the call
    journal.append(new_row(dept, skill, "retired", actor=actor, job_run_id=job_run_id))
    return "retired"
