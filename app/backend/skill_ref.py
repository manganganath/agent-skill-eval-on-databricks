from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

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


def live_version(rows: list[dict]) -> int | None:
    """Live version from skill_versions rows, oldest first.

    The latest `switched` row's to_version is live, unless a later `retired` row exists. A `published` row
    never makes a version live. It relies on the rows being ordered by ts, as queries.journal_sql returns them.
    """
    current: int | None = None
    for r in rows:
        action = r.get("action")
        if action == "switched":
            current = r.get("to_version")
        elif action == "retired":
            current = None
    return current


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
) -> dict[str, dict[str, str]]:
    """{skill: {model_profile: worst verdict across the gating modes}} for each skill's live version.

    Skills without a live version get no verdicts. Baseline (without_skill) rows are ignored, and so are rows of
    a mode that does not gate a release (counts_toward_verdict).
    """
    gating_modes = tuple(gating_modes)
    grouped: dict[str, dict[str, list[str | None]]] = {}
    for r in rows:
        skill = str(r["skill"])
        live = live_versions.get(skill)
        if (
            live is None
            or r.get("skill_version") != live
            or not counts_toward_verdict(r.get("mode"), gating_modes)
        ):
            continue
        grouped.setdefault(skill, {}).setdefault(str(r["model_profile"]), []).append(r.get("verdict"))  # type: ignore[arg-type]
    return {s: {p: worst_verdict(v) for p, v in prof.items()} for s, prof in grouped.items()}
