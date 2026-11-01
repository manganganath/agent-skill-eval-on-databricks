from types import SimpleNamespace

import mlflow
import pytest
from mlflow import MlflowClient
from mlflow.entities import Feedback

from skillhub.agents import cases as agent_cases
from skillhub.agents.contract import AGENT_TRACES, CASE_ID, MODE, PROFILE, RUN_KEY, TRACE_KEY
from skillhub.evalspec import EvalCase
from skillhub.registry.stage import RunScope, StagedSkill
from tests.fakes import make_cfg
from tests.unit.agents.fake_agent import FakeAgent

CFG = make_cfg()
CASES = {
    1: EvalCase(id=1, prompt="Audit this model", should_trigger=True),
    2: EvalCase(id=2, prompt="What is EBITDA?", should_trigger=False, reviewed=False),
    101: EvalCase(
        id=101,
        kind="effectiveness",
        prompt="Audit model.xlsx",
        should_trigger=True,
        assertions=["Finds the #REF!"],
        fixtures=["northwind/model.xlsx"],
    ),  # fmt: skip
}


def skill(name, dept="common"):
    return StagedSkill(
        name, dept, f"skill_hub.run_k_common.{name}", f"skill_hub.common.{name}", "sha256:x", "e", "live"
    )


def ctx(tmp_path):
    scope = RunScope("common", ["skill_hub.run_k_common"], [skill("audit-xls"), skill("xlsx-author")])
    return SimpleNamespace(
        cfg=CFG, req=SimpleNamespace(run_key="hub_ab12cd34"), scope=scope, skill=scope.skills[0], cases=CASES,
        fixtures=SimpleNamespace(root=tmp_path), sql=object(),
    )  # fmt: skip


@pytest.fixture
def run_id():
    with mlflow.start_run() as run:
        yield run.info.run_id


@pytest.fixture
def agent(monkeypatch):
    a = FakeAgent(
        calls_skill=lambda case: (
            "skill_skill_hub_common_audit-xls" if case.prompt.startswith("Audit") else None
        )
    )
    monkeypatch.setattr(agent_cases, "wait_for_traces", a.wait_for_traces)
    return a


def test_each_case_repeat_goes_to_the_agent_with_the_contract_fields(tmp_path, agent, run_id):
    agent_cases.run_agent_cases(ctx(tmp_path), agent, "cc-sonnet", "trigger", [1, 2], 2, [], run_id)
    assert len(agent.seen) == 4
    first = agent.seen[0]
    assert first.skills_mcp == (f"{CFG.workspace.host}/ai-gateway/skills/?schema=skill_hub.common",)
    assert first.trace_table == "skill_hub.common_evals.traces_otel_spans" and first.max_turns == 3
    assert {c.attributes[RUN_KEY] for c in agent.seen} == {"hub_ab12cd34"}
    assert len({c.attributes[TRACE_KEY] for c in agent.seen}) == 1 and len(first.attributes[TRACE_KEY]) == 32
    assert {(c.attributes[MODE], c.attributes[PROFILE]) for c in agent.seen} == {("trigger", "cc-sonnet")}


def test_without_skill_the_agent_gets_no_skills_server(tmp_path, agent, run_id):
    agent_cases.run_agent_cases(ctx(tmp_path), agent, "cc-sonnet", "without_skill", [101], 1, [], run_id)
    [case] = agent.seen
    assert case.skills_mcp == () and case.max_turns == 8 and case.case is CASES[101]


def test_trigger_traces_are_scored_from_the_agents_skill_calls_and_the_scores_land_on_the_trace(
    tmp_path, agent, run_id
):
    traces = agent_cases.run_agent_cases(ctx(tmp_path), agent, "cc-sonnet", "trigger", [1, 2], 1, [], run_id)
    by_case = {t.info.tags["case_id"]: t for t in traces}
    assert set(by_case) == {"1", "2"} and by_case["2"].info.tags["reviewed"] == "false"
    [hit] = [a for a in by_case["1"].info.assessments if a.name == "skill_trigger"]
    assert (hit.value, hit.metadata["invoked_skill"]) == ("correct", "audit-xls")
    stored = mlflow.get_trace(by_case["2"].info.trace_id)
    assert [(a.name, a.value) for a in stored.info.assessments] == [("skill_trigger", "correct_reject")]


def test_judges_score_the_agents_answer_and_a_failing_judge_counts_as_no_answer(tmp_path, agent, run_id):
    seen = []

    class Judge:
        name = "assertions__gpt"

        def run(self, *, inputs, outputs, expectations, trace):
            seen.append((inputs, outputs, expectations["guidelines"]))
            return Feedback(name=self.name, value="yes")

    class Broken:
        name = "rubric_c1__gpt"

        def run(self, **kw):
            raise RuntimeError("judge down")

    [t] = agent_cases.run_agent_cases(
        ctx(tmp_path), agent, "cc", "with_skill", [101], 1, [Judge(), Broken()], run_id
    )
    assert seen == [({"prompt": "Audit model.xlsx", "case_id": 101}, "Done.", ["Finds the #REF!"])]
    got = {a.name: a for a in t.info.assessments}
    assert (
        got["assertions__gpt"].value == "yes"
        and got["rubric_c1__gpt"].error.error_message == "RuntimeError: judge down"
    )


def test_a_failed_call_or_a_trace_that_never_arrived_leaves_the_case_without_a_trace(
    tmp_path, monkeypatch, run_id
):
    agent = FakeAgent(fail_cases={1}, no_trace_cases={2})
    monkeypatch.setattr(agent_cases, "wait_for_traces", agent.wait_for_traces)
    assert agent_cases.run_agent_cases(ctx(tmp_path), agent, "cc", "trigger", [1, 2], 1, [], run_id) == []


def test_the_child_run_records_which_trace_is_which_case_and_the_tokens(tmp_path, agent, run_id):
    traces = agent_cases.run_agent_cases(ctx(tmp_path), agent, "cc", "trigger", [1, 2], 2, [], run_id)
    listed = mlflow.artifacts.load_dict(f"runs:/{run_id}/{AGENT_TRACES}")["traces"]
    assert sorted((e["case_id"], e["repeat"]) for e in listed) == [(1, 0), (1, 1), (2, 0), (2, 1)]
    assert {e["trace_id"] for e in listed} == {t.info.trace_id for t in traces}
    assert {e["mode"] for e in listed} == {"trigger"} and listed[0]["prompt"] in (
        "Audit this model",
        "What is EBITDA?",
    )
    metrics = MlflowClient().get_run(run_id).data.metrics
    assert (metrics["agent_input_tokens"], metrics["agent_output_tokens"]) == (400.0, 40.0)


def test_an_assessment_that_cannot_be_written_makes_the_case_an_error(tmp_path, agent, run_id, monkeypatch):
    def refuse(**kw):
        raise RuntimeError("PERMISSION_DENIED")

    monkeypatch.setattr(agent_cases.mlflow, "log_assessment", refuse)
    assert agent_cases.run_agent_cases(ctx(tmp_path), agent, "cc", "trigger", [1], 1, [], run_id) == []


def test_the_agents_case_attributes_name_the_case_and_repeat(tmp_path, agent, run_id):
    agent_cases.run_agent_cases(ctx(tmp_path), agent, "cc", "trigger", [1], 2, [], run_id)
    assert sorted(c.attributes[CASE_ID] + "/" + c.attributes["skillhub.repeat"] for c in agent.seen) == [
        "1/0",
        "1/1",
    ]
