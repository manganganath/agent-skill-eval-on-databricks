from __future__ import annotations

import json
from pathlib import Path

from mlflow.entities import Feedback, SpanType
from mlflow.genai.scorers import scorer

from skillhub.eval.outcomes import case_outcome
from skillhub.evalspec import PLANTED_FIXTURE, PLANTED_MANIFEST, EvalCase  # the dataset digest covers both


def invoked_skill_names(trace) -> list[str]:
    """Skill NAMES (last dotted segment of `skill.fqn`) for each TOOL span, in call order.

    The agent loop sets `skill.fqn` only for skills that were offered and invoked, so a hallucinated
    `skill_*` call (a TOOL span without the attribute) does not count.
    """
    names: list[str] = []
    for span in trace.data.spans:
        if span.span_type != SpanType.TOOL:
            continue
        fqn = (span.attributes or {}).get("skill.fqn")
        if fqn:
            names.append(str(fqn).rsplit(".", 1)[-1])
    return names


def skill_trigger_feedback(trace, expected: str | None) -> Feedback:
    """`expected` is the skill NAME the evals.json case expects, or None. It never comes from the dataset row,
    which can carry a stale label."""
    invoked = invoked_skill_names(trace)
    first = invoked[0] if invoked else None
    outcome = case_outcome(expected, first)
    return Feedback(
        name="skill_trigger",
        value=outcome.value,
        rationale=f"expected={expected} invoked={first}",
        metadata={"invoked_skill": first, "expected_skill": expected},
    )


def _final_text(trace) -> str:
    """The final answer, from the root `agent_run` span outputs (`final_text`, written by the agent loop)."""
    text = ""
    for span in trace.data.spans:
        if span.name != "agent_run":
            continue
        outputs = span.outputs
        if isinstance(outputs, str):  # a trace read back from Unity Catalog trace storage holds a JSON string
            try:
                outputs = json.loads(outputs)
            except ValueError:
                continue
        if isinstance(outputs, dict):
            text = str(outputs.get("final_text") or "")
    return text


def planted_bugs_feedback(trace, bugs: list[dict], bug_scope: list[str] | None = None) -> Feedback:
    """Recall of the planted-bug manifest in the final answer. Code only."""
    if bug_scope is not None:
        scoped = [b for b in bugs if b["id"] in bug_scope]
        if not scoped:
            # An unknown or misspelled bug id is a misconfigured case, not "found none".
            known = sorted(b["id"] for b in bugs)
            return Feedback(
                name="planted_bugs_found",
                error=f"bug_scope {bug_scope} matches no bug in the manifest (known ids: {known})",
                rationale="bug_scope matches no planted bug; check the case's bug_scope ids",
            )
        bugs = scoped
    lowered = _final_text(trace).lower()
    found = [b["id"] for b in bugs if any(m.lower() in lowered for m in b["match"])]
    value = len(found) / len(bugs) if bugs else 0.0
    return Feedback(name="planted_bugs_found", value=value, metadata={"found": found, "total": len(bugs)})


def _trace_case(trace, cases: dict[int, EvalCase], inputs: dict | None = None) -> EvalCase | None:
    """The evals.json case of a trace, by the `case_id` tag the runner sets (else the record's inputs)."""
    raw = (trace.info.tags or {}).get("case_id")
    if raw is None and isinstance(inputs, dict):
        raw = inputs.get("case_id")
    try:
        return cases.get(int(raw)) if raw is not None else None
    except (TypeError, ValueError):
        return None


def planted_bugs_check(trace, cases: dict[int, EvalCase], bugs: list[dict]) -> Feedback | None:
    """Scope rule: only cases whose fixtures include northwind/model.xlsx get a score."""
    case = _trace_case(trace, cases)
    if case is None or PLANTED_FIXTURE not in case.fixtures:
        return None
    return planted_bugs_feedback(trace, bugs, case.bug_scope)


def load_planted_bugs(fixtures_root: Path) -> list[dict]:
    return json.loads((Path(fixtures_root) / PLANTED_MANIFEST).read_text(encoding="utf-8"))["bugs"]


def make_planted_bugs_scorer(cases: dict[int, EvalCase], fixtures_root: Path):
    """Build the audit-xls scorer for one child run. The manifest is read once, here."""
    bugs = load_planted_bugs(fixtures_root)

    @scorer(name="planted_bugs_found")
    def planted_bugs_found(trace, expectations=None):
        return planted_bugs_check(trace, cases, bugs)

    return planted_bugs_found


def make_skill_trigger_scorer(cases: dict[int, EvalCase], skill_name: str):
    """The trigger scorer for one skill's child run. Labels come from the evals.json case, joined on case_id.

    It takes no `expectations`: a dataset row can hold a label from an older or unmerged evals.json."""

    @scorer(name="skill_trigger")
    def skill_trigger(trace, inputs=None):
        case = _trace_case(trace, cases, inputs)
        if case is None:
            return Feedback(
                name="skill_trigger", error="the trace's case_id is not a case of this evals.json"
            )
        return skill_trigger_feedback(trace, case.resolved_expected(skill_name))

    return skill_trigger
