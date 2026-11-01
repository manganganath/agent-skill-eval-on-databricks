"""An external agent for tests that writes an agent-shaped trace per case into the test MLflow store. Not a test
module. `found` stands in for the spans table: (case_id, repeat) -> trace ID, filled as the agent answers."""

from __future__ import annotations

import threading

from skillhub.agents.contract import CASE_ID, REPEAT, AgentAnswer, AgentCase
from tests.unit.agents.agent_traces import build_trace


class FakeAgent:
    harness = "claude-code"

    def __init__(self, calls_skill=None, answer="Done.", fail_cases=(), no_trace_cases=(), concurrency=2):
        self.calls_skill = calls_skill or (lambda case: None)  # case -> tool name the agent calls, or None
        self.answer = answer
        self.fail_cases = set(fail_cases)
        self.no_trace_cases = set(no_trace_cases)
        self.concurrency = concurrency
        self.seen: list[AgentCase] = []
        self.found: dict[tuple[int, int], str] = {}
        self._lock = threading.Lock()

    def run(self, case: AgentCase) -> AgentAnswer:
        cell = (int(case.attributes[CASE_ID]), int(case.attributes[REPEAT]))
        with self._lock:
            self.seen.append(case)
        if cell[0] in self.fail_cases:
            return AgentAnswer("", "exit code 1: the agent stopped")
        tool = self.calls_skill(case)
        children = [
            {"name": "claude_code.llm_request", "attributes": {"input_tokens": 100, "output_tokens": 10}}
        ]
        if tool:
            children.append({"name": "claude_code.tool", "attributes": {"gen_ai.tool.name": tool}})
        trace_id = build_trace({"name": "claude_code.interaction", "attributes": dict(case.attributes),
                                "children": children})  # fmt: skip
        if cell[0] not in self.no_trace_cases:
            with self._lock:
                self.found[cell] = trace_id
        return AgentAnswer(self.answer)

    def wait_for_traces(self, sql, table, expected, **kw):
        return {cell: tid for cell, tid in self.found.items() if cell in expected}
