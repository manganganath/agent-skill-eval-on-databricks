from types import SimpleNamespace as NS

import pytest
from mlflow.exceptions import MlflowException
from typer.testing import CliRunner

from skillhub import cli
from skillhub.results import label as label_mod
from tests.fakes import make_cfg

PATHS = {"11": "/Shared/skill-hub/legal/a", "12": "/Shared/skill-hub/legal/b"}


def trace(trace_id, sources):
    return NS(
        info=NS(trace_id=trace_id, assessments=[NS(name=n, source=NS(source_type=s)) for n, s in sources])
    )


class FakeSession:
    def __init__(self, url, name="x"):
        self.url, self.name, self.added = url, name, []

    def add_traces(self, traces):
        self.added.append([t.info.trace_id for t in traces])
        return self


@pytest.fixture
def env(monkeypatch):
    e = NS(
        calls=[],
        runs=[NS(info=NS(run_id="r1", experiment_id="11"))],
        traces={
            "r1": [
                trace("t1", [("rubric_c1__gpt", "LLM_JUDGE"), ("skill_trigger", "CODE")]),
                trace("t2", [("assertions__gpt", "LLM_JUDGE")]),
            ]
        },
        schemas=[],
        sessions=[],  # existing sessions
        created=[],
        search_kw=[],
        schema_error=MlflowException("RESOURCE_DOES_NOT_EXIST: nope"),
    )

    def search(**kw):
        e.search_kw.append(kw)
        return e.traces.get(kw["run_id"], [])

    def get_schema(name):
        e.calls.append(("get_schema",))
        raise e.schema_error

    def create_schema(name, **kw):
        e.calls.append(("create_schema",))
        e.schemas.append((name, kw))

    def get_sessions():
        e.calls.append(("get_sessions",))
        return list(e.sessions)

    def create_session(name, label_schemas=None, **kw):
        e.calls.append(("create_session",))
        s = FakeSession(f"https://review.example.invalid/{len(e.created)}", name)
        e.created.append((s, label_schemas))
        return s

    monkeypatch.setattr(label_mod, "configure_mlflow", lambda cfg: None)
    monkeypatch.setattr(label_mod, "child_runs", lambda cfg, key: e.runs)
    monkeypatch.setattr(label_mod.mlflow, "search_traces", search)
    monkeypatch.setattr(
        label_mod.mlflow,
        "set_experiment",
        lambda experiment_id: e.calls.append(("set_experiment", experiment_id)),
    )
    monkeypatch.setattr(label_mod.mlflow, "get_experiment", lambda i: NS(name=PATHS[i]))
    monkeypatch.setattr(label_mod.label_schemas, "get_label_schema", get_schema)
    monkeypatch.setattr(label_mod.label_schemas, "create_label_schema", create_schema)
    monkeypatch.setattr(label_mod.labeling, "get_labeling_sessions", get_sessions)
    monkeypatch.setattr(label_mod.labeling, "create_labeling_session", create_session)
    return e


def run(names=None):
    return label_mod.create_labeling_session(make_cfg(), "pr12_ab12cd3", assessment_names=names or [])


def test_label_schema_names_equal_judge_assessment_names(env):
    urls = run()
    assert urls == {PATHS["11"]: "https://review.example.invalid/0"}
    assert sorted(n for n, _ in env.schemas) == ["assertions__gpt", "rubric_c1__gpt"]
    assert env.created[0][1] == ["assertions__gpt", "rubric_c1__gpt"]
    assert env.created[0][0].name == "pr12_ab12cd3-labels"


def test_schemas_are_pass_fail_feedback(env):
    run()
    _, kw = env.schemas[0]
    assert kw["type"] == "feedback"
    assert kw["input"].options == ["pass", "fail"]
    assert kw["enable_comment"] is True


def test_set_experiment_precedes_every_review_app_call(env):
    run()
    assert env.calls[0] == ("set_experiment", "11")
    assert {c[0] for c in env.calls[1:]} <= {"get_sessions", "get_schema", "create_schema", "create_session"}
    assert "set_experiment" not in [c[0] for c in env.calls[1:]]


def test_two_experiments_give_two_sessions(env):
    env.runs.append(NS(info=NS(run_id="r2", experiment_id="12")))
    env.traces["r2"] = [trace("t3", [("rubric_c1__gpt", "LLM_JUDGE")])]
    urls = run()
    assert set(urls) == set(PATHS.values())
    assert [s.added for s, _ in env.created] == [[["t1", "t2"]], [["t3"]]]
    order = [c for c in env.calls if c[0] in ("set_experiment", "create_session")]
    assert order == [
        ("set_experiment", "11"),
        ("create_session",),
        ("set_experiment", "12"),
        ("create_session",),
    ]


def test_experiment_without_traces_is_skipped(env):
    env.runs.append(NS(info=NS(run_id="r2", experiment_id="12")))
    assert list(run()) == [PATHS["11"]]
    assert ("set_experiment", "12") not in env.calls


def test_traces_are_added_in_chunks_of_100(env):
    env.traces["r1"] = [trace(f"t{i}", [("rubric_c1__gpt", "LLM_JUDGE")]) for i in range(250)]
    run()
    assert [len(c) for c in env.created[0][0].added] == [100, 100, 50]


def test_search_does_not_load_spans(env):
    run()
    assert env.search_kw[0]["include_spans"] is False
    assert env.search_kw[0]["run_id"] == "r1"


def test_second_call_reuses_existing_session(env):
    env.sessions = [FakeSession("https://review.example.invalid/old", "pr12_ab12cd3-labels")]
    assert run() == {PATHS["11"]: "https://review.example.invalid/old"}
    assert env.created == [] and env.schemas == []


def test_existing_schema_is_reused(env, monkeypatch):
    monkeypatch.setattr(label_mod.label_schemas, "get_label_schema", lambda name: NS(name=name))
    run(["rubric_c1__gpt"])
    assert env.schemas == []
    assert env.created[0][1] == ["rubric_c1__gpt"]


def test_schema_lookup_error_other_than_not_found_propagates(env):
    env.schema_error = MlflowException("PERMISSION_DENIED: no")
    with pytest.raises(MlflowException, match="PERMISSION_DENIED"):
        run()
    assert env.schemas == []


def test_no_child_runs_is_an_error(env):
    env.runs.clear()
    with pytest.raises(ValueError, match="no child runs"):
        run()


def test_no_traces_at_all_is_an_error(env):
    env.traces.clear()
    with pytest.raises(ValueError, match="no traces"):
        run()


def test_no_judge_assessment_names_is_an_error(env):
    env.traces["r1"] = [trace("t1", [("skill_trigger", "CODE")])]
    with pytest.raises(ValueError, match="no judge assessment names"):
        run()
    assert env.created == []


def test_cli_prints_one_line_per_experiment(monkeypatch):
    seen = {}

    def fake_create(cfg, run_key, *, assessment_names):
        seen.update(run_key=run_key, names=assessment_names)
        return {"/p/a": "https://r.example.invalid/1", "/p/b": "https://r.example.invalid/2"}

    monkeypatch.setattr(label_mod, "create_labeling_session", fake_create)
    monkeypatch.setattr(cli, "load_config", lambda p=None: make_cfg())
    res = CliRunner().invoke(cli.app, ["label", "--run-key", "pr12_ab12cd3"])
    assert res.exit_code == 0, res.output
    assert res.output.splitlines() == [
        "/p/a\thttps://r.example.invalid/1",
        "/p/b\thttps://r.example.invalid/2",
    ]
    assert seen == {"run_key": "pr12_ab12cd3", "names": []}


@pytest.mark.parametrize(
    "exc", [MlflowException("boom"), ImportError("databricks-agents missing"), ValueError("no traces")]
)
def test_cli_turns_errors_into_one_line(monkeypatch, exc):
    def fake_create(cfg, run_key, *, assessment_names):
        raise exc

    monkeypatch.setattr(label_mod, "create_labeling_session", fake_create)
    monkeypatch.setattr(cli, "load_config", lambda p=None: make_cfg())
    res = CliRunner().invoke(cli.app, ["label", "--run-key", "pr12_ab12cd3"])
    assert res.exit_code == 1
    assert res.output.startswith("error: ") and "Traceback" not in res.output
