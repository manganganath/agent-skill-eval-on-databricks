import json
import os
import sys
import threading
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import mlflow
import mlflow.openai
import pandas as pd
import pytest
from mlflow import MlflowClient
from typer.testing import CliRunner

from skillhub import cli
from skillhub.agent.fixtures import FixtureStore
from skillhub.bundle import SkillBundle
from skillhub.config import ModelProfile
from skillhub.eval import runner
from skillhub.eval.runner import EvalRunError, RunRequest, RunSummary, run_eval
from skillhub.evalspec import Criteria, Criterion, EvalCase, EvalFile, Rubric
from skillhub.gateway.models import ChatResult, ToolCall
from skillhub.gateway.toolnames import ToolNameMap
from skillhub.prompts import PromptRef, load_packaged
from skillhub.registry.stage import RunScope, StagedSkill
from skillhub.results.datasets import case_record
from tests.fakes import FakeModelClient, FakeSkillsMcp

RUN_KEY = "pr12_ab12cd3"
REPEATS = 2
PROFILE = ModelProfile(route="serving", endpoint="databricks-gpt-5-5", **{"class": "frontier"})
CLAUDE = ModelProfile(route="serving", endpoint="databricks-claude-sonnet-5-5", **{"class": "frontier"})
CFG = SimpleNamespace(
    catalog="cat",
    workspace=SimpleNamespace(host="https://ws.example.test", sql_warehouse_id="wh"),
    models={"gpt": PROFILE, "claude": CLAUDE},
    judges=["gpt"],
    judges_pr=["gpt"],
    generator="gpt",
    defaults=SimpleNamespace(
        repeats=REPEATS,
        concurrency_per_endpoint=2,
        fail_rate=0.5,
        warn_rate=0.8,
        max_error_rate=0.1,
        min_reviewed_cases=1,
        pass_score=0.7,
        models=["gpt"],
    ),
)
REQUIRE_RESULTS = runner._require_results  # the wired fixture replaces it
EQ = f"cat.run_{RUN_KEY}_dept_equity_research"
CO = f"cat.run_{RUN_KEY}_common"


def staged(name, dept, schema, origin="candidate"):
    base = "common" if dept == "common" else f"dept_{dept}"
    return StagedSkill(
        name=name,
        dept=dept,
        staged_fqn=f"{schema}.{name}",
        production_fqn=f"cat.{base}.{name}",
        content_hash=f"sha256:{name}",
        etag=f"etag-{name}",
        origin=origin,
    )


# A department scope holds its own skills plus common; common sees only common.
SCOPES = [
    RunScope(dept="common", schema_fqn=CO, skills=[staged("audit-xls", "common", CO, "live")]),
    RunScope(
        dept="equity_research",
        schema_fqn=EQ,
        skills=[staged("earnings-preview", "equity_research", EQ), staged("audit-xls", "common", EQ, "live")],
    ),
]
BUNDLES = [
    SkillBundle(dept="common", name="audit-xls", root=Path("/x/common/audit-xls"), files={}, frontmatter={}),
    SkillBundle(
        dept="equity_research",
        name="earnings-preview",
        root=Path("/x/equity_research/earnings-preview"),
        files={},
        frontmatter={},
    ),
]
TRIGGER = [
    EvalCase(id=1, prompt="p1", should_trigger=True),
    EvalCase(id=2, prompt="p2", should_trigger=False),
]
EFFECT = [
    EvalCase(
        id=101,
        kind="effectiveness",
        prompt="p101",
        should_trigger=True,
        assertions=["Mentions NVDA"],
        fixtures=["northwind/model.xlsx"],
    ),
    EvalCase(id=102, kind="effectiveness", prompt="p102", should_trigger=True),
]
CASES = TRIGGER + EFFECT
CRITERIA = [
    Criterion(
        id=f"c{i}",
        text="States EPS",
        type="task_quality" if i <= 3 else "skill_format",
        weight=1,
        source="SKILL.md",
    )
    for i in range(1, 6)
]


def eval_files(cases=CASES):
    return {n: EvalFile(skill_name=n, evals=list(cases)) for n in ("earnings-preview", "audit-xls")}


RUBRICS = {
    n: Rubric(
        skill_name=n,
        source_hash="sha256:r",
        generated_by="gpt",
        pass_score=0.7,
        reviewed=True,
        criteria=CRITERIA,
    )
    for n in ("earnings-preview", "audit-xls")
}


def record(c, skill="earnings-preview"):
    return case_record(c, skill)


DATA = [record(c) for c in CASES]


def offered_for(schemas):
    """What the skills server offers for these schemas: one skill_<fqn> tool per staged skill."""
    return {
        f"skill_{s.staged_fqn}": (
            f"Load {s.staged_fqn} from {sc.schema_fqn}",
            f"SKILL BODY of {s.staged_fqn}",
        )
        for sc in SCOPES
        for s in sc.skills
        if sc.schema_fqn in schemas
    }


class TextModel:
    profile_name = "gpt"

    def __init__(self):
        self.seen = []

    def chat(self, messages, tools, *, max_tokens=None, trigger=False):
        self.seen.append({"messages": messages, "tools": [(t.name, t.description) for t in tools]})
        return ChatResult(
            text="ok",
            tool_calls=[],
            finish_reason="stop",
            usage={"prompt_tokens": 1, "completion_tokens": 1},
            assistant_message={"role": "assistant", "content": "ok"},
        )


class SkillCallingModel(TextModel):
    """Calls the first offered skill tool, then answers, so with_skill and trigger traces get a TOOL span."""

    def chat(self, messages, tools, *, max_tokens=None, trigger=False):
        skill_tools = [t.name for t in tools if t.name.startswith("skill_")]
        if skill_tools and messages[-1]["role"] != "tool":
            call = ToolCall(id="c1", name=skill_tools[0], arguments={})
            message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": call.name, "arguments": "{}"}}
                ],
            }
            return ChatResult("", [call], "tool_calls", {"prompt_tokens": 1, "completion_tokens": 1}, message)
        return super().chat(messages, tools, max_tokens=max_tokens, trigger=trigger)


def A(name, value, metadata=None, error=None):
    return SimpleNamespace(name=name, value=value, metadata=metadata, error=error)


def fake_trace(case_id, rep, mode, assessments, failed=False, timestamp_ms=1000):
    return SimpleNamespace(
        info=SimpleNamespace(
            tags={"case_id": str(case_id), "repeat": str(rep), "mode": mode},
            state="ERROR" if failed else "OK",
            assessments=assessments,
            timestamp_ms=timestamp_ms,
        )
    )


def default_assessments(skill, mode, case, rep):
    if mode == "trigger":
        if case.should_trigger:
            return [A("skill_trigger", "correct", {"invoked_skill": skill, "expected_skill": skill})]
        return [A("skill_trigger", "correct_reject", {"invoked_skill": None, "expected_skill": None})]
    out = [A(f"rubric_{c.id}__gpt", True) for c in CRITERIA]
    if case.assertions:
        out.append(A("assertions__gpt", "yes"))
    if skill == "audit-xls" and "northwind/model.xlsx" in case.fixtures:
        out.append(A("planted_bugs_found", 0.5))
    return out


@pytest.fixture
def wired(tmp_path, monkeypatch):
    state = {
        "dropped": [],
        "synced": [],
        "evaluate_calls": 0,
        "model": TextModel(),
        "stage": {},
        "tags": [],
        "rubrics": [],
        "eval_runs": [],
        "evaluated": [],
        "rows": [],
        "dataset": DATA,
        "expected": [],
        "env": [],
        "mcp": lambda host, schemas, headers: FakeSkillsMcp(offered_for(schemas)),
        "scopes": SCOPES,
        "eval_files": eval_files(),
        "assess": default_assessments,
        "scorers": lambda cfg, rubric, profiles, mode, *, skill_name, cases=None, fixtures_root=None: (
            [SimpleNamespace(name="planted_bugs_found")]
            if skill_name == "audit-xls" and mode != "trigger"
            else []
        ),
    }

    def ensure_experiment(cfg, dept, skill):
        name = f"/t2-test/{tmp_path.name}/{dept}/{skill}"
        found = mlflow.get_experiment_by_name(name)
        if found:
            return found.experiment_id
        return mlflow.create_experiment(name, artifact_location=(tmp_path / "artifacts").as_uri())

    def fake_stage(cfg, uc, journal, run_key, candidates, scope_depts=None, versions=None):
        state["stage"] = {
            "candidates": [c.name for c in candidates],
            "versions": versions,
            "scope_depts": scope_depts,
        }
        return state["scopes"]

    def fake_traces(exp_id, run_id, expected, **kw):
        state["expected"].append(expected)
        params = MlflowClient().get_run(run_id).data.params
        mode, skill = params["mode"], params["skill"]
        kind = "trigger" if mode == "trigger" else "effectiveness"
        out = []
        for rep in range(REPEATS):
            for case in (c for c in state["eval_files"][skill].evals if c.kind == kind):
                got = state["assess"](skill, mode, case, rep)
                if got is None:  # the trace never arrived
                    continue
                failed = got == "ERROR"  # a failed final attempt still carries the dataset expectations
                out.append(
                    fake_trace(case.id, rep, mode, [A("should_trigger", True)] if failed else got, failed)
                )
        return out

    def fake_evaluate(data, predict_fn, scorers):
        state["evaluate_calls"] += 1
        run_id = mlflow.active_run().info.run_id
        state["eval_runs"].append(run_id)
        state["evaluated"].append((run_id, sorted(row["inputs"]["case_id"] for row in data)))
        state["rows"].append((run_id, list(data)))
        state["env"].append(
            {k: os.environ.get(k) for k in (runner.SKIP_TRACE_VALIDATION, runner.MAX_SCORER_WORKERS)}
        )
        for row in data:
            predict_fn(**row["inputs"])
        return SimpleNamespace()

    monkeypatch.setattr(runner, "workspace_client", lambda: object())
    monkeypatch.setattr(runner, "SqlClient", lambda *a, **k: object())
    monkeypatch.setattr(runner, "UCSkills", lambda w: object())
    monkeypatch.setattr(
        runner, "Journal", lambda cfg, sql: SimpleNamespace(version_by_hash=lambda d, s, h: None)
    )
    monkeypatch.setattr(runner, "discover_bundles", lambda *a, **k: BUNDLES)
    monkeypatch.setattr(runner, "stage", fake_stage)
    monkeypatch.setattr(
        runner, "record_rubric", lambda cfg, sql, dept, rubric: state["rubrics"].append((dept, rubric))
    )
    monkeypatch.setattr(runner, "ensure_experiment", ensure_experiment)
    monkeypatch.setattr(
        runner, "sync_dataset", lambda cfg, dept, skill, ef, exp: (state["dataset"], "digest-1")
    )
    monkeypatch.setattr(runner, "sync_run", lambda cfg, sql, run_key, **k: state["synced"].append(run_key))
    monkeypatch.setattr(runner, "drop_run_schemas", lambda cfg, w, run_key: state["dropped"].append(run_key))
    monkeypatch.setattr(
        runner, "load_prompt", lambda cfg, key: PromptRef(key, f"cat.eval.{key}", 1, load_packaged(key))
    )
    monkeypatch.setattr(runner, "model_client", lambda cfg, profile, headers: state["model"])
    monkeypatch.setattr(runner, "SkillsMcpClient", lambda *a: state["mcp"](*a))

    def read_evals(path):
        got = state["eval_files"][Path(path).parts[-3]]
        if isinstance(got, Exception):
            raise got
        return got, got.model_dump_json().encode()

    monkeypatch.setattr(runner, "read_evals", read_evals)
    monkeypatch.setattr(runner, "_require_results", lambda: None)  # the results package is not needed here
    monkeypatch.setattr(runner, "load_rubric", lambda path: RUBRICS[Path(path).parts[-3]])
    monkeypatch.setattr(runner, "scorers_for", lambda *a, **k: state["scorers"](*a, **k))
    monkeypatch.setattr(runner, "_tag_current_trace", lambda tags: state["tags"].append(tags))
    monkeypatch.setattr(runner, "read_child_traces", fake_traces)
    monkeypatch.setattr(mlflow.openai, "autolog", lambda *a, **k: None)
    monkeypatch.setattr(mlflow.genai, "evaluate", fake_evaluate)
    state["fake_traces"] = fake_traces
    return state


def request(**over):
    base = dict(
        run_key=RUN_KEY,
        skills=["earnings-preview", "audit-xls"],
        models=["gpt"],
        modes=["trigger", "effectiveness"],
        source="draft:1",
        trigger_source="manual",
    )
    base.update(over)
    return RunRequest(**base)


def child(summary, skill, mode):
    return next(c for c in summary.children if c.skill == skill and c.mode == mode)


def test_one_parent_per_department_and_one_child_per_skill_model_mode(wired):
    summary = run_eval(CFG, request())
    assert isinstance(summary, RunSummary)
    assert set(summary.parent_run_ids) == {"equity_research", "common"}
    # 2 skills x 1 model x (trigger + with_skill + without_skill)
    assert len(summary.children) == 6
    by_skill = {}
    for c in summary.children:
        by_skill.setdefault(c.skill, set()).add(c.mode)
    assert by_skill == {
        "earnings-preview": {"trigger", "with_skill", "without_skill"},
        "audit-xls": {"trigger", "with_skill", "without_skill"},
    }


def test_each_mode_runs_only_its_kind_of_case(wired):
    summary = run_eval(CFG, request())
    mode_of = {c.run_id: c.mode for c in summary.children}
    for run_id, ids in wired["evaluated"]:
        assert ids == ([1, 2] if mode_of[run_id] == "trigger" else [101, 102])
    assert set(wired["expected"]) == {2 * REPEATS}  # records of the mode x repeats
    assert all(c.metrics["n_cases"] == 2 for c in summary.children)


def test_repeats_drive_one_evaluate_call_per_repeat(wired):
    run_eval(CFG, request())
    assert wired["evaluate_calls"] == 6 * REPEATS


def test_children_verdicts_are_logged_and_tagged_with_parent_and_run_key(wired):
    summary = run_eval(CFG, request())
    client = MlflowClient()
    for c in summary.children:
        tags = client.get_run(c.run_id).data.tags
        assert tags["verdict"] == c.verdict == "pass"
        assert tags["parent_run_id"] == summary.parent_run_ids[c.dept]
        assert tags["run_key"] == RUN_KEY  # sync finds child runs by this tag
        assert tags["harness"] == "reference" and c.harness == "reference"


def test_child_runs_write_each_field_where_sync_reads_it(wired, monkeypatch):
    """Sync reads runner tags only from tags and runner params only from params, and treats a run whose
    fields are anywhere else as tampered. The runner's own runs, failed ones included, must never look tampered."""
    from skillhub.results.sync import CHILD_PARAMS, CHILD_TAGS, tampering

    summary = run_eval(CFG, request())
    monkeypatch.setattr(mlflow.genai, "evaluate", lambda **kw: (_ for _ in ()).throw(RuntimeError("down")))
    failed = run_eval(CFG, request(run_key="pr12_failed"))
    client = MlflowClient()
    for c in [*summary.children, *failed.children]:
        run = client.get_run(c.run_id)
        own_tags = {k for k in run.data.tags if not k.startswith("mlflow.")}
        assert set(run.data.params) == CHILD_PARAMS
        assert own_tags <= CHILD_TAGS
        assert tampering(run) is None


def test_trigger_and_uplift_metrics_are_present(wired):
    summary = run_eval(CFG, request())
    trig = child(summary, "earnings-preview", "trigger")
    assert {"accuracy", "precision", "recall"} <= set(trig.metrics)
    with_ = child(summary, "earnings-preview", "with_skill")
    assert {"uplift_task_quality", "uplift_skill_format", "pass_rate_without"} <= set(with_.metrics)


def test_model_only_sees_production_names(wired):
    run_eval(CFG, request())
    seen = json.dumps(wired["model"].seen)
    assert "run_pr12" not in seen
    assert "skill_cat__dept_equity_research__earnings-preview" in seen
    assert "skill_cat__common__audit-xls" in seen
    assert "from cat.dept_equity_research" in seen  # the scope schema is rewritten too


def test_sync_then_drop_once_per_run(wired):
    run_eval(CFG, request())
    assert wired["synced"] == [RUN_KEY]
    assert wired["dropped"] == [RUN_KEY]


def test_evaluate_failure_closes_each_child_inconclusive_and_the_run_still_syncs(wired, monkeypatch):
    def boom(**kw):
        raise RuntimeError("judge endpoint down")

    monkeypatch.setattr(mlflow.genai, "evaluate", boom)
    summary = run_eval(CFG, request())
    client = MlflowClient()
    assert len(summary.children) == len(summary.errors) == 6
    for c in summary.children:
        run = client.get_run(c.run_id)
        assert (run.info.status, run.data.tags["verdict"], c.verdict) == (
            "FAILED",
            "inconclusive",
            "inconclusive",
        )
        assert run.data.tags["error"] == "RuntimeError: judge endpoint down"
    assert wired["synced"] == [RUN_KEY] and wired["dropped"] == [RUN_KEY]


def _parents(tmp_path):
    return MlflowClient().search_runs(
        _experiment_ids(tmp_path), filter_string=f"tags.run_key = '{RUN_KEY}' and tags.kind = 'parent'"
    )


def test_drop_runs_and_parents_fail_when_the_run_raises(wired, monkeypatch, tmp_path):
    def sync_down(cfg, sql, run_key, **k):
        raise RuntimeError("warehouse down")

    monkeypatch.setattr(runner, "sync_run", sync_down)
    with pytest.raises(RuntimeError, match="warehouse down"):
        run_eval(CFG, request())
    assert wired["dropped"] == [RUN_KEY]
    parents = _parents(tmp_path)
    assert parents and {p.info.status for p in parents} == {"FAILED"}


def _experiment_ids(tmp_path):
    """This test's skill experiments (the sqlite store is shared by the session)."""
    prefix = f"/t2-test/{tmp_path.name}/"
    return [e.experiment_id for e in MlflowClient().search_experiments() if e.name.startswith(prefix)]


def test_parents_finish_and_record_what_was_tested(wired, tmp_path):
    summary = run_eval(CFG, request())
    client = MlflowClient()
    for dept, parent_id in summary.parent_run_ids.items():
        run = client.get_run(parent_id)
        assert run.info.status == "FINISHED"
        assert run.data.tags["run_key"] == RUN_KEY and run.data.tags["dept"] == dept
        assert json.loads(run.data.tags["prompt_versions"])["agent_system"] == 1
        scope = json.loads(
            Path(client.download_artifacts(parent_id, "run_scope.json", str(tmp_path))).read_text()
        )
        expected = next(s for s in SCOPES if s.dept == dept)
        assert scope["schema"] == expected.schema_fqn
        assert [(s["staged_fqn"], s["content_hash"], s["etag"]) for s in scope["skills"]] == [
            (s.staged_fqn, s.content_hash, s.etag) for s in expected.skills
        ]


def test_unknown_skill_is_rejected_before_staging(wired):
    with pytest.raises(ValueError):
        run_eval(CFG, request(skills=["no-such-skill"]))
    assert wired["dropped"] == [] and wired["stage"] == {}


def test_bare_name_in_two_departments_is_rejected_and_dept_name_selects(wired, monkeypatch):
    twin = SkillBundle(
        dept="equity_research",
        name="audit-xls",
        root=Path("/x/equity_research/audit-xls"),
        files={},
        frontmatter={},
    )
    monkeypatch.setattr(runner, "discover_bundles", lambda *a, **k: [*BUNDLES, twin])
    with pytest.raises(EvalRunError, match="several departments"):
        run_eval(CFG, request(skills=["audit-xls"]))
    assert wired["stage"] == {}
    summary = run_eval(CFG, request(skills=["common/audit-xls"]))
    assert {(c.dept, c.skill) for c in summary.children} == {("common", "audit-xls")}


def test_repeats_run_inside_one_child_run(wired):
    summary = run_eval(CFG, request())
    counts = Counter(wired["eval_runs"])
    assert set(counts) == {c.run_id for c in summary.children}  # evaluate ran inside each child's active run
    assert set(counts.values()) == {REPEATS}


def test_child_runs_log_repeats_and_per_trace_tags(wired):
    summary = run_eval(CFG, request())
    assert MlflowClient().get_run(summary.children[0].run_id).data.params["repeats"] == str(REPEATS)
    assert all(set(t) == {"case_id", "repeat", "mode", "reviewed"} for t in wired["tags"])
    assert {t["reviewed"] for t in wired["tags"]} == {"true"}
    assert {t["repeat"] for t in wired["tags"]} == {str(r) for r in range(REPEATS)}


def test_run_key_goes_through_make_run_key(wired):
    summary = run_eval(CFG, request(run_key="PR 12/x"))
    assert summary.run_key == "pr_12_x"
    assert wired["synced"] == ["pr_12_x"] and wired["dropped"] == ["pr_12_x"]


def _pinned_scopes(origin="version"):
    eq = [
        staged("earnings-preview", "equity_research", EQ, origin),
        staged("audit-xls", "common", EQ, "live"),
    ]
    return [SCOPES[0], RunScope(dept="equity_research", schema_fqn=EQ, skills=eq)]


def test_versions_are_passed_through_to_stage(wired):
    wired["scopes"] = _pinned_scopes()
    summary = run_eval(CFG, request(versions={"earnings-preview": 3}))
    assert wired["stage"]["candidates"] == ["audit-xls"]
    assert wired["stage"]["versions"] == {"equity_research/earnings-preview": 3}  # stage() takes dept/name
    assert child(summary, "earnings-preview", "trigger").skill_version == 3
    assert (
        MlflowClient()
        .get_run(child(summary, "earnings-preview", "trigger").run_id)
        .data.params["skill_version"]
        == "3"
    )


def test_versions_accept_dept_name_keys(wired):
    run_eval(CFG, request(versions={"equity_research/earnings-preview": 2}))
    assert wired["stage"]["versions"] == {"equity_research/earnings-preview": 2}


def test_a_pinned_run_stages_the_version_of_the_pinned_department_through_the_real_stage(
    wired, monkeypatch, reg, make_bundle
):
    """End to end: the runner's pins reach the real stage() as dept/name, so a pin resolves in its own
    department and never in another department's skill of the same name."""
    from skillhub.bundle import content_hash
    from skillhub.registry import stage as stage_module
    from skillhub.registry.versions import publish

    def published(dept, body):
        b = make_bundle(dept, "earnings-preview", body=body)
        publish(
            reg.cfg, reg.uc, reg.journal, b, git_sha="a" * 40, eval_run_id="r", actor="ci", job_run_id=None
        )
        return b

    published("equity_research", "equity v1")
    pinned = published("equity_research", "equity v2")
    published("common", "common v1")
    published("common", "common v2")  # the same name and number in common, with other content
    seen = {}

    def real_stage(cfg, uc, journal, run_key, candidates, **kw):
        seen["versions"] = kw.get("versions")
        seen["scopes"] = stage_module.stage(reg.cfg, reg.uc, reg.journal, run_key, candidates, **kw)
        return seen["scopes"]

    monkeypatch.setattr(runner, "stage", real_stage)
    wired["mcp"] = lambda host, schemas, headers: FakeSkillsMcp(
        {
            f"skill_{s.staged_fqn}": (f"Load {s.staged_fqn}", "SKILL BODY")
            for sc in seen["scopes"]
            for s in sc.skills
            if sc.schema_fqn in schemas
        }
    )
    summary = run_eval(
        CFG,
        request(
            skills=["equity_research/earnings-preview"],
            versions={"equity_research/earnings-preview": 2},
            modes=["trigger"],
        ),
    )
    assert summary.errors == [], summary.errors
    assert seen["versions"] == {"equity_research/earnings-preview": 2}
    [scope] = seen["scopes"]
    [skill] = scope.skills
    assert (scope.dept, skill.dept, skill.origin) == ("equity_research", "equity_research", "version")
    assert skill.content_hash == content_hash(pinned.files)
    assert reg.uc.read_files(skill.staged_fqn)["SKILL.md"] == pinned.files["SKILL.md"]
    trigger = child(summary, "earnings-preview", "trigger")
    params = MlflowClient().get_run(trigger.run_id).data.params
    assert (trigger.skill_version, params["skill_version"]) == (2, "2")
    assert params["content_hash"] == content_hash(pinned.files)


def test_versions_for_an_unselected_skill_are_rejected_before_staging(wired):
    with pytest.raises(ValueError):
        run_eval(CFG, request(skills=["audit-xls"], versions={"earnings-preview": 3}))
    assert wired["dropped"] == [] and wired["stage"] == {}


def test_records_one_rubric_per_child_run(wired):
    summary = run_eval(CFG, request())
    assert len(wired["rubrics"]) == len(summary.children) == 6
    assert sorted((d, r.skill_name) for d, r in wired["rubrics"]) == sorted(
        (c.dept, c.skill) for c in summary.children
    )


def test_planted_recall_comes_only_from_workbook_cases(wired):
    summary = run_eval(CFG, request())
    assert child(summary, "audit-xls", "with_skill").metrics["planted_bugs_recall"] == pytest.approx(0.5)
    assert all("planted_bugs_recall" not in c.metrics for c in summary.children if c.skill != "audit-xls")
    assert "planted_bugs_recall" not in child(summary, "audit-xls", "trigger").metrics


def test_missing_and_errored_traces_count_as_errors(wired):
    def assess(skill, mode, case, rep):
        if rep == 1:
            return None if case.id in (1, 101) else "ERROR"
        return default_assessments(skill, mode, case, rep)

    wired["assess"] = assess
    summary = run_eval(CFG, request(skills=["earnings-preview"]))
    for c in summary.children:
        assert c.metrics["error_rate"] == pytest.approx(0.5)  # 2 of 4 case-repeats lost or failed
        assert c.verdict == "inconclusive"


def test_guideline_no_votes_fail_the_case(wired):
    def assess(skill, mode, case, rep):
        out = default_assessments(skill, mode, case, rep)
        return [A(a.name, "no") if a.name == "assertions__gpt" else a for a in out]

    wired["assess"] = assess
    summary = run_eval(CFG, request(skills=["earnings-preview"]))
    with_ = child(summary, "earnings-preview", "with_skill")
    assert with_.metrics["pass_rate"] == pytest.approx(0.5)  # case 101 fails its assertion, 102 passes
    assert with_.verdict == "warn"


def test_trigger_metrics_do_not_trust_a_stringified_none(wired):
    def assess(skill, mode, case, rep):
        if mode == "trigger" and not case.should_trigger:  # metadata values are strings on Databricks
            return [A("skill_trigger", "correct_reject", {"invoked_skill": "None", "expected_skill": "None"})]
        return default_assessments(skill, mode, case, rep)

    wired["assess"] = assess
    summary = run_eval(CFG, request(skills=["earnings-preview"]))
    trig = child(summary, "earnings-preview", "trigger")
    assert (
        trig.metrics["accuracy"] == 1.0 and trig.metrics["precision"] == 1.0 and trig.metrics["recall"] == 1.0
    )


def test_failed_planted_bug_assessment_is_an_error(wired):
    def assess(skill, mode, case, rep):
        out = default_assessments(skill, mode, case, rep)
        return [A(a.name, None, error="bad manifest") if a.name == "planted_bugs_found" else a for a in out]

    wired["assess"] = assess
    summary = run_eval(CFG, request(skills=["audit-xls"]))
    with_ = child(summary, "audit-xls", "with_skill")
    assert with_.metrics["error_rate"] == pytest.approx(0.5) and with_.verdict == "inconclusive"
    assert "planted_bugs_recall" not in with_.metrics


def test_modes_without_cases_are_skipped(wired):
    wired["eval_files"] = eval_files(TRIGGER)
    summary = run_eval(CFG, request(skills=["earnings-preview"]))
    assert [c.mode for c in summary.children] == ["trigger"]


def test_live_source_stages_no_candidates_and_skips_skills_without_a_live_copy(wired):
    wired["scopes"] = [
        RunScope(dept="common", schema_fqn=CO, skills=[staged("audit-xls", "common", CO, "live")]),
        RunScope(dept="equity_research", schema_fqn=EQ, skills=[staged("audit-xls", "common", EQ, "live")]),
    ]
    summary = run_eval(CFG, request(source="live", trigger_source="nightly"))
    assert wired["stage"]["candidates"] == []
    assert wired["stage"]["scope_depts"] == ["common", "equity_research"]
    assert {c.skill for c in summary.children} == {"audit-xls"}
    assert set(summary.parent_run_ids) == {"common"}


def test_candidate_missing_from_its_scope_is_recorded_and_the_rest_runs(wired):
    wired["scopes"] = [SCOPES[0], RunScope(dept="equity_research", schema_fqn=EQ, skills=[])]
    summary = run_eval(CFG, request())
    assert [(e.dept, e.skill) for e in summary.errors] == [("equity_research", "earnings-preview")]
    assert {c.skill for c in summary.children} == {"audit-xls"}
    assert wired["synced"] == [RUN_KEY] and wired["dropped"] == [RUN_KEY]


def _runs(tmp_path, skill):
    return MlflowClient().search_runs(_experiment_ids(tmp_path), filter_string=f"params.skill = '{skill}'")


def test_setup_failure_creates_no_child_run_and_is_recorded(wired, tmp_path):
    def scorers(cfg, rubric, profiles, mode, *, skill_name, cases=None, fixtures_root=None):
        if skill_name == "audit-xls" and mode != "trigger":
            raise FileNotFoundError("fixtures/northwind/model_bugs.json")
        return []

    wired["scorers"] = scorers
    summary = run_eval(CFG, request())
    [err] = summary.errors
    assert (err.dept, err.skill, err.run_id) == ("common", "audit-xls", None)
    assert "common/audit-xls without_skill: scorers could not be built" in err.message
    assert "model_bugs.json" in err.message
    assert _runs(tmp_path, "audit-xls") == []  # no child run that sync could not read
    assert {c.skill for c in summary.children} == {"earnings-preview"}
    assert wired["synced"] == [RUN_KEY]


def test_unreadable_evals_json_is_recorded_before_staging_and_the_rest_runs(wired):
    wired["eval_files"] = {**eval_files(), "audit-xls": ValueError("evals.json: invalid JSON")}
    summary = run_eval(CFG, request())
    [err] = summary.errors
    assert (err.dept, err.skill) == ("common", "audit-xls") and "invalid JSON" in err.message
    assert {c.skill for c in summary.children} == {"earnings-preview"}
    assert sorted(wired["stage"]["candidates"]) == ["audit-xls", "earnings-preview"]  # siblings still see it


def test_child_failing_after_it_started_is_closed_inconclusive_and_synced(wired, monkeypatch, tmp_path):
    def traces(exp_id, run_id, expected, **kw):
        params = MlflowClient().get_run(run_id).data.params
        if params["skill"] == "earnings-preview" and params["mode"] == "with_skill":
            raise ConnectionError(f"trace store down while reading {EQ}.earnings-preview")
        return wired["fake_traces"](exp_id, run_id, expected, **kw)

    monkeypatch.setattr(runner, "read_child_traces", traces)
    summary = run_eval(CFG, request())
    failed = child(summary, "earnings-preview", "with_skill")
    run = MlflowClient().get_run(failed.run_id)
    assert run.info.status == "FAILED"
    assert run.data.tags["verdict"] == failed.verdict == "inconclusive"  # the gate blocks: fail closed
    assert "trace store down" in run.data.tags["error"] and "run_pr12" not in run.data.tags["error"]
    assert run.data.metrics["n_cases"] == 0 and run.data.params["repeats"] == str(REPEATS)  # sync can read it
    [err] = summary.errors
    assert (err.run_id, err.mode, err.model_profile) == (failed.run_id, "with_skill", "gpt")
    assert wired["synced"] == [RUN_KEY]
    assert all(c.verdict == "pass" for c in summary.children if c is not failed)


def test_skills_server_connect_failure_creates_no_child_and_uses_production_names(wired, tmp_path):
    class Down:
        def __init__(self, host, schemas, headers):
            self.url = f"{host}/ai-gateway/skills/?schema={schemas[0]}"

        async def __aenter__(self):
            raise ConnectionError(f"cannot reach {self.url}")

        async def __aexit__(self, *exc):
            return None

    wired["mcp"] = Down
    summary = run_eval(CFG, request(skills=["earnings-preview"]))
    assert [c.mode for c in summary.children] == ["without_skill"]
    assert sorted(e.mode for e in summary.errors) == ["trigger", "with_skill"]
    for e in summary.errors:
        assert e.run_id is None and "run_pr12" not in e.message
        assert "schema=cat.dept_equity_research" in e.message
    assert {r.data.params["mode"] for r in _runs(tmp_path, "earnings-preview")} == {"without_skill"}


def _child_records(summary):
    """Params, tags and traces (as JSON) of every child run."""
    client = MlflowClient()
    mlflow.flush_trace_async_logging()
    out = []
    for c in summary.children:
        run = client.get_run(c.run_id)
        traces = mlflow.search_traces(locations=[run.info.experiment_id], run_id=c.run_id, return_type="list")
        out.append((c, json.dumps([run.data.params, run.data.tags]), [t.to_json() for t in traces]))
    return out


def test_no_staged_names_in_child_params_tags_or_traces(wired):
    wired["model"] = SkillCallingModel()
    records = _child_records(run_eval(CFG, request()))
    for c, run_json, traces in records:
        assert "run_pr12" not in run_json
        assert traces and all("run_pr12" not in t for t in traces)
        if c.mode != "without_skill":  # the TOOL span holds the skill's production name and rewritten output
            names = ("skill_cat.dept_equity_research.earnings-preview", "skill_cat.common.audit-xls")
            assert any(n in t for n in names for t in traces)
            assert any("SKILL BODY of cat." in t for t in traces) or c.mode == "trigger"


def test_the_leak_check_fails_without_the_name_rewrite(wired, monkeypatch):
    """Control for the test above: with an empty name map the staged names reach the traces."""
    wired["model"] = SkillCallingModel()
    monkeypatch.setattr(runner, "_run_names", lambda cfg, scope: ToolNameMap())
    records = _child_records(run_eval(CFG, request(skills=["earnings-preview"])))
    leaked = {c.mode for c, _, traces in records if any("run_pr12" in t for t in traces)}
    assert leaked == {"trigger", "with_skill"}


def test_a_child_that_fails_is_inconclusive_even_when_errors_would_not_gate(wired, monkeypatch):
    lenient = SimpleNamespace(
        **{**vars(CFG), "defaults": SimpleNamespace(**{**vars(CFG.defaults), "max_error_rate": 1.0})}
    )

    def boom(**kw):
        raise RuntimeError("judge endpoint down")

    monkeypatch.setattr(mlflow.genai, "evaluate", boom)
    summary = run_eval(lenient, request(skills=["audit-xls"]))
    client = MlflowClient()
    assert summary.children and all(c.verdict == "inconclusive" for c in summary.children)
    assert {client.get_run(c.run_id).data.tags["verdict"] for c in summary.children} == {"inconclusive"}


def test_a_profile_no_client_can_serve_is_recorded_per_skill_before_staging(wired, monkeypatch):
    built = []

    def clients(cfg, profile, headers):
        built.append((profile, wired["stage"] == {}))  # (profile, still before staging)
        if profile == "claude":
            raise NotImplementedError("ug-anthropic route is not supported yet")
        return wired["model"]

    monkeypatch.setattr(runner, "model_client", clients)
    summary = run_eval(CFG, request(models=["gpt", "claude"]))
    assert built == [("gpt", True), ("claude", True)]  # one client per profile, all before staging
    assert {c.model_profile for c in summary.children} == {"gpt"}
    assert sorted((e.skill, e.model_profile) for e in summary.errors) == [
        ("audit-xls", "claude"),
        ("earnings-preview", "claude"),
    ]
    assert all("ug-anthropic route is not supported yet" in e.message for e in summary.errors)
    assert wired["synced"] == [RUN_KEY]


def test_a_retried_attempt_listed_first_is_not_counted_as_an_error(wired, monkeypatch):
    def traces(exp_id, run_id, expected, **kw):
        out = wired["fake_traces"](exp_id, run_id, expected, **kw)
        retried = [fake_trace(t.info.tags["case_id"], t.info.tags["repeat"], t.info.tags["mode"], [], True, 1)
                   for t in out]  # fmt: skip
        return retried + out  # MLflow's rate-limit retry: an earlier, unscored ERROR trace per cell

    monkeypatch.setattr(runner, "read_child_traces", traces)
    summary = run_eval(CFG, request())
    assert all(c.verdict == "pass" and c.metrics["error_rate"] == 0 for c in summary.children)


def _trigger_ctx():
    return SimpleNamespace(cases={c.id: c for c in TRIGGER}, judges=["gpt"], rubric=None, skill=None)


def test_cells_prefer_the_scored_attempt_then_the_newest():
    ok = A("skill_trigger", "correct")
    retry_old = fake_trace(1, 0, "trigger", [], failed=True, timestamp_ms=1)
    final = fake_trace(1, 0, "trigger", [ok], timestamp_ms=2)
    assert runner._cells([retry_old, final], _trigger_ctx().cases)[(1, 0)] is final
    final_error = fake_trace(1, 0, "trigger", [A("should_trigger", True)], failed=True, timestamp_ms=0)
    assert runner._cells([retry_old, final_error], _trigger_ctx().cases)[(1, 0)] is final_error


def test_a_duplicate_does_not_fill_a_missing_cell(monkeypatch):
    ok = [A("skill_trigger", "correct")]
    early = [
        fake_trace(1, 0, "trigger", [], failed=True, timestamp_ms=1),  # retried attempt
        fake_trace(1, 0, "trigger", ok, timestamp_ms=2),
        fake_trace(2, 0, "trigger", ok),
        fake_trace(1, 1, "trigger", ok),
    ]  # 4 traces for 4 cells, but (2, 1) has not landed
    full = [*early, fake_trace(2, 1, "trigger", ok)]
    reads = iter([early, full])
    monkeypatch.setattr(runner.mlflow, "search_traces", lambda **kw: next(reads))
    monkeypatch.setattr(runner.mlflow, "flush_trace_async_logging", lambda: None)
    sleeps = []
    monkeypatch.setattr(runner.time, "sleep", lambda s: sleeps.append(s))
    ctx = _trigger_ctx()
    done = lambda ts: runner._complete(ctx, ts, "trigger", [1, 2], 2, False)  # noqa: E731
    assert not done(early) and done(full)
    assert runner.read_child_traces("e", "r", 4, complete=done) is full and len(sleeps) == 1


def test_skill_models_override_defaults_and_an_explicit_list_wins(wired):
    files = eval_files()
    files["earnings-preview"] = files["earnings-preview"].model_copy(update={"models": ["claude", "gpt"]})
    wired["eval_files"] = files
    summary = run_eval(CFG, request(models=[]))
    by_skill = {}
    for c in summary.children:
        by_skill.setdefault(c.skill, set()).add(c.model_profile)
    assert by_skill == {"earnings-preview": {"claude", "gpt"}, "audit-xls": {"gpt"}}  # defaults.models
    summary = run_eval(CFG, request(models=["gpt"]))
    assert {c.model_profile for c in summary.children} == {"gpt"}


def test_unknown_model_in_evals_json_is_rejected_before_staging(wired):
    files = eval_files()
    files["audit-xls"] = files["audit-xls"].model_copy(update={"models": ["nope"]})
    wired["eval_files"] = files
    with pytest.raises(EvalRunError, match="common/audit-xls: evals.json names unknown model profile"):
        run_eval(CFG, request(models=[]))
    assert wired["stage"] == {} and wired["dropped"] == []


def test_pins_on_one_name_in_two_departments_both_reach_stage(wired, monkeypatch):
    """stage() resolves each dept/name pin in its own department, so neither pin is lost."""
    twin = SkillBundle(
        dept="equity_research",
        name="audit-xls",
        root=Path("/x/equity_research/audit-xls"),
        files={},
        frontmatter={},
    )
    monkeypatch.setattr(runner, "discover_bundles", lambda *a, **k: [*BUNDLES, twin])
    pins = {"common/audit-xls": 3, "equity_research/audit-xls": 2}
    run_eval(CFG, request(skills=["common/audit-xls", "equity_research/audit-xls"], versions=pins))
    assert wired["stage"]["versions"] == pins and wired["stage"]["candidates"] == []


def test_evaluate_runs_with_bounded_judges_and_restores_the_environment(wired, monkeypatch):
    monkeypatch.delenv(runner.MAX_SCORER_WORKERS, raising=False)
    run_eval(CFG, request(skills=["audit-xls"]))
    assert {(e[runner.SKIP_TRACE_VALIDATION], e[runner.MAX_SCORER_WORKERS]) for e in wired["env"]} == {
        ("true", "2")
    }
    assert runner.MAX_SCORER_WORKERS not in os.environ and runner.SKIP_TRACE_VALIDATION not in os.environ
    monkeypatch.setenv(runner.MAX_SCORER_WORKERS, "5")  # an operator's own setting is kept
    run_eval(CFG, request(skills=["audit-xls"]))
    assert wired["env"][-1][runner.MAX_SCORER_WORKERS] == "5"


def test_a_broken_results_package_fails_before_staging(wired, monkeypatch):
    monkeypatch.setattr(runner, "_require_results", REQUIRE_RESULTS)
    monkeypatch.setitem(sys.modules, "skillhub.results.sync", None)
    with pytest.raises(EvalRunError, match=r"skillhub\.results\.[\w.]+ is needed to run evals"):
        run_eval(CFG, request())
    assert wired["stage"] == {} and wired["evaluate_calls"] == 0


def test_prompt_versions_record_registry_versions_only_for_the_text_used(wired, monkeypatch):
    def load(cfg, key):
        text = load_packaged(key) if key == "agent_system" else "an edited judge prompt"
        return PromptRef(key, f"cat.eval.{key}", 4, text)

    monkeypatch.setattr(runner, "load_prompt", load)
    summary = run_eval(CFG, request(skills=["audit-xls"]))
    params = MlflowClient().get_run(summary.children[0].run_id).data.params
    # judges use the packaged text, so a registry version whose text differs is not the one used
    assert json.loads(params["prompt_versions"]) == {
        "agent_system": 4,
        "judge_criterion": 0,
        "judge_skill_use": 0,
    }


def test_endpoint_semaphore_is_shared_per_endpoint():
    a = runner._endpoint_semaphore("ep-x", 2)
    assert a is runner._endpoint_semaphore("ep-x", 2)
    assert a is not runner._endpoint_semaphore("ep-y", 2)


def _ctx(tmp_path, case):
    return SimpleNamespace(
        cases={case.id: case}, names=ToolNameMap(), fixtures=FixtureStore(tmp_path), system_prompt="sys"
    )


def _only_trace(run_id):
    mlflow.flush_trace_async_logging()
    [trace] = mlflow.search_traces(run_id=run_id, return_type="list")
    return trace


def test_predict_tags_the_trace_before_any_call_that_can_fail(tmp_path):
    class Down:
        def chat(self, *a, **k):
            raise RuntimeError("endpoint down")

    case = EvalCase(id=7, prompt="p", should_trigger=True, reviewed=False)
    predict = runner._predict_fn(
        _ctx(tmp_path, case), Down(), "with_skill", 1, threading.BoundedSemaphore(1), None
    )
    with mlflow.start_run() as run:
        with pytest.raises(RuntimeError, match="endpoint down"):
            predict(prompt="p", case_id=7)
    trace = _only_trace(run.info.run_id)
    assert {k: trace.info.tags[k] for k in ("case_id", "repeat", "mode", "reviewed")} == {
        "case_id": "7",
        "repeat": "1",
        "mode": "with_skill",
        "reviewed": "false",
    }
    assert trace.info.state.value == "ERROR"


def test_agent_error_is_a_model_fail_recorded_on_the_trace(tmp_path):
    loop = ChatResult(
        text="",
        tool_calls=[ToolCall(id="t", name="nope", arguments={})],
        finish_reason="tool_calls",
        usage={"prompt_tokens": 1, "completion_tokens": 1},
        assistant_message={"role": "assistant", "content": ""},
    )
    case = EvalCase(id=8, prompt="p", should_trigger=True)
    model = FakeModelClient([loop] * runner.MAX_TURNS)
    predict = runner._predict_fn(
        _ctx(tmp_path, case), model, "without_skill", 0, threading.BoundedSemaphore(1), None
    )
    with mlflow.start_run() as run:
        assert predict(prompt="p", case_id=8) == ""
    trace = _only_trace(run.info.run_id)
    assert trace.info.state.value == "OK"  # judges score the empty answer: a fail, not an error
    assert trace.info.tags["agent_error"] == f"max_turns={runner.MAX_TURNS} reached"


def test_skills_list_is_fetched_once_and_failures_use_production_names():
    names = ToolNameMap()
    names.set_fqn_rewrite(
        {
            f"{EQ}.earnings-preview": "cat.dept_equity_research.earnings-preview",
            EQ: "cat.dept_equity_research",
        }
    )

    class Bridge:
        calls = 0
        closed = False

        def list_tools(self):
            Bridge.calls += 1
            return ["tool"]

        def close(self):
            Bridge.closed = True

    cached = runner._CachedSkills(Bridge(), names)
    assert cached.list_tools() == cached.list_tools() == ["tool"]
    assert Bridge.calls == 1
    cached.close()
    assert Bridge.closed

    class Down(Bridge):
        def list_tools(self):
            raise ConnectionError(
                f"GET https://h/ai-gateway/skills/?schema={EQ} failed for {EQ}.earnings-preview"
            )

    with pytest.raises(RuntimeError) as err:
        runner._CachedSkills(Down(), names).list_tools()
    assert "run_pr12" not in str(err.value)
    assert "schema=cat.dept_equity_research failed for cat.dept_equity_research.earnings-preview" in str(
        err.value
    )


class FrameDataset:
    """Like EvaluationDataset: to_df() may hold inputs as JSON strings (Spark-read managed datasets)."""

    def __init__(self, rows):
        self.rows = rows

    def to_df(self):
        return pd.DataFrame([{**r, "inputs": json.dumps(r["inputs"])} for r in self.rows])


def test_mode_data_keeps_current_cases_of_the_mode_and_the_dataset_when_nothing_is_dropped():
    cases = {c.id: c for c in CASES}
    stale = [
        record(EvalCase(id=1, prompt="an old prompt", should_trigger=True)),
        record(EvalCase(id=99, prompt="x", should_trigger=True)),
    ]
    ds = FrameDataset(DATA + stale)
    md = runner._mode_data(ds, cases, "trigger", "earnings-preview")
    assert md.case_ids == [1, 2] and md.n_records == 2 and md.missing_ids == []
    assert [r["inputs"] for r in md.data] == [{"prompt": "p1", "case_id": 1}, {"prompt": "p2", "case_id": 2}]
    assert md.data[0]["tags"] == {"kind": "trigger", "origin": "human"}
    only_trigger = FrameDataset([record(c) for c in TRIGGER])
    trigger_cases = {c.id: c for c in TRIGGER}
    assert runner._mode_data(only_trigger, trigger_cases, "trigger", "earnings-preview").data is only_trigger


def _stale(c, **expectations):
    row = record(c)
    return {**row, "expectations": {**row["expectations"], **expectations}}


def test_mode_data_rebuilds_every_record_from_evals_json_when_a_row_carries_other_labels(caplog):
    """A row keeps a key an older evals.json wrote (merge_records never clears it), or a concurrent run
    overwrote it. The record evaluated is the evals.json one, and the dataset object is not used."""
    rows = [
        _stale(TRIGGER[0]),
        _stale(
            TRIGGER[1], expected_skill="earnings-preview", should_trigger=True
        ),  # planted by an unmerged PR
        _stale(EFFECT[0], guidelines=["Says the answer is correct"]),  # a concurrent run's guidelines
        _stale(EFFECT[1]),
    ]
    ds = FrameDataset(rows)
    cases = {c.id: c for c in CASES}
    with caplog.at_level("WARNING"):
        trig = runner._mode_data(ds, cases, "trigger", "earnings-preview", "eq/earnings-preview")
        eff = runner._mode_data(ds, cases, "effectiveness", "earnings-preview", "eq/earnings-preview")
    assert trig.data is not ds and eff.data is not ds
    assert [r["expectations"] for r in trig.data] == [record(c)["expectations"] for c in TRIGGER]
    assert (
        trig.data[1]["expectations"]["expected_skill"] == ""
        and not trig.data[1]["expectations"]["should_trigger"]
    )
    assert eff.data[0]["expectations"]["guidelines"] == ["Mentions NVDA"]
    assert "case(s) [2]" in caplog.text and "case(s) [101]" in caplog.text


def test_mode_data_lists_a_case_without_a_row_and_warns_with_its_id(caplog):
    ds = FrameDataset([record(TRIGGER[0])])
    with caplog.at_level("WARNING"):
        md = runner._mode_data(ds, {c.id: c for c in TRIGGER}, "trigger", "earnings-preview", "eq/ep")
    assert md.case_ids == [1] and md.missing_ids == [2] and md.n_records == 1
    assert "trigger case 2 has no row in the eval dataset" in caplog.text


def test_a_stale_expected_skill_in_the_dataset_never_turns_a_false_trigger_into_a_pass(wired):
    """Regression: reviewed negative case 2 carries expected_skill=earnings-preview in the
    shared dataset. The skill fires on it, and the assessment repeats the stale label. Per evals.json it is a
    false trigger, so the case fails and the verdict is not `pass`."""
    wired["dataset"] = [record(TRIGGER[0]), _stale(TRIGGER[1], expected_skill="earnings-preview"), *DATA[2:]]

    def assess(skill, mode, case, rep):
        if mode == "trigger":  # the skill fires on both cases
            stale = {"invoked_skill": skill, "expected_skill": skill}
            return [A("skill_trigger", "correct", stale)]
        return default_assessments(skill, mode, case, rep)

    wired["assess"] = assess
    summary = run_eval(CFG, request(skills=["earnings-preview"]))
    trig = child(summary, "earnings-preview", "trigger")
    assert trig.metrics["pass_rate"] == pytest.approx(0.5)  # case 1 correct, case 2 false_trigger
    assert trig.verdict == "warn"
    assert trig.metrics["precision"] == pytest.approx(0.5) and trig.metrics["accuracy"] == pytest.approx(0.5)
    evaluated = next(rows for run_id, rows in wired["rows"] if run_id == trig.run_id)
    assert [r["expectations"]["expected_skill"] for r in evaluated] == ["earnings-preview", ""]


def test_a_trigger_assessment_without_invoked_skill_is_an_error_not_a_pass(wired):
    def assess(skill, mode, case, rep):
        if mode == "trigger":
            return [A("skill_trigger", "correct_reject", {})]
        return default_assessments(skill, mode, case, rep)

    wired["assess"] = assess
    summary = run_eval(CFG, request(skills=["earnings-preview"], modes=["trigger"]))
    trig = child(summary, "earnings-preview", "trigger")
    assert trig.metrics["error_rate"] == 1.0 and trig.verdict == "inconclusive"


def test_a_reviewed_case_without_a_dataset_row_is_an_error_in_its_cells(wired, caplog):
    """The case is not dropped. Each of its repeats is an error, so the run cannot pass on fewer cases."""
    wired["dataset"] = [r for r in DATA if r["inputs"]["case_id"] != 2]
    with caplog.at_level("WARNING"):
        summary = run_eval(CFG, request(skills=["earnings-preview"], modes=["trigger"]))
    trig = child(summary, "earnings-preview", "trigger")
    assert next(ids for run_id, ids in wired["evaluated"] if run_id == trig.run_id) == [1]
    assert trig.metrics["n_cases"] == 2 and trig.metrics["error_rate"] == pytest.approx(0.5)
    assert trig.verdict == "inconclusive"
    assert "trigger case 2 has no row in the eval dataset" in caplog.text
    assert wired["expected"] == [REPEATS]  # trace polling waits only for the cases that ran


def test_duplicate_case_ids_in_evals_json_are_a_setup_error_before_staging(wired, monkeypatch, tmp_path):
    """Evalspec refuses a repeated case id at load, and the runner records that for the skill only."""
    from skillhub import evalspec

    files = eval_files()
    data = files["earnings-preview"].model_dump(mode="json")
    data["evals"].append({"id": 2, "prompt": "another prompt", "should_trigger": False})
    path = tmp_path / "evals.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(
        runner,
        "read_evals",
        lambda p: (
            evalspec.read_evals(path)
            if Path(p).parts[-3] == "earnings-preview"
            else (files["audit-xls"], b"{}")
        ),
    )
    summary = run_eval(CFG, request())
    assert {c.skill for c in summary.children} == {"audit-xls"}
    [err] = summary.errors
    assert err.skill == "earnings-preview" and "case ids must be unique; repeated: 2" in err.message


def test_read_child_traces_polls_until_every_trace_is_ready(monkeypatch):
    @mlflow.trace
    def case(i):
        return i

    with mlflow.start_run() as run:
        case(1)
        case(2)
    exp, run_id = run.info.experiment_id, run.info.run_id
    sleeps = []
    monkeypatch.setattr(runner.time, "sleep", lambda s: sleeps.append(s))
    traces = runner.read_child_traces(exp, run_id, 2, complete=lambda ts: len(sleeps) >= 2)
    assert len(traces) == 2 and len(sleeps) == 2
    # after the timeout the partial list is returned; the runner scores what is missing as errors
    assert len(runner.read_child_traces(exp, run_id, 3, timeout_s=0)) == 2


def test_results_functions_fail_clearly_without_the_results_package(monkeypatch):
    monkeypatch.setitem(sys.modules, "skillhub.results.experiments", None)
    with pytest.raises(EvalRunError, match="skillhub.results.experiments"):
        runner.ensure_experiment(CFG, "common", "audit-xls")


# --- skillhub eval ---------------------------------------------------------------------------------------

EVAL_ARGS = ["--models", "gpt", "--modes", "trigger", "--source", "live", "--trigger-source", "manual"]


@pytest.fixture
def cli_calls(monkeypatch):
    seen = {"roots": []}

    def fake_run(cfg, req):
        seen["req"] = req
        return RunSummary(run_key=req.run_key, parent_run_ids={"common": "p1"}, children=[])

    def discover(root=None):
        seen["roots"].append(root)
        return BUNDLES

    monkeypatch.setattr(cli, "load_config", lambda *a, **k: CFG)
    monkeypatch.setattr("skillhub.bundle.discover_bundles", discover)
    monkeypatch.setattr("skillhub.eval.runner.run_eval", fake_run)
    return seen


def invoke(*args):
    return CliRunner().invoke(cli.app, ["eval", *args])


def test_eval_command_prints_run_summary_json(cli_calls):
    result = invoke("--run-key", RUN_KEY, "--skills", "audit-xls", *EVAL_ARGS)
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out["run_key"] == RUN_KEY and out["parent_run_ids"] == {"common": "p1"}
    assert cli_calls["req"].skills == ["common/audit-xls"]  # always sent as dept/name


def test_eval_command_expands_all_skills_and_default_models(cli_calls):
    result = invoke(
        "--run-key", "PR 12/x", "--skills", "all", "--models", "default", "--modes", "trigger",
        "--source", "live", "--trigger-source", "manual",
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert sorted(cli_calls["req"].skills) == ["common/audit-xls", "equity_research/earnings-preview"]
    assert cli_calls["req"].models == []  # each skill's evals.json models, else defaults.models
    assert cli_calls["req"].run_key == "pr_12_x"  # CLI labels go through make_run_key


def test_eval_command_treats_empty_job_params_as_unset(cli_calls):
    result = invoke(
        "--run-key", RUN_KEY, "--skills", "audit-xls", "--models", "gpt", "--modes", "trigger,effectiveness",
        "--source", "", "--trigger-source", "manual", "--requested-by", "", "--versions", "",
        "--skills-root", "", "--fixtures-root", "",
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    req = cli_calls["req"]
    assert req.source == "live" and req.requested_by is None and req.versions is None
    assert req.skills_root is None and req.fixtures_root is None
    assert cli_calls["roots"] == [None]  # discover_bundles falls back to $SKILLHUB_SKILLS
    assert req.modes == ["trigger", "effectiveness"]


def test_eval_command_passes_the_candidate_roots_through(cli_calls, tmp_path):
    result = invoke(
        "--run-key", RUN_KEY, "--skills", "audit-xls", *EVAL_ARGS, "--requested-by", "alice",
        "--skills-root", str(tmp_path / "skills"), "--fixtures-root", str(tmp_path / "fixtures"),
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    req = cli_calls["req"]
    assert cli_calls["roots"] == [tmp_path / "skills"]
    assert req.skills_root == str(tmp_path / "skills") and req.fixtures_root == str(tmp_path / "fixtures")
    assert req.requested_by == "alice"


def test_eval_command_accepts_dept_name_and_rejects_ambiguous_bare_names(cli_calls, monkeypatch):
    twin = SkillBundle(
        dept="equity_research",
        name="audit-xls",
        root=Path("/x/equity_research/audit-xls"),
        files={},
        frontmatter={},
    )
    monkeypatch.setattr("skillhub.bundle.discover_bundles", lambda root=None: [*BUNDLES, twin])
    assert invoke("--run-key", RUN_KEY, "--skills", "audit-xls", *EVAL_ARGS).exit_code == 2
    result = invoke(
        "--run-key", RUN_KEY, "--skills", "equity_research/audit-xls", *EVAL_ARGS,
        "--versions", "equity_research/audit-xls=3",
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert cli_calls["req"].skills == ["equity_research/audit-xls"]
    assert cli_calls["req"].versions == {"equity_research/audit-xls": 3}


def test_eval_command_passes_pins_on_one_name_in_two_departments(cli_calls, monkeypatch):
    twin = SkillBundle(
        dept="equity_research",
        name="audit-xls",
        root=Path("/x/equity_research/audit-xls"),
        files={},
        frontmatter={},
    )
    monkeypatch.setattr("skillhub.bundle.discover_bundles", lambda root=None: [*BUNDLES, twin])
    result = invoke(
        "--run-key", RUN_KEY, "--skills", "common/audit-xls,equity_research/audit-xls", *EVAL_ARGS,
        "--versions", "common/audit-xls=3,equity_research/audit-xls=2",
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert cli_calls["req"].versions == {"common/audit-xls": 3, "equity_research/audit-xls": 2}


def test_eval_command_exits_1_on_errors_and_keeps_stdout_json(cli_calls, monkeypatch):
    def fake_run(cfg, req):
        print("MLflow banner and run URL")  # libraries print to stdout during the run
        return RunSummary(
            run_key=req.run_key,
            parent_run_ids={"common": "p1"},
            children=[],
            errors=[runner.RunError("common", "audit-xls", "setup failed: no rubric", "gpt", "with_skill")],
        )

    monkeypatch.setattr("skillhub.eval.runner.run_eval", fake_run)
    result = invoke("--run-key", RUN_KEY, "--skills", "audit-xls", *EVAL_ARGS)
    assert result.exit_code == 1
    out = json.loads(result.stdout)  # stdout is the RunSummary JSON only
    assert out["errors"][0]["message"] == "setup failed: no rubric"
    assert "error: common/audit-xls gpt with_skill: setup failed: no rubric" in result.stderr
    assert "MLflow banner" in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        ["--run-key", "", "--skills", "audit-xls", *EVAL_ARGS],
        ["--run-key", RUN_KEY, "--skills", "", *EVAL_ARGS],
        ["--run-key", RUN_KEY, "--skills", "audit-xls", *EVAL_ARGS[:-6], "--modes", "speed", *EVAL_ARGS[4:]],
        ["--run-key", RUN_KEY, "--skills", "audit-xls", "--models", "nope", *EVAL_ARGS[2:]],
        ["--run-key", RUN_KEY, "--skills", "audit-xls", *EVAL_ARGS[:4], "--source", "main", *EVAL_ARGS[6:]],
        ["--run-key", RUN_KEY, "--skills", "audit-xls", *EVAL_ARGS[:6], "--trigger-source", "cron"],
        ["--run-key", RUN_KEY, "--skills", "audit-xls", *EVAL_ARGS, "--versions", "earnings-preview=3"],
        ["--run-key", RUN_KEY, "--skills", "audit-xls", *EVAL_ARGS, "--versions", "audit-xls=0"],
        ["--run-key", RUN_KEY, "--skills", "audit-xls", *EVAL_ARGS[:4], "--source", "", *EVAL_ARGS[6:],
         "--skills-root", "/candidates/skills"],
    ],
    ids=["empty-run-key", "no-skills", "bad-mode", "unknown-model", "bad-source", "bad-trigger-source",
         "version-for-unselected-skill", "version-not-positive", "skills-root-without-source"],
)  # fmt: skip
def test_eval_command_rejects_bad_values_with_exit_2(cli_calls, args):
    result = invoke(*args)
    assert result.exit_code == 2, result.output
    assert "req" not in cli_calls


def test_a_pinned_skill_staged_from_other_content_is_an_error_not_labelled_as_the_version(wired):
    """When the pin resolves to another department, the run schema holds the live copy. It must not be
    evaluated and reported as the pinned version."""
    wired["scopes"] = _pinned_scopes(origin="live")
    summary = run_eval(CFG, request(versions={"earnings-preview": 5}))
    assert {c.skill for c in summary.children} == {"audit-xls"}
    [err] = summary.errors
    assert (
        err.skill == "earnings-preview"
        and "version 5 was pinned" in err.message
        and "live copy" in err.message
    )


def test_a_judge_profile_listed_twice_votes_once(wired):
    summary = run_eval(CFG, request(skills=["earnings-preview"], judges=["gpt", "gpt"]))
    params = MlflowClient().get_run(child(summary, "earnings-preview", "with_skill").run_id).data.params
    assert params["judges"] == "gpt"


def test_a_rubric_for_another_skill_is_a_setup_error(wired, monkeypatch):
    rubrics = {**RUBRICS, "earnings-preview": RUBRICS["audit-xls"]}
    monkeypatch.setattr(runner, "load_rubric", lambda path: rubrics[Path(path).parts[-3]])
    summary = run_eval(CFG, request())
    [err] = summary.errors
    assert err.skill == "earnings-preview" and "rubric.json is for skill 'audit-xls'" in err.message
    assert {c.skill for c in summary.children} == {"audit-xls"}


def test_a_rubric_generated_from_other_content_is_tagged_stale(wired):
    summary = run_eval(CFG, request(skills=["earnings-preview"]))
    tags = {c.mode: MlflowClient().get_run(c.run_id).data.tags for c in summary.children}
    assert tags["with_skill"]["rubric_stale"] == "true" and tags["without_skill"]["rubric_stale"] == "true"
    assert "rubric_stale" not in tags["trigger"]


def test_an_interrupted_child_is_closed_with_a_verdict_and_the_interrupt_propagates(
    wired, monkeypatch, tmp_path
):
    """A Job cancel (KeyboardInterrupt/SystemExit) inside evaluate must not end the child FINISHED without
    a verdict, or sync could never complete the run key."""

    def cancelled(**kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(mlflow.genai, "evaluate", cancelled)
    with pytest.raises(KeyboardInterrupt):
        run_eval(CFG, request(skills=["earnings-preview"], modes=["trigger"]))
    [run] = _runs(tmp_path, "earnings-preview")
    assert run.info.status == "FAILED"
    assert run.data.tags["verdict"] == "inconclusive" and "interrupted" in run.data.tags["error"]
    assert wired["dropped"] == [RUN_KEY]


def _with_thresholds(wired, monkeypatch, *, fail_rate=None, warn_rate=None, pass_score=0.7):
    files = eval_files()
    files["earnings-preview"] = files["earnings-preview"].model_copy(
        update={"criteria": Criteria(fail_rate=fail_rate, warn_rate=warn_rate)}
    )
    wired["eval_files"] = files
    rubrics = {
        **RUBRICS,
        "earnings-preview": RUBRICS["earnings-preview"].model_copy(update={"pass_score": pass_score}),
    }
    monkeypatch.setattr(runner, "load_rubric", lambda path: rubrics[Path(path).parts[-3]])


def test_a_per_skill_threshold_that_would_loosen_the_gate_is_ignored_with_a_warning(
    wired, monkeypatch, caplog
):
    """The evals.json criteria and rubric pass_score may only tighten cfg.defaults."""
    _with_thresholds(wired, monkeypatch, fail_rate=0.1, warn_rate=0.2, pass_score=0.1)

    def assess(skill, mode, case, rep):  # 3 of 5 criteria pass: 0.6 < the default pass_score 0.7
        if mode == "trigger":
            return default_assessments(skill, mode, case, rep)
        return [A(f"rubric_{c.id}__gpt", i < 3) for i, c in enumerate(CRITERIA)] + [
            A("assertions__gpt", "yes")
        ]

    wired["assess"] = assess
    with caplog.at_level("WARNING"):
        summary = run_eval(CFG, request(skills=["earnings-preview"]))
    assert child(summary, "earnings-preview", "with_skill").verdict == "fail"  # defaults gate: 0.0 < 0.5
    for issue in (
        "criteria.fail_rate 0.1 is below defaults.fail_rate 0.5",
        "criteria.warn_rate 0.2 is below defaults.warn_rate 0.8",
        "rubric pass_score 0.1 is below defaults.pass_score 0.7",
    ):  # the evalspec.gate_thresholds warning, once per skill
        assert caplog.text.count(f"equity_research/earnings-preview: {issue}; the default is used") == 1


def test_a_per_skill_threshold_that_tightens_the_gate_applies(wired, monkeypatch):
    _with_thresholds(wired, monkeypatch, fail_rate=0.6, warn_rate=0.9, pass_score=0.7)

    def assess(skill, mode, case, rep):  # case 101 passes, case 102 fails: pass_rate 0.5
        out = default_assessments(skill, mode, case, rep)
        if mode != "trigger" and case.id == 102:
            out = [A(a.name, False) if a.name.startswith("rubric_") else a for a in out]
        return out

    wired["assess"] = assess
    summary = run_eval(CFG, request(skills=["earnings-preview"]))
    assert (
        child(summary, "earnings-preview", "with_skill").verdict == "fail"
    )  # 0.5 < 0.6; the default says warn


def _effect_assess(passing):
    """Assessments where `passing(case_id, repeat)` of the 5 criteria pass in the effectiveness modes."""

    def assess(skill, mode, case, rep):
        if mode == "trigger":
            return default_assessments(skill, mode, case, rep)
        n = passing(case.id, rep)
        out = [A(f"rubric_{c.id}__gpt", i < n) for i, c in enumerate(CRITERIA)]
        return out + ([A("assertions__gpt", "yes")] if case.assertions else [])

    return assess


@pytest.mark.parametrize(
    ("loosening", "passing", "verdict"),
    [
        # every case scores 0.6: pass_score 0.5 would pass them all, the default 0.7 fails them all
        ({"pass_score": 0.5}, lambda cid, rep: 3, "fail"),
        # 1 of 4 case-repeats passes (0.25): fail_rate 0.1 would make the fail a warn
        ({"fail_rate": 0.1}, lambda cid, rep: 5 if (cid, rep) == (101, 0) else 0, "fail"),
        # 2 of 4 pass (0.5): warn_rate 0.2 would make the warn a pass
        ({"warn_rate": 0.2}, lambda cid, rep: 5 if cid == 101 else 0, "warn"),
    ],
    ids=["pass_score", "fail_rate", "warn_rate"],
)
def test_a_loosening_per_skill_value_has_no_effect_on_the_gating_verdict(
    wired, monkeypatch, loosening, passing, verdict
):
    """Every verdict uses evalspec.gate_thresholds, so a looser evals.json or rubric.json value gives
    the same verdict and pass rate as no per-skill value at all."""
    wired["assess"] = _effect_assess(passing)
    req = request(skills=["earnings-preview"], modes=["effectiveness"])

    def outcome(summary):
        return {c.mode: (c.verdict, c.metrics["pass_rate"]) for c in summary.children}

    baseline = outcome(run_eval(CFG, req))
    assert baseline["with_skill"][0] == verdict
    _with_thresholds(wired, monkeypatch, **{"pass_score": 0.7, **loosening})
    assert outcome(run_eval(CFG, req)) == baseline


def test_a_rubric_without_pass_score_gates_on_the_hub_default(wired, monkeypatch):
    """gate_thresholds: when rubric.json does not set pass_score, defaults.pass_score applies, not the Rubric
    model's own 0.7."""
    rubric = Rubric(
        skill_name="earnings-preview",
        source_hash="sha256:r",
        generated_by="gpt",
        reviewed=True,
        criteria=CRITERIA,
    )
    assert "pass_score" not in rubric.model_fields_set
    rubrics = {**RUBRICS, "earnings-preview": rubric}
    monkeypatch.setattr(runner, "load_rubric", lambda path: rubrics[Path(path).parts[-3]])
    cfg = SimpleNamespace(
        **{**vars(CFG), "defaults": SimpleNamespace(**{**vars(CFG.defaults), "pass_score": 0.5})}
    )
    wired["assess"] = _effect_assess(lambda cid, rep: 3)  # every case scores 0.6
    summary = run_eval(cfg, request(skills=["earnings-preview"], modes=["effectiveness"]))
    assert child(summary, "earnings-preview", "with_skill").verdict == "pass"  # 0.6 >= 0.5


# --- The runner logs the eval-input digests of what it loaded ---------------------------------------------


def test_child_runs_log_the_dataset_digest_of_the_bytes_and_fixtures_they_used(wired, tmp_path):
    from skillhub import evalspec

    fx = tmp_path / "fixtures"
    (fx / "northwind").mkdir(parents=True)
    (fx / "northwind" / "model.xlsx").write_bytes(b"workbook")
    (fx / "northwind" / "model_bugs.json").write_bytes(b'{"bugs": []}')
    (fx / "web_index.json").write_bytes(b'{"keywords": {}}')
    summary = run_eval(CFG, request(fixtures_root=str(fx)))
    client = MlflowClient()
    for c in summary.children:
        params = client.get_run(c.run_id).data.params
        ef = wired["eval_files"][c.skill]
        data = ef.model_dump_json().encode()
        paths = evalspec.fixture_paths(ef, evalspec.fixture_index_files(fx))
        assert "northwind/model_bugs.json" in paths and "web_index.json" in paths
        want = evalspec.dataset_digest(data, {p: evalspec.read_fixture(fx, p) for p in paths})
        assert params["dataset_digest"] == want
        assert params["rubric_hash"] == evalspec.rubric_hash(RUBRICS[c.skill])


def test_a_skill_without_rubric_json_logs_no_rubric(wired, monkeypatch):
    from skillhub.evalspec import NO_RUBRIC

    def load_rubric(path):
        raise FileNotFoundError(path)

    monkeypatch.setattr(runner, "load_rubric", load_rubric)
    summary = run_eval(CFG, request(modes=["trigger"]))
    for c in summary.children:
        assert MlflowClient().get_run(c.run_id).data.params["rubric_hash"] == NO_RUBRIC


def test_the_agent_reads_the_fixture_snapshot_not_the_candidate_folder(tmp_path):
    """A fixture swapped in the candidate folder after the digest never reaches the agent or the scorers."""
    from skillhub import evalspec

    src = tmp_path / "src"
    (src / "northwind").mkdir(parents=True)
    (src / "northwind" / "model.xlsx").write_bytes(b"original")
    (src / "unused.md").write_bytes(b"u")
    (tmp_path / "secret.md").write_bytes(b"s")
    ef = EvalFile(
        skill_name="s",
        evals=[
            EvalCase(
                id=101,
                kind="effectiveness",
                prompt="p",
                should_trigger=True,
                fixtures=["northwind/model.xlsx"],
            ),
            EvalCase(
                id=102, kind="effectiveness", prompt="q", should_trigger=True, fixtures=["../secret.md"]
            ),
        ],
    )
    plan = runner._SkillPlan(bundle=BUNDLES[0], ef=ef, models=["gpt"], evals_bytes=b"{}")
    store = runner._snapshot_fixtures(src, [plan], tmp_path / "snap")
    (src / "northwind" / "model.xlsx").write_bytes(b"swapped")
    assert (store.root / "northwind" / "model.xlsx").read_bytes() == b"original"
    assert not (store.root / "unused.md").exists()
    assert not (tmp_path / "snap" / ".." / "secret.md").resolve().is_relative_to(store.root)
    assert plan.dataset_digest == evalspec.dataset_digest(
        b"{}", {"northwind/model.xlsx": b"original", "northwind/model_bugs.json": None, "../secret.md": None}
    )


def test_the_runner_and_eval_digests_compute_the_same_dataset_digest(tmp_path):
    """The plan step's expected value (skillhub eval-digests on the checkout) equals what the runner logs."""
    from skillhub import evalspec

    root = tmp_path / "skills" / "common" / "audit-xls"
    (root / "evals").mkdir(parents=True)
    ef = EvalFile(skill_name="audit-xls", evals=list(CASES))
    evalspec.save_evals(root / "evals" / "evals.json", ef)
    fx = tmp_path / "fixtures"
    (fx / "northwind").mkdir(parents=True)
    (fx / "northwind" / "model.xlsx").write_bytes(b"wb")
    (fx / "northwind" / "web_index.json").write_bytes(b"{}")
    bundle = SkillBundle(dept="common", name="audit-xls", root=root, files={}, frontmatter={})
    loaded, data = runner._load_evals(bundle)
    plan = runner._SkillPlan(bundle=bundle, ef=loaded, models=["gpt"], evals_bytes=data)
    runner._snapshot_fixtures(fx, [plan], tmp_path / "snap")
    assert plan.dataset_digest == evalspec.eval_digests(root, fx).dataset_digest


def test_the_runner_produces_exactly_the_planned_gating_cells(wired):
    """The gates require a row for every cell evalspec.planned_cells plans, so the runner must plan the
    same cells: models resolved request > evals.json > defaults, modes filtered by the cases present."""
    from skillhub import evalspec

    wired["eval_files"]["audit-xls"] = EvalFile(
        skill_name="audit-xls", evals=list(TRIGGER), models=["claude"]
    )
    req = request(models=[])
    summary = run_eval(CFG, req)
    for skill in ("earnings-preview", "audit-xls"):
        ran = sorted(
            f"{c.model_profile}/{c.mode}"
            for c in summary.children
            if c.skill == skill and c.mode != "without_skill"
        )
        plan = evalspec.planned_cells(
            wired["eval_files"][skill],
            request_models=req.models,
            default_models=CFG.defaults.models,
            modes=req.modes,
        )
        assert ran == sorted(plan) and plan
