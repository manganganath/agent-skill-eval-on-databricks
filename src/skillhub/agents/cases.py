"""One child run's cases on an external agent: invoke the agent, find its traces, score them, and record them.

1. Each (case, repeat) goes to the agent as a `contract.AgentCase`, up to `agent.concurrency` at once. A case whose
   call fails is an error.
2. The traces are found in the department's spans table by the run key and a random trace key that only this child
   run knows (`traces.wait_for_traces`), then read with `mlflow.get_trace`. A case whose trace has not arrived
   within `traces.TRACE_WAIT_S` is an error.
3. The scorers run on each trace with the case's inputs, the agent's answer and the evals.json expectations. Each
   result is logged with `mlflow.log_assessment` on the agent's own trace. In trigger mode the scorer reads the
   skill calls from the agent's tool spans (`scorers.make_agent_trigger_scorer`).
4. The child run records which trace is which case in its `agent_traces.json` artifact, which sync reads: the traces
   were not created inside the run, so a search by run would not find them. The traces are also linked to the run
   where the tracking store allows it, so that the MLflow run page lists them.

The traces are returned with the case's tags and their new assessments set in memory, as the built-in agent loop's
traces come back from MLflow, so the runner turns them into case scores with the same code.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import mlflow
from mlflow import MlflowClient
from mlflow.entities import AssessmentError, AssessmentSource, AssessmentSourceType, Feedback

from skillhub.agents.contract import (
    AGENT_TRACES,
    AgentAnswer,
    AgentCase,
    ExternalAgent,
    attributes,
    skills_urls,
    trace_table,
    turns,
)
from skillhub.agents.traces import Cell, token_usage, tool_names, wait_for_traces
from skillhub.eval.scorers import make_agent_trigger_scorer
from skillhub.results.datasets import case_record
from skillhub.retry import is_transient, retrying

log = logging.getLogger(__name__)

SINCE_SLACK_NS = 300 * 10**9  # spans older than the child run by more than this are not read
SCORER_WORKERS = 2  # judges at once per case, as in the runner
CALL_ATTEMPTS = 4  # a trace read, a judge call or an assessment write, after a rate limit
CALL_BACKOFF_S = 2.0
CALL_BACKOFF_MAX_S = 30.0
MESSAGE_MAX = 500
LINK_BATCH = 100  # MLflow links at most 100 traces per call
DEFAULT_FEEDBACK_NAME = "feedback"


def _retry(fn: Callable[[], Any], what: str) -> Any:
    return retrying(
        fn,
        attempts=CALL_ATTEMPTS,
        base_s=CALL_BACKOFF_S,
        cap_s=CALL_BACKOFF_MAX_S,
        retry_on=is_transient,
        what=what,
    )


def _feedbacks(scorer: Any, *, inputs: dict, outputs: str, expectations: dict, trace: Any) -> list[Feedback]:
    """What one scorer says about one trace, as Feedback. A scorer that fails gives a Feedback with its error, as
    `mlflow.genai.evaluate` records it, so the case counts the judge as one that did not answer."""
    name = getattr(scorer, "name", DEFAULT_FEEDBACK_NAME)
    try:
        value = _retry(
            lambda: scorer.run(inputs=inputs, outputs=outputs, expectations=expectations, trace=trace),
            f"scorer {name}",
        )
    except Exception as exc:
        error = AssessmentError(
            error_code="SCORER_ERROR", error_message=f"{type(exc).__name__}: {exc}"[:MESSAGE_MAX]
        )
        return [Feedback(name=name, error=error)]
    out: list[Feedback] = []
    for v in value if isinstance(value, list) else [value]:
        if v is None:
            continue
        if not isinstance(v, Feedback):
            v = Feedback(name=name, value=v, source=AssessmentSource(AssessmentSourceType.CODE, name))
        elif v.name in (None, "", DEFAULT_FEEDBACK_NAME):
            v.name = name
        out.append(v)
    return out


def _case_tags(case: Any, repeat: int, mode: str) -> dict[str, str]:
    return {
        "case_id": str(case.id),
        "repeat": str(repeat),
        "mode": mode,
        "reviewed": str(case.reviewed).lower(),
    }


def run_agent_cases(
    ctx: Any,
    agent: ExternalAgent,
    profile: str,
    mode: str,
    case_ids: list[int],
    repeats: int,
    scorers: list,
    run_id: str,
) -> list:
    """Every (case, repeat) of the mode on the agent. Returns the scored traces, one per case that has one.

    `ctx` is the runner's skill context: its cfg, req, scope, skill, cases, fixtures and sql are read."""
    cfg, dept = ctx.cfg, ctx.scope.dept
    table = trace_table(cfg, dept)
    trace_key = secrets.token_hex(16)  # only this process knows it, so no one else's spans can fill a case
    since_ns = time.time_ns() - SINCE_SLACK_NS
    urls = () if mode == "without_skill" else skills_urls(cfg, dept)
    cells: list[Cell] = [(cid, rep) for cid in case_ids for rep in range(repeats)]

    def invoke(cell: Cell) -> tuple[Cell, AgentAnswer]:
        cid, rep = cell
        case = ctx.cases[cid]
        attrs = attributes(
            run_key=ctx.req.run_key, trace_key=trace_key, case_id=cid, repeat=rep, mode=mode, profile=profile
        )
        job = AgentCase(
            prompt=case.prompt,
            mode=mode,
            skills_mcp=urls,
            attributes=attrs,
            trace_table=table,
            max_turns=turns(mode),
            case=case,
            fixtures_root=ctx.fixtures.root,
        )
        try:
            return cell, agent.run(job)
        except Exception as exc:  # an agent that raises is an error of this case only
            return cell, AgentAnswer("", f"{type(exc).__name__}: {exc}"[:MESSAGE_MAX])

    with ThreadPoolExecutor(max_workers=max(1, agent.concurrency), thread_name_prefix="agent-case") as pool:
        answers = dict(pool.map(invoke, cells))
    for (cid, rep), answer in sorted(answers.items()):
        if answer.error is not None:
            log.warning("%s %s case %s repeat %s: %s", profile, mode, cid, rep, answer.error)
    answered = {cell for cell, a in answers.items() if a.error is None}
    found = (
        wait_for_traces(
            ctx.sql, table, answered, run_key=ctx.req.run_key, trace_key=trace_key, since_ns=since_ns
        )
        if answered
        else {}
    )
    if mode == "trigger":
        fqns = [s.production_fqn for s in [*ctx.scope.skills, *ctx.scope.hidden]]
        scorers = [make_agent_trigger_scorer(ctx.cases, ctx.skill.name, tool_names(fqns))]

    def score(cell: Cell) -> Any:
        cid, rep = cell
        trace_id = found[cell]
        trace = _retry(lambda: mlflow.get_trace(trace_id), f"read trace {trace_id}")
        if trace is None:
            log.warning(
                "%s %s case %s repeat %s: trace %s could not be read", profile, mode, cid, rep, trace_id
            )
            return None
        case = ctx.cases[cid]
        trace.info.tags.update(_case_tags(case, rep, mode))  # in memory, as the runner's traces carry them
        record = case_record(case, ctx.skill.name)
        got: list[Feedback] = []
        with ThreadPoolExecutor(max_workers=SCORER_WORKERS, thread_name_prefix="agent-judge") as judges:
            for fbs in judges.map(
                lambda s: _feedbacks(
                    s,
                    inputs=record["inputs"],
                    outputs=answers[cell].text,
                    expectations=record["expectations"],
                    trace=trace,
                ),
                scorers,
            ):
                got += fbs
        try:
            logged = [
                _retry(
                    lambda fb=fb: mlflow.log_assessment(trace_id=trace_id, assessment=fb), "log assessment"
                )
                for fb in got
            ]
        except Exception as exc:  # unrecorded scores would leave the case without rows: it is an error
            log.warning("%s %s case %s repeat %s: assessments not logged: %s", profile, mode, cid, rep, exc)
            return None
        trace.info.assessments = [*(trace.info.assessments or []), *logged]
        return trace

    scored = [cell for cell in sorted(found) if cell in answered]
    concurrency = max(1, ctx.cfg.defaults.concurrency_per_endpoint)
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="agent-score") as pool:
        traces = list(pool.map(score, scored))
    kept = [(cell, t) for cell, t in zip(scored, traces, strict=True) if t is not None]
    _record(run_id, kept, mode, ctx)
    return [t for _, t in kept]


def _record(run_id: str, kept: list[tuple[Cell, Any]], mode: str, ctx: Any) -> None:
    client = MlflowClient()
    entries = [
        {
            "trace_id": t.info.trace_id,
            "case_id": cid,
            "repeat": rep,
            "mode": mode,
            "reviewed": bool(ctx.cases[cid].reviewed),
            "prompt": ctx.cases[cid].prompt,
        }
        for (cid, rep), t in kept
    ]
    client.log_dict(run_id, {"traces": entries}, AGENT_TRACES)
    tokens = [token_usage(t) for _, t in kept]
    client.log_metric(run_id, "agent_input_tokens", float(sum(i for i, _ in tokens)))
    client.log_metric(run_id, "agent_output_tokens", float(sum(o for _, o in tokens)))
    ids = [e["trace_id"] for e in entries]
    for i in range(0, len(ids), LINK_BATCH):
        try:
            client.link_traces_to_run(ids[i : i + LINK_BATCH], run_id=run_id)
        except Exception as exc:  # the run page then lists no traces; sync reads agent_traces.json
            log.info("agent traces not linked to run %s: %s", run_id, type(exc).__name__)
            return
