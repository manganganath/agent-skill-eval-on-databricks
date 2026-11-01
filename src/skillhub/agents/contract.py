"""What the eval Job hands an external agent for one case-repeat, and what it gets back.

The agent loads skills from the Unity Gateway skills MCP (one URL per schema: the department's first, then common's),
except in `without_skill` mode, and runs the case on its own. It puts the `attributes` on every span it exports, and
sends its spans with OTLP/HTTP to `https://<host>/api/2.0/otel/v1/traces` with the header
`X-Databricks-UC-Table-Name: <trace_table>`. It returns its final answer as text. The Job finds the trace by its
attributes (`skillhub.agents.traces`), so the agent needs no MLflow code.

External agents read the live skills: the department's and common's live schemas. Their tool names are therefore the
production names, as in the built-in agent loop, and only a `live` run without pinned versions can use them.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from skillhub.config import HubConfig
from skillhub.evalspec import EvalCase
from skillhub.gateway.skills_mcp import build_url
from skillhub.naming import evals_schema, live_schema
from skillhub.registry.stage import visible_depts

MAX_TURNS = 8  # the effectiveness modes, as in the built-in agent loop
TRIGGER_TURNS = 3  # trigger mode: the first skill call decides, so a short run is enough
OTLP_PATH = "/api/2.0/otel/v1/traces"
TABLE_HEADER = "X-Databricks-UC-Table-Name"
SPANS_TABLE = "traces_otel_spans"  # MLflow's spans table in each <d>_evals schema (table prefix `traces`)
# The child-run artifact that lists which agent trace is which case: sync reads the traces back by these IDs.
AGENT_TRACES = "agent_traces.json"
LIVE_ONLY = (
    "external agents evaluate live skills only: they load skills by their production names, so a candidate, a "
    "draft or a pinned version cannot be shown to them"
)
# Attribute names. Values are letters, digits, `_` and `-`, so they fit OTEL_RESOURCE_ATTRIBUTES as they are.
RUN_KEY, TRACE_KEY, CASE_ID, REPEAT, MODE, PROFILE = (
    "skillhub.run_key",
    "skillhub.trace_key",
    "skillhub.case_id",
    "skillhub.repeat",
    "skillhub.mode",
    "skillhub.profile",
)
_VALUE = re.compile(r"[A-Za-z0-9_-]{1,128}")


@dataclass(frozen=True)
class AgentCase:
    """One case-repeat, as an agent gets it."""

    prompt: str
    mode: str  # trigger, with_skill or without_skill
    skills_mcp: tuple[str, ...]  # empty in without_skill mode: the agent must not connect to the skills MCP
    attributes: Mapping[str, str]  # put on every span
    trace_table: str  # <catalog>.<dept>_evals.traces_otel_spans
    max_turns: int
    case: EvalCase | None = None  # the evals.json case: its fixture files are the agent's only files
    fixtures_root: Path | None = None  # the run's fixture snapshot, which holds those files


@dataclass(frozen=True)
class AgentAnswer:
    """The agent's final answer, or why there is none. `error` makes the case an error, never a fail."""

    text: str
    error: str | None = None
    seconds: float = 0.0


class ExternalAgent(Protocol):
    harness: str
    concurrency: int  # cases at once in one process

    def run(self, case: AgentCase) -> AgentAnswer: ...


def trace_table(cfg: HubConfig, dept: str) -> str:
    return f"{evals_schema(cfg, dept)}.{SPANS_TABLE}"


def skills_urls(cfg: HubConfig, dept: str) -> tuple[str, ...]:
    """One skills MCP URL per live schema the department's scope reads: its own first, then common's. The gateway
    offers at most 20 per-skill tools per connection, so each schema gets its own connection."""
    return tuple(build_url(cfg.workspace.host, [live_schema(cfg, d)]) for d in visible_depts(dept))


def otlp_endpoint(host: str) -> str:
    return host.rstrip("/") + OTLP_PATH


def turns(mode: str) -> int:
    return TRIGGER_TURNS if mode == "trigger" else MAX_TURNS


def attributes(
    *, run_key: str, trace_key: str, case_id: int, repeat: int, mode: str, profile: str
) -> dict[str, str]:
    values = {
        RUN_KEY: run_key,
        TRACE_KEY: trace_key,
        CASE_ID: str(case_id),
        REPEAT: str(repeat),
        MODE: mode,
        PROFILE: profile,
    }
    bad = [k for k, v in values.items() if not _VALUE.fullmatch(v)]
    if bad:
        raise ValueError(f"trace attribute values must be letters, digits, _ and -: {', '.join(bad)}")
    return values
