"""Verdicts, uplift, trigger metrics and child-run logging.

The CI gate, PR comment, Skill Hub and dashboards read these values, and `sync` copies them unchanged.
Three kinds of result are kept apart on purpose:
- a fail: the judges answered and the case did not pass
- an error: no usable answer (judge or infrastructure failure, `Feedback(error=...)`, an undecided panel)
- a missing assessment (an out-of-scope scorer): it is not an assessment, so it is never a fail
"""

from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass, field
from typing import Literal

from mlflow import MlflowClient
from mlflow.entities import Metric

from skillhub.eval.outcomes import Outcome, case_outcome
from skillhub.evalspec import Criterion

Verdict = Literal["pass", "warn", "fail", "inconclusive", "advisory"]
PASS_OUTCOMES = frozenset({Outcome.CORRECT.value, Outcome.CORRECT_REJECT.value})
OUTCOME_VALUES = frozenset(o.value for o in Outcome)

# Metric names are the `eval_runs` columns that `sync` copies (tables.sql). `unreviewed_cases` is derived in a view.
CHILD_METRICS = frozenset(
    {
        "pass_rate",
        "spread",
        "error_rate",
        "n_cases",
        "n_reviewed",
        "accuracy",
        "precision",
        "recall",
        "pass_rate_without",
        "uplift_task_quality",
        "uplift_skill_format",
        "planted_bugs_recall",
    }
)


@dataclass
class CaseScore:
    case_id: int
    repeat: int
    reviewed: bool
    error: bool
    passed: bool | None  # None when error
    task_quality: float | None  # weighted score over task_quality criteria (0..1)
    skill_format: float | None


@dataclass
class VerdictResult:
    verdict: Verdict
    pass_rate: float
    spread: float
    error_rate: float
    n_cases: int
    n_reviewed: int
    metrics: dict[str, float] = field(default_factory=dict)


def panel_majority(votes: list[bool | None]) -> bool | None:
    """True only with a strict majority of the whole panel (one vote per configured judge; None = no response).

    The result is decided only when the missing votes cannot change it: 2 of 3 judges voting True
    passes; 1 True and 2 missing is undecided, because the silent judges could outvote it. Undecided is None,
    which makes the case an error. A tie with every judge responding is False. No votes is None.
    """
    n = len(votes)
    yes = sum(1 for v in votes if v is True)
    missing = sum(1 for v in votes if v is None)
    if n == 0:
        return None
    if 2 * yes > n:
        return True
    if 2 * (yes + missing) <= n:
        return False
    return None


def weighted_score(items: list[tuple[int, bool | None]]) -> float | None:
    """Weight of the passing criteria over the total weight. None when there is nothing to score."""
    total = sum(w for w, _ in items)
    if not items or total == 0:
        return None
    return sum(w for w, p in items if p) / total


def _error_score(case_id: int, repeat: int, reviewed: bool) -> CaseScore:
    return CaseScore(case_id, repeat, reviewed, True, None, None, None)


def case_score_from_assessments(
    *,
    case_id: int,
    repeat: int,
    reviewed: bool,
    criteria: list[Criterion],
    votes_by_criterion: dict[str, list[bool | None]],
    assertion_votes: list[bool | None] | None,
    pass_score: float,
    error: bool = False,
) -> CaseScore:
    """Score one case-repeat. Each vote list holds one entry per judge model; None means the judge did not respond.

    A criterion (or the assertions) that no judge answered, or that has no assessment at all, makes the case an
    error, never a fail. `assertion_votes=None` means the case has no assertions; an empty list means assertions
    were expected but no assessment was found, which is an error. Pass `error=True` for a
    failure outside the judges (a misconfigured scorer, a failed agent run).
    """
    majorities = {c.id: panel_majority(votes_by_criterion.get(c.id, [])) for c in criteria}
    assertion = True if assertion_votes is None else panel_majority(assertion_votes)
    if error or assertion is None or any(v is None for v in majorities.values()):
        return _error_score(case_id, repeat, reviewed)
    per = [(c, bool(majorities[c.id])) for c in criteria]
    task_quality = weighted_score([(c.weight, p) for c, p in per if c.type == "task_quality"])
    skill_format = weighted_score([(c.weight, p) for c, p in per if c.type == "skill_format"])
    total = weighted_score([(c.weight, p) for c, p in per]) or 0.0
    passed = total >= pass_score and assertion
    return CaseScore(case_id, repeat, reviewed, False, passed, task_quality, skill_format)


def trigger_case_score(case_id: int, repeat: int, reviewed: bool, outcome: str | None) -> CaseScore:
    """A trigger case passes on `correct` or `correct_reject`. A missing or unknown outcome is an error."""
    if outcome is None or str(outcome) not in OUTCOME_VALUES:
        return _error_score(case_id, repeat, reviewed)
    return CaseScore(case_id, repeat, reviewed, False, str(outcome) in PASS_OUTCOMES, None, None)


def _valid(scores: list[CaseScore]) -> list[CaseScore]:
    return [s for s in scores if not s.error]


def gating_scores(scores: list[CaseScore]) -> list[CaseScore]:
    """Case-repeats that count: reviewed and not an error. Falls back to all valid ones only for display
    when nothing is reviewed (such a run is `advisory`). When reviewed cases exist but all errored, there
    is no fallback to unreviewed ones. The runner uses the same population for trigger metrics and
    planted-bug recall."""
    valid = _valid(scores)
    reviewed = [s for s in valid if s.reviewed]
    if reviewed or any(s.reviewed for s in scores):
        return reviewed
    return valid


def _pass_rate(scores: list[CaseScore]) -> float:
    return sum(1 for s in scores if s.passed) / len(scores) if scores else 0.0


def compute_verdict(
    scores: list[CaseScore],
    *,
    fail_rate: float,
    warn_rate: float,
    max_error_rate: float,
    min_reviewed_cases: int,
    judges_responded_ratio: float = 1.0,
) -> VerdictResult:
    """Precedence: inconclusive (overall errors, judges, no usable reviewed result), then advisory (too few
    reviewed cases), then inconclusive (reviewed error rate, too few reviewed cases with a usable result),
    then thresholds.

    `n_cases` and `n_reviewed` count distinct cases, not repeats.
    """
    n_cases = len({s.case_id for s in scores})
    reviewed_all = [s for s in scores if s.reviewed]
    # An errored reviewed case still counts as reviewed: errors must never loosen the gate (and keep
    # `unreviewed_cases = n_cases - n_reviewed` right).
    n_reviewed = len({s.case_id for s in reviewed_all})
    n_valid_reviewed = len({s.case_id for s in _valid(reviewed_all)})
    error_rate = (sum(1 for s in scores if s.error) / len(scores)) if scores else 1.0
    reviewed_error_rate = sum(1 for s in reviewed_all if s.error) / len(reviewed_all) if reviewed_all else 0.0
    gate = gating_scores(scores)
    pass_rate = _pass_rate(gate)
    per_repeat = [_pass_rate([s for s in gate if s.repeat == r]) for r in sorted({s.repeat for s in gate})]
    spread = statistics.pstdev(per_repeat) if len(per_repeat) >= 2 else 0.0

    if error_rate > max_error_rate or judges_responded_ratio < 0.5:
        verdict: Verdict = "inconclusive"
    elif n_reviewed > 0 and n_valid_reviewed == 0:
        verdict = "inconclusive"  # reviewed cases exist but none produced a usable result
    elif n_reviewed == 0 or n_reviewed < min_reviewed_cases:
        verdict = "advisory"  # advisory never gates, so the reviewed error rate is not checked here
    elif reviewed_error_rate > max_error_rate:
        verdict = "inconclusive"
    elif n_valid_reviewed < min_reviewed_cases:
        verdict = "inconclusive"  # enough reviewed cases exist, but too few produced a usable result
    elif pass_rate < fail_rate:
        verdict = "fail"
    elif pass_rate < warn_rate:
        verdict = "warn"
    else:
        verdict = "pass"

    metrics = {
        "pass_rate": pass_rate,
        "spread": spread,
        "error_rate": error_rate,
        "n_cases": float(n_cases),
        "n_reviewed": float(n_reviewed),
    }
    return VerdictResult(verdict, pass_rate, spread, error_rate, n_cases, n_reviewed, metrics)


def _mean(xs: list[float]) -> float | None:
    return statistics.fmean(xs) if xs else None


def _per_case(scores: list[CaseScore]) -> dict[int, dict]:
    """Per case_id, over its non-error repeats: reviewed flag and the mean of each score."""
    groups: dict[int, list[CaseScore]] = {}
    for sc in _valid(scores):
        groups.setdefault(sc.case_id, []).append(sc)
    return {
        cid: {
            "reviewed": all(x.reviewed for x in g),
            "uplift_task_quality": _mean([x.task_quality for x in g if x.task_quality is not None]),
            "uplift_skill_format": _mean([x.skill_format for x in g if x.skill_format is not None]),
            "pass": _mean([float(bool(x.passed)) for x in g]),
        }
        for cid, g in groups.items()
    }


def uplift(with_scores: list[CaseScore], without_scores: list[CaseScore]) -> dict[str, float]:
    """with_skill minus without_skill, paired by case_id.

    Each case contributes the mean over its non-error repeats on each side. Only cases with a valid result on both
    sides count. Reviewed pairs are used; if there are none, all pairs are used (for both sides together).
    `pass_rate_without` is computed over the same paired cases. A value that cannot be computed is left out, not
    reported as 0. `without_skill` is a baseline and never gates.
    """
    w, wo = _per_case(with_scores), _per_case(without_scores)
    pairs = [c for c in w if c in wo]
    reviewed = [c for c in pairs if w[c]["reviewed"] and wo[c]["reviewed"]]
    pairs = reviewed or pairs
    out: dict[str, float] = {}
    for key in ("uplift_task_quality", "uplift_skill_format"):
        both = [c for c in pairs if w[c][key] is not None and wo[c][key] is not None]
        if both:
            out[key] = statistics.fmean(w[c][key] for c in both) - statistics.fmean(wo[c][key] for c in both)
    if pairs:
        out["pass_rate_without"] = statistics.fmean(wo[c]["pass"] for c in pairs)
    return out


def trigger_metrics(pairs: list[tuple[str | None, str | None]], skill: str) -> dict[str, float]:
    """`pairs` is (expected skill name, first invoked skill name) per trigger case that produced a result.
    Accuracy covers all pairs. Precision and recall are for `skill`. A metric whose denominator is zero is
    undefined and left out. Callers pass the gating population (reviewed, non-error cases)."""
    correct = sum(1 for e, i in pairs if case_outcome(e, i) in (Outcome.CORRECT, Outcome.CORRECT_REJECT))
    tp = sum(1 for e, i in pairs if e == skill and i == skill)
    fp = sum(1 for e, i in pairs if i == skill and e != skill)
    fn = sum(1 for e, i in pairs if e == skill and i != skill)
    out: dict[str, float] = {}
    if pairs:
        out["accuracy"] = correct / len(pairs)
    if tp + fp:
        out["precision"] = tp / (tp + fp)
    if tp + fn:
        out["recall"] = tp / (tp + fn)
    return out


def log_verdict(
    run_id: str,
    result: VerdictResult,
    *,
    extra_metrics: dict[str, float] | None = None,
    client: MlflowClient | None = None,
) -> None:
    """Log metrics and the `verdict` tag on the child run. Call it last. Unknown metric names and non-finite values raise
    before anything is written, because `sync` only copies the `eval_runs` columns."""
    extra = extra_metrics or {}
    unknown = sorted(set(extra) - CHILD_METRICS)
    if unknown:
        raise ValueError(f"unknown child-run metric(s) {unknown}; allowed: {sorted(CHILD_METRICS)}")
    values = {k: float(v) for k, v in {**result.metrics, **extra}.items()}
    bad = sorted(k for k, v in values.items() if not math.isfinite(v))
    if bad:
        raise ValueError(f"non-finite child-run metric value(s) for {bad}")
    c = client or MlflowClient()
    now = int(time.time() * 1000)
    c.log_batch(run_id, metrics=[Metric(k, v, now, 0) for k, v in values.items()])
    c.set_tag(run_id, "verdict", result.verdict)
