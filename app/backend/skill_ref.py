from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import TypeVar

from backend.authz import Reason
from backend.deps import problem
from skillhub.config import HubConfig
from skillhub.evalspec import CASE_KIND, REQUEST_MODES
from skillhub.naming import live_schema

_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")  # used with fullmatch: "$" would let a trailing newline in
# Worst first. An unknown verdict ranks worse than all of these.
VERDICT_SEVERITY = ("fail", "inconclusive", "warn", "advisory", "pass")
# The baseline run never counts against a skill.
BASELINE_MODE = "without_skill"


def parse_fqn(cfg: HubConfig, fqn: str) -> tuple[str, str]:
    """Map `catalog.schema.skill` to (dept, skill). An unknown catalog, schema or name is a 404."""
    parts = fqn.split(".")
    if len(parts) != 3 or not _NAME.fullmatch(parts[2]):
        raise problem(404, Reason.SKILL_NOT_FOUND)
    catalog_schema = f"{parts[0]}.{parts[1]}"
    for dept in cfg.departments:
        if live_schema(cfg, dept) == catalog_schema:
            return dept, parts[2]
    raise problem(404, Reason.SKILL_NOT_FOUND)


def _live(rows: list[dict]) -> tuple[int | None, str | None]:
    """(version, content hash) that are live: the latest `switched` row's, cleared by a later `retired` row."""
    version: int | None = None
    content: str | None = None
    for r in rows:
        action = r.get("action")
        if action == "switched":
            version = r.get("to_version")
            content = r.get("content_hash") or None
        elif action == "retired":
            version = content = None
    return version, content


def live_version(rows: list[dict]) -> int | None:
    """Live version from skill_versions rows, oldest first.

    The latest `switched` row's to_version is live, unless a later `retired` row exists. A `published` row
    never makes a version live. It relies on the rows being ordered by ts, as queries.journal_sql returns them.
    """
    return _live(rows)[0]


def live_content_hash(rows: list[dict]) -> str | None:
    """The content hash of the live version, from the same rows as live_version. None when nothing is live."""
    return _live(rows)[1]


def _stamp(value: object) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value or "")


def is_live_row(row: Mapping[str, object], version: int | None, content_hash: str | None) -> bool:
    """Whether a matrix row was run on the live version: its skill_version is the live one, or its content is the live
    content. A release's eval (the merge re-evaluation, or a reused pull request gate run) runs before publishing,
    so its rows are version 0 with the live hash."""
    return row.get("skill_version") == version or (
        content_hash is not None and row.get("content_hash") == content_hash
    )


_Row = TypeVar("_Row", bound=Mapping[str, object])


def newest_per_key(rows: Iterable[_Row], key: Callable[[_Row], tuple]) -> list[_Row]:
    """The row with the latest started_at for each key. A tie keeps the row that came first."""
    best: dict[tuple, _Row] = {}
    for r in rows:
        k = key(r)
        if k not in best or _stamp(r.get("started_at")) > _stamp(best[k].get("started_at")):
            best[k] = r
    return list(best.values())


def _severity(verdict: object) -> int:
    return VERDICT_SEVERITY.index(verdict) if verdict in VERDICT_SEVERITY else -1


def worst_verdict(verdicts: Iterable[str | None]) -> str:
    """The worst of several verdicts. NULL counts as inconclusive. An unknown verdict ranks worst."""
    cleaned = ["inconclusive" if v is None else v for v in verdicts]
    return str(min(cleaned, key=_severity))


def counts_toward_verdict(mode: object, gating_modes: Iterable[str] = REQUEST_MODES) -> bool:
    """Whether a row of this run mode counts toward a skill's verdict chips: the rule of the release gate. The
    baseline never counts, a mode left out of `gating_modes` (config defaults.gating_modes) is reported on the
    matrix only, and an unknown mode always counts (fail closed)."""
    if mode == BASELINE_MODE:
        return False
    kind = CASE_KIND.get(str(mode or ""))
    return kind is None or kind in set(gating_modes)


def live_verdicts(
    rows: Iterable[Mapping[str, object]],
    live_versions: Mapping[str, int | None],
    gating_modes: Iterable[str] = REQUEST_MODES,
    live_hashes: Mapping[str, str | None] | None = None,
) -> dict[str, dict[str, str]]:
    """{skill: {model_profile: worst verdict across the gating modes}} for each skill's live version.

    A row counts when it is on the live version (is_live_row, with the live content hash from `live_hashes`). Skills
    without a live version get no verdicts. Baseline (without_skill) rows are ignored, and so are rows of a mode that
    does not gate a release (counts_toward_verdict). For each skill, model and mode only the newest row counts.
    """
    gating_modes = tuple(gating_modes)
    hashes = live_hashes or {}
    eligible = []
    for r in rows:
        skill = str(r["skill"])
        live = live_versions.get(skill)
        if (
            live is not None
            and is_live_row(r, live, hashes.get(skill))
            and counts_toward_verdict(r.get("mode"), gating_modes)
        ):
            eligible.append(r)
    grouped: dict[str, dict[str, list[str | None]]] = {}
    for r in newest_per_key(eligible, lambda r: (r["skill"], r["model_profile"], r["mode"])):
        skill = str(r["skill"])
        grouped.setdefault(skill, {}).setdefault(str(r["model_profile"]), []).append(r.get("verdict"))  # type: ignore[arg-type]
    return {s: {p: worst_verdict(v) for p, v in prof.items()} for s, prof in grouped.items()}
