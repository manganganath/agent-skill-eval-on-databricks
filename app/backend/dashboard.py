"""Python steps of the Dashboard. SQL groups the rows (queries.dashboard_sql); these steps combine departments and
apply the two rules that need the whole result: the live version of each skill, and the summary tiles."""

from __future__ import annotations

from collections import Counter, defaultdict

from backend import skill_ref

VERDICTS = ("pass", "warn", "fail", "inconclusive", "advisory")


def live_uplift(rows: list[dict], journal: list[dict]) -> list[dict]:
    """The uplift rows of each skill's live version (skill_versions, as in skill_ref.live_version). A skill with no
    live version keeps its newest tested version, so the uplift still shows for it."""
    history: defaultdict[str, list[dict]] = defaultdict(list)
    for r in journal:  # oldest first, as the query orders them
        history[r["skill"]].append(r)
    live = {skill: skill_ref.live_version(rs) for skill, rs in history.items()}
    newest: dict[str, int] = {}
    for r in rows:
        newest[r["skill"]] = max(newest.get(r["skill"], 0), r["skill_version"])
    return [
        r
        for r in rows
        if r["skill_version"] == (newest[r["skill"]] if live.get(r["skill"]) is None else live[r["skill"]])
    ]


def _mean(rows: list[dict], total_key: str, count_key: str) -> float | None:
    """The mean over every department's rows: the sums and counts of each part, combined."""
    count = sum(int(r[count_key] or 0) for r in rows)
    if count == 0:
        return None
    return sum(float(r[total_key] or 0.0) for r in rows) / count


def summary(summaries: list[dict], verdicts: list[dict]) -> dict:
    """The summary tiles over every readable department and model. Verdicts count the latest gating rows."""
    counts: Counter[str] = Counter()
    for r in verdicts:
        counts[r["verdict"]] += int(r["n"])
    return {
        "skills": sum(int(r["skills"] or 0) for r in summaries),
        "verdicts": {v: counts[v] for v in VERDICTS},
        "trigger_accuracy": _mean(summaries, "accuracy_sum", "accuracy_n"),
        "uplift_task_quality": _mean(summaries, "uplift_sum", "uplift_n"),
        "error_rate": _mean(summaries, "error_sum", "error_n"),
    }
