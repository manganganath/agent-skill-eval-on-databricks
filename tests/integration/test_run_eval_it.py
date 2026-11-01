"""One real run_eval: one skill, trigger mode, one repeat.

It checks what the unit tests fake: `evaluate` inside an active child run, trace read-back through
`search_traces(run_id=...)`, the real `info.assessments` shape, and the tags sync depends on.

Needs SKILLHUB_IT=1, a dev workspace, the results package and
SKILLHUB_IT_SKILL=<dept>/<name> naming a bundle under $SKILLHUB_SKILLS that has trigger cases.
"""

import os
import uuid

import mlflow
import pytest
from mlflow import MlflowClient

from skillhub.config import load_config
from skillhub.eval.runner import RunRequest, run_eval

pytestmark = pytest.mark.integration


@pytest.mark.skipif(os.environ.get("SKILLHUB_IT") != "1", reason="needs SKILLHUB_IT=1 and a dev workspace")
def test_trigger_run_of_one_skill_logs_a_verdict_and_tagged_traces():
    skill = os.environ.get("SKILLHUB_IT_SKILL")
    if not skill:
        pytest.skip("set SKILLHUB_IT_SKILL=<dept>/<name>")
    cfg = load_config()
    cfg = cfg.model_copy(update={"defaults": cfg.defaults.model_copy(update={"repeats": 1})})
    req = RunRequest(
        run_key=f"it-{uuid.uuid4().hex[:8]}",
        skills=[skill],
        models=[cfg.defaults.models[0]],
        modes=["trigger"],
        source="draft:it",
        trigger_source="manual",
        requested_by="integration-test",
    )
    summary = run_eval(cfg, req)

    assert not summary.errors, summary.errors
    [child] = summary.children
    assert child.mode == "trigger" and child.verdict in ("pass", "warn", "fail", "inconclusive", "advisory")
    run = MlflowClient().get_run(child.run_id)
    assert run.data.tags["verdict"] == child.verdict
    assert run.data.tags["run_key"] == summary.run_key
    assert run.data.tags["parent_run_id"] == summary.parent_run_ids[child.dept]
    assert run.data.params["repeats"] == "1"
    traces = mlflow.search_traces(locations=[run.info.experiment_id], run_id=child.run_id, return_type="list")
    assert len(traces) == int(run.data.metrics["n_cases"])
    for t in traces:
        assert {"case_id", "repeat", "mode", "reviewed"} <= set(t.info.tags)
        assert t.info.tags["mode"] == "trigger" and t.info.tags["repeat"] == "0"
        assert t.info.state.value == "ERROR" or "skill_trigger" in {a.name for a in t.info.assessments}
        assert f"run_{summary.run_key}_" not in t.to_json()  # production names only
