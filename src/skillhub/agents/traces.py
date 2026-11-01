"""Finding and reading the OpenTelemetry traces that external agents send to a department's trace table.

- **Finding.** The trace attributes are not in MLflow's trace metadata: they sit in the spans table, as resource
  attributes (Claude Code puts OTEL_RESOURCE_ATTRIBUTES there) or as span attributes (a Strands agent's trace
  attributes). So the traces of a child run are found in SQL, by the run key and the child run's random trace key,
  and each is then read with `mlflow.get_trace`. A trace counts once its root span is in the table: the root ends
  last, so its children were exported before it. A retried case can leave two traces; the newest is kept.
- **Reading.** The invoked skill is the first span, by start time, whose `gen_ai.tool.name` is a skill's tool. Agents
  name a skill tool differently: `skill_<catalog>.<schema>.<name>` as the gateway lists it, with the dots turned into
  underscores (Claude Code), and shortened with a hash when a model allows only 64 characters (Bedrock). A name
  that an agent cut short still matches when it is the start of exactly one skill's tool name. Token counts come
  from the model-call spans only, because an agent's root span repeats the totals.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from mlflow.entities import SpanType

from skillhub.agents.contract import CASE_ID, REPEAT, RUN_KEY, SPANS_TABLE, TRACE_KEY
from skillhub.gateway.bedrock import tool_name as bedrock_tool_name
from skillhub.sql import SqlExecutor

log = logging.getLogger(__name__)

TRACE_WAIT_S = 300  # how long a child run waits for its agents' traces after the last case has answered
TRACE_POLL_S = 10
SKILL_TOOL = "skill_"
TOOL_NAME = "gen_ai.tool.name"
_MCP_PREFIX = re.compile(r"mcp__[A-Za-z0-9-]+__")  # a Claude Code tool name keeps its server's name in front
_TABLE = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+\." + SPANS_TABLE)
_MODEL_SPANS = frozenset({"claude_code.llm_request"})
_MODEL_TYPES = frozenset({SpanType.CHAT_MODEL, SpanType.LLM})
_MODEL_OPERATIONS = frozenset({"chat", "text_completion", "generate_content"})  # gen_ai.operation.name
_GENAI_INPUT = "gen_ai.usage.input_tokens"  # the GenAI convention's input already includes cached input
_CLAUDE_INPUT = (
    "input_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
)  # Claude Code counts them apart
_OUTPUT_TOKENS = ("gen_ai.usage.output_tokens", "output_tokens")

Cell = tuple[int, int]  # (case_id, repeat)


def tool_names(fqns: Iterable[str]) -> dict[str, str]:
    """Each name that an agent may give a skill's tool -> that skill's name. `fqns` are the production FQNs."""
    out: dict[str, str] = {}
    for fqn in fqns:
        skill = fqn.rsplit(".", 1)[-1]
        raw = SKILL_TOOL + fqn
        flat = raw.replace(".", "_")
        for name in (raw, flat, bedrock_tool_name(flat), bedrock_tool_name(flat, plain=True)):
            out.setdefault(name, skill)
    return out


def _attr(span: Any, key: str) -> Any:
    return (getattr(span, "attributes", None) or {}).get(key)


def _skill_of(tool: str, names: Mapping[str, str]) -> str | None:
    tool = _MCP_PREFIX.sub("", tool, count=1) if tool.startswith("mcp__") else tool
    if tool in names:
        return names[tool]
    if not tool.startswith(SKILL_TOOL):
        return None
    skills = {skill for name, skill in names.items() if name.startswith(tool)}
    return skills.pop() if len(skills) == 1 else None


def skill_calls(trace: Any, names: Mapping[str, str]) -> list[str]:
    """The skills the agent called, in call order: tool spans whose `gen_ai.tool.name` is a skill's tool."""
    spans = sorted(trace.data.spans or [], key=lambda s: getattr(s, "start_time_ns", 0) or 0)
    out: list[str] = []
    for span in spans:
        tool = _attr(span, TOOL_NAME)
        skill = _skill_of(tool, names) if isinstance(tool, str) else None
        if skill is not None:
            out.append(skill)
    return out


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def token_usage(trace: Any) -> tuple[int, int]:
    """(input tokens, output tokens) over the trace's model-call spans. Input counts cached input too: a Claude Code
    span carries new, cache-read and cache-created input apart, and all three are summed."""
    tokens_in = tokens_out = 0
    for span in trace.data.spans or []:
        model_call = (
            span.name in _MODEL_SPANS
            or getattr(span, "span_type", None) in _MODEL_TYPES
            or _attr(span, "gen_ai.operation.name") in _MODEL_OPERATIONS
        )
        if not model_call:
            continue
        if _attr(span, _GENAI_INPUT) is not None:
            tokens_in += _int(_attr(span, _GENAI_INPUT))
        else:
            tokens_in += sum(_int(_attr(span, k)) for k in _CLAUDE_INPUT)
        tokens_out += next((_int(_attr(span, k)) for k in _OUTPUT_TOKENS if _attr(span, k) is not None), 0)
    return tokens_in, tokens_out


def _key(name: str) -> str:
    """One trace attribute, from the resource attributes, else the span's own."""
    path = f"'$[\"{name}\"]'"
    return f"coalesce(variant_get(resource.attributes, {path}, 'string'), variant_get(attributes, {path}, 'string'))"


def find_sql(table: str) -> str:
    if not _TABLE.fullmatch(table):
        raise ValueError(f"not a trace spans table: {table!r}")
    return (
        f"SELECT trace_id, max({_key(CASE_ID)}) AS case_id, max({_key(REPEAT)}) AS repeat, "
        "max(CASE WHEN parent_span_id IS NULL OR parent_span_id = '' THEN 1 ELSE 0 END) AS has_root, "
        "min(start_time_unix_nano) AS started_ns "
        f"FROM {table} WHERE start_time_unix_nano >= :since_ns "
        f"AND {_key(RUN_KEY)} = :run_key AND {_key(TRACE_KEY)} = :trace_key GROUP BY trace_id"
    )


def mlflow_trace_id(table: str, otel_trace_id: str) -> str:
    """The MLflow ID of a trace in the spans table: trace:/<catalog>.<schema>.traces/<OTel trace ID>."""
    return f"trace:/{table.removesuffix('_otel_spans')}/{otel_trace_id}"


def find_traces(
    sql: SqlExecutor, table: str, *, run_key: str, trace_key: str, since_ns: int
) -> dict[Cell, str]:
    """The MLflow trace ID of each (case, repeat) whose trace has arrived whole: its root span is in the table."""
    rows = sql.execute(find_sql(table), {"since_ns": since_ns, "run_key": run_key, "trace_key": trace_key})
    best: dict[Cell, tuple[int, str]] = {}
    for row in rows:
        try:
            cell = (int(row["case_id"]), int(row["repeat"]))
        except (KeyError, TypeError, ValueError):
            continue
        if not _int(row.get("has_root")) or not row.get("trace_id"):
            continue
        started = _int(row.get("started_ns"))
        if cell not in best or started > best[cell][0]:
            best[cell] = (started, str(row["trace_id"]))
    return {cell: mlflow_trace_id(table, tid) for cell, (_, tid) in best.items()}


def wait_for_traces(
    sql: SqlExecutor,
    table: str,
    expected: set[Cell],
    *,
    run_key: str,
    trace_key: str,
    since_ns: int,
    timeout_s: float = TRACE_WAIT_S,
    poll_s: float = TRACE_POLL_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[Cell, str]:
    """Poll until every expected cell has a trace, or until the timeout. Then return what has arrived."""
    start = clock()
    found: dict[Cell, str] = {}
    while True:
        try:
            found = find_traces(sql, table, run_key=run_key, trace_key=trace_key, since_ns=since_ns)
        except Exception as exc:  # the cases have run: one failed poll must not lose them
            log.warning("polling %s for agent traces failed (%s); trying again", table, type(exc).__name__)
        missing = expected - set(found)
        if not missing:
            return found
        if clock() - start >= timeout_s:
            log.warning(
                "%d of %d agent traces did not reach %s within %ss: %s",
                len(missing),
                len(expected),
                table,
                int(timeout_s),
                sorted(missing),
            )
            return found
        sleep(poll_s)
