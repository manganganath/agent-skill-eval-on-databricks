from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from skillhub import SkillHubError
from skillhub.bundle import SkillBundle, content_hash
from skillhub.config import HubConfig
from skillhub.lint import assert_clean
from skillhub.naming import version_fqn, versions_schema
from skillhub.registry.journal import Journal, new_row
from skillhub.registry.uc_skills import UCSkills
from skillhub.sql import SqlExecutor


class MissingProvenance(SkillHubError, ValueError):
    """git_sha or eval_run_id is empty, so the version would have no provenance."""


class PublishRefused(SkillHubError):
    """The content to publish has no eval result that allows it to go live."""


# Verdicts that let content be published. fail, inconclusive, NULL and anything unknown block it.
NON_BLOCKING = frozenset({"pass", "warn", "advisory"})


def check_provenance(git_sha: str, eval_run_id: str) -> None:
    for label, value in (("git_sha", git_sha), ("eval_run_id", eval_run_id)):
        if not value or not value.strip():
            raise MissingProvenance(f"{label} must not be empty")


def require_evaluated(
    cfg: HubConfig,
    sql: SqlExecutor,
    dept: str,
    skill: str,
    *,
    run_key: str,
    content_hash: str,
    trusted: Iterable[str],
    rubric_hash: str | None = None,
    dataset_digest: str | None = None,
    cells: Iterable[str] | None = None,
) -> None:
    """Refuse unless the eval for `run_key` evaluated exactly this content with the expected eval inputs and
        did not block it.

        Uses the merge gate's reader (`ci.pr_comment`): the latest gating row of each (version, model, harness,
        mode) cell, from runs by `trusted` authors only, so a run logged by anyone else cannot let content through
    . Every gating row of this skill must carry `content_hash`, `rubric_hash` and `dataset_digest` (the
        rubric, and evals.json with the fixtures its cases use, from the merge plan), and none may have a blocking
        verdict. Content or eval inputs swapped between the eval and the publish therefore never go live. Every
        planned gating cell (`cells`, model/mode) needs a row: a failing child run deleted before sync cannot
        leave only passing rows. Without the expected eval inputs or cells the publish is refused (fail closed).
    """
    from skillhub.ci.pr_comment import (
        Expected,
        binding_mismatches,
        blocking_verdicts,
        load_matrix_rows,
        missing_cells,
    )

    where = f"{dept}/{skill} for run key {run_key}"
    if not rubric_hash or not dataset_digest:
        raise PublishRefused(
            f"the expected rubric_hash and dataset_digest of {where} are missing; publish binds the eval inputs "
            "as well as the content (pass them from `skillhub eval-digests`)"
        )
    planned = tuple(cells or ())
    if not planned:
        raise PublishRefused(
            f"the planned gating cells of {where} are missing; publish needs a result for every planned "
            "model and mode (pass them from `skillhub eval-digests`)"
        )
    try:
        rows = load_matrix_rows(sql.execute, cfg, run_key, trusted=trusted)
    except ValueError as e:  # no trusted authors: fail closed
        raise PublishRefused(f"cannot check the eval for {where}: {e}") from None
    rows = [r for r in rows if r.get("dept") == dept and r.get("skill") == skill]
    gating = [r for r in rows if r.get("mode") != "without_skill"]
    if not gating:
        raise PublishRefused(f"no gating eval result for {where}; evaluate this content before publishing")
    expected = {f"{dept}/{skill}": Expected(content_hash, rubric_hash, dataset_digest, planned)}
    absent = missing_cells(rows, expected)
    if absent:
        raise PublishRefused(
            f"the eval for {where} has no trusted result for planned cell(s) "
            + ", ".join(a.split(" ", 1)[1] for a in absent)
        )
    wrong = binding_mismatches(rows, expected)
    if wrong:
        raise PublishRefused(
            f"the eval for {where} ran on other content or other eval inputs, so something changed after it "
            "was evaluated: " + "; ".join(wrong)
        )
    if any(v not in NON_BLOCKING for v in blocking_verdicts(rows)):
        cells = sorted(
            f"{r.get('model_profile')}/{r.get('mode') or 'none'}={r.get('verdict') or 'none'}"
            for r in gating
            if not r.get("mode") or r.get("verdict") not in NON_BLOCKING
        )
        raise PublishRefused(f"the eval for {where} blocks publishing: {', '.join(cells)}")


@dataclass
class PublishResult:
    version: int
    versioned_fqn: str
    created: bool


def publish(
    cfg: HubConfig,
    uc: UCSkills,
    journal: Journal,
    bundle: SkillBundle,
    *,
    git_sha: str,
    eval_run_id: str,
    actor: str,
    job_run_id: str | None,
) -> PublishResult:
    """Register the next immutable version -v<N>. The live skill is not touched.

    Not safe to run concurrently for one skill: call it only through the serialised skill-switch Job
    (max_concurrent_runs: 1). If a step fails after the version is created, the partial version is removed.
    """
    check_provenance(git_sha, eval_run_id)
    assert_clean(bundle)  # nothing is written for an invalid bundle
    ch = content_hash(bundle.files)
    existing_version = journal.version_by_hash(bundle.dept, bundle.name, ch)
    if existing_version is not None:
        return PublishResult(
            existing_version, version_fqn(cfg, bundle.dept, bundle.name, existing_version), False
        )
    n = journal.latest_version(bundle.dept, bundle.name) + 1
    vfqn = version_fqn(cfg, bundle.dept, bundle.name, n)
    partial = uc.get(vfqn)
    if partial is not None:
        # A previous publish crashed before it was journaled. No journal row refers to it, so replace it.
        uc.delete(vfqn, etag=partial.etag)
    try:
        uc.create(versions_schema(cfg, bundle.dept), f"{bundle.name}-v{n}")
        for rel, data in sorted(bundle.files.items()):
            uc.upload_file(vfqn, rel, data)
        uc.finalize(vfqn)
        tags = {"version": str(n), "content_hash": ch, "git_sha": git_sha, "eval_run_id": eval_run_id}
        for key, value in tags.items():
            uc.set_tag(vfqn, key, value)
    except Exception:
        _discard_partial(uc, vfqn)
        raise
    journal.append(
        new_row(
            bundle.dept,
            bundle.name,
            "published",
            actor=actor,
            job_run_id=job_run_id,
            version=n,
            versioned_fqn=vfqn,
            content_hash=ch,
            git_sha=git_sha,
            eval_run_id=eval_run_id,
        )
    )
    return PublishResult(n, vfqn, True)


def _discard_partial(uc: UCSkills, vfqn: str) -> None:
    """Best effort: remove a half-built version so the next publish starts clean."""
    try:
        partial = uc.get(vfqn)
        if partial is not None:
            uc.delete(vfqn, etag=partial.etag)
    except Exception:  # noqa: BLE001 - the original error is the one to report
        pass
