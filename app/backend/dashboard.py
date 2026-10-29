"""Python steps of the Dashboard. SQL reads small row sets (queries.dashboard_sql); these steps keep each skill's
live version, count verdicts and health per model, and combine the departments into the summary tiles."""

from __future__ import annotations

from collections import Counter, defaultdict

from backend import skill_ref

VERDICTS = ("pass", "warn", "fail", "inconclusive", "advisory")


def live_rows(rows: list[dict], journal: list[dict]) -> list[dict]:
    """The rows of each skill's live version (skill_versions, as in skill_ref.live_version). A skill with no live
    version keeps its newest tested version, so its results still show. Older versions never count."""
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


def _sum_count(cells: list[dict], key: str, mode: str | None = None) -> tuple[float | None, int]:
    values = [
        float(r[key]) for r in cells if r.get(key) is not None and (mode is None or r.get("mode") == mode)
    ]
    return (sum(values) if values else None), len(values)


def department_summary(cells: list[dict]) -> dict:
    """One department's part of the summary tiles: sums and counts, so departments combine into exact means.
    `cells` are the department's live-version matrix rows without the baseline (live_rows)."""
    accuracy_sum, accuracy_n = _sum_count(cells, "accuracy", "trigger")
    uplift_sum, uplift_n = _sum_count(cells, "uplift_task_quality", "with_skill")
    error_sum, error_n = _sum_count(cells, "error_rate")
    return {
        "skills": len({r["skill"] for r in cells}),
        "accuracy_sum": accuracy_sum,
        "accuracy_n": accuracy_n,
        "uplift_sum": uplift_sum,
        "uplift_n": uplift_n,
        "error_sum": error_sum,
        "error_n": error_n,
    }


def verdict_counts(cells: list[dict]) -> list[dict]:
    """Live-version matrix rows per model and verdict. A NULL verdict counts as inconclusive, as on the cards."""
    counts = Counter((r["model_profile"], r.get("verdict") or "inconclusive") for r in cells)
    return [{"model_profile": m, "verdict": v, "n": n} for (m, v), n in sorted(counts.items())]


def health(cells: list[dict]) -> list[dict]:
    """Per model: how many live-version rows, their mean error rate, and the share that came out inconclusive."""
    by_model: defaultdict[str, list[dict]] = defaultdict(list)
    for r in cells:
        by_model[r["model_profile"]].append(r)
    out = []
    for model, rows in sorted(by_model.items()):
        errors = [float(r["error_rate"]) for r in rows if r.get("error_rate") is not None]
        out.append(
            {
                "model_profile": model,
                "cells": len(rows),
                "error_rate": sum(errors) / len(errors) if errors else None,
                "inconclusive_rate": sum(
                    1 for r in rows if (r.get("verdict") or "inconclusive") == "inconclusive"
                )
                / len(rows),
            }
        )
    return out


def summary(summaries: list[dict], verdicts: list[dict]) -> dict:
    """The summary tiles over every readable department and model. Verdicts count each skill's live-version rows."""
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
