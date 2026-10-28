import contextlib
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from app_fakes import FakeApi, FakeWorkspace
from backend import authz
from backend.api import try_panel
from conftest import make_cfg

from skillhub.registry.uc_skills import SkillInfo

ME = "/api/2.0/preview/scim/v2/Me"
FQN = "skill_hub.dept_equity_research.earnings-preview"
HDR = {"x-forwarded-access-token": "user-token-123"}


class FakeMcp:
    def __init__(self, host, schemas, headers):
        self.host, self.schemas, self.headers = host, list(schemas), headers


class FakeBridge:
    """Stands in for the sync SkillsBridge: records that it was closed."""

    def __init__(self, client):
        self.client = client
        self.closed = 0

    def close(self):
        self.closed += 1


class FakeSpan:
    def __init__(self, trace_id):
        self.trace_id = trace_id
        self.inputs = self.outputs = self.error = None
        self.ended = False

    def set_inputs(self, value):
        self.inputs = value

    def set_outputs(self, value):
        self.outputs = value


class FakeMlflow:
    """Each start_span opens a root span (one trace). update_current_trace tags the open trace and records how
    many agent runs had started by then, so a test can tell tags set at the start from tags set at the end."""

    def __init__(self, calls):
        self._calls = calls
        self._open = None
        self.no_op = False
        self.tag_error = None

    def set_experiment(self, experiment_id):
        self._calls["experiments"].append(experiment_id)

    @contextlib.contextmanager
    def start_span(self, name, span_type=None):
        n = len(self._calls["spans"]) + 1
        span = FakeSpan("MLFLOW_NO_OP_SPAN_TRACE_ID" if self.no_op else f"tr-{n}")
        self._calls["spans"].append(span)
        self._open = span
        try:
            yield span
        except BaseException as exc:
            span.error = exc
            raise
        finally:
            span.ended = True
            self._open = None

    def update_current_trace(self, tags):
        if self.tag_error is not None:
            raise self.tag_error
        assert self._open is not None and not self._open.ended, "tags must go on the live trace"
        self._calls["tags"].append((self._open.trace_id, dict(tags), len(self._calls["agent"])))


def eventually(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", "https://ws.example.test")
    calls = {
        "spans": [],
        "tags": [],
        "experiments": [],
        "agent": [],
        "bridges": [],
        "models": [],
        "model_hosts": [],
        "boom": None,
    }

    class _UC:
        def __init__(self, w):
            pass

        def get(self, fqn):
            return SkillInfo(
                fqn=fqn,
                name="earnings-preview",
                bundle_name=None,
                description=None,
                etag="e",
                finalize_time=None,
                comment=None,
            )

    def make_bridge(client):
        b = FakeBridge(client)
        calls["bridges"].append(b)
        return b

    def fake_model_client(cfg, profile, headers, **kw):
        calls.setdefault("model_kw", []).append(kw)
        calls["models"].append((profile, headers()))
        calls["model_hosts"].append(cfg.workspace.host)
        closed = calls.setdefault("closed", [])
        return SimpleNamespace(
            profile_name=profile, _client=SimpleNamespace(close=lambda: closed.append(profile))
        )

    def fake_run_agent(case, *, model, skills, names, fixtures, trigger, system_prompt, max_turns=8):
        calls["agent"].append(
            {"skills": skills, "trigger": trigger, "system_prompt": system_prompt, "prompt": case.prompt}
        )
        if calls["boom"] and skills is not None:
            raise calls["boom"]
        if skills is not None:
            return SimpleNamespace(
                final_text="with skill answer", invoked_skills=[FQN], tool_calls=[], turns=2, error=None
            )
        return SimpleNamespace(
            final_text="plain answer", invoked_skills=[], tool_calls=[], turns=1, error=None
        )

    engine = SimpleNamespace(
        mlflow=FakeMlflow(calls),
        FixtureStore=lambda: None,
        SkillsBridge=make_bridge,
        run_agent=fake_run_agent,
        model_client=fake_model_client,
        SkillsMcpClient=lambda h, s, hd: FakeMcp(h, s, hd),
        ToolNameMap=lambda: None,
        load_packaged=lambda key: f"packaged {key}",
        ensure_experiment=lambda cfg, dept, skill: "exp-9",
    )
    monkeypatch.setattr(authz, "UCSkills", _UC)
    monkeypatch.setattr(try_panel, "_load_engine", lambda: engine)
    return calls


def user_w():
    return FakeWorkspace(api=FakeApi({ME: {"userName": "ana@example.com", "groups": []}}))


def post(make_client, body, **kw):
    c = make_client(user_w=user_w(), app_w=FakeWorkspace(), cfg=make_cfg())
    return c.post("/api/try", json=body, headers=HDR)


def body(**over):
    return {"fqn": FQN, "prompt": "prep earnings", "models": ["claude-sonnet"]} | over


def test_try_runs_with_and_without_and_reports_invocation(make_client, wired):
    r = post(make_client, body())
    assert r.status_code == 200
    out = r.json()[0]
    assert out["with_skill"] == {"text": "with skill answer", "invoked": True}
    assert out["without_skill"] == {"text": "plain answer"}
    assert out["trace_url"] == "https://ws.example.test/ml/experiments/exp-9/traces?selectedEvaluationId=tr-1"
    assert [a["skills"] is not None for a in wired["agent"]] == [True, False]
    assert all(a["trigger"] is False for a in wired["agent"])
    assert wired["agent"][0]["system_prompt"] == "packaged agent_system"


def test_try_closes_the_bridge(make_client, wired):
    post(make_client, body())
    assert [b.closed for b in wired["bridges"]] == [1]


def test_try_uses_the_user_token_and_scopes_to_dept_and_common(make_client, wired):
    post(make_client, body())
    bridge = wired["bridges"][0]
    assert bridge.client.headers() == {"Authorization": "Bearer user-token-123"}
    assert bridge.client.schemas == ["skill_hub.common", "skill_hub.dept_equity_research"]
    assert bridge.client.host == "https://ws.example.test"  # workspace_host(), like the user client
    assert wired["models"] == [("claude-sonnet", {"Authorization": "Bearer user-token-123"})]


def test_try_uses_the_normalised_host_for_the_model_client_skills_server_and_trace_link(
    make_client, wired, monkeypatch
):
    # Inside Databricks Apps DATABRICKS_HOST is the bare hostname, and hub.yaml sets host: ${DATABRICKS_HOST}.
    monkeypatch.setenv("DATABRICKS_HOST", "ws.example.test/")
    cfg = make_cfg()
    cfg.workspace.host = "ws.example.test"
    c = make_client(user_w=user_w(), app_w=FakeWorkspace(), cfg=cfg)
    r = c.post("/api/try", json=body(), headers=HDR)
    assert r.status_code == 200
    assert wired["model_hosts"] == ["https://ws.example.test"]
    assert wired["bridges"][0].client.host == "https://ws.example.test"
    assert r.json()[0]["trace_url"].startswith("https://ws.example.test/ml/experiments/exp-9/traces?")
    assert cfg.workspace.host == "ws.example.test"  # the shared config is not changed


def test_try_without_databricks_host_is_refused(make_client, wired, monkeypatch):
    monkeypatch.delenv("DATABRICKS_HOST")
    r = post(make_client, body())
    assert r.status_code == 403 and r.json() == {"detail": {"reason": "authz_check_failed"}}
    assert wired["agent"] == []


def test_try_tags_each_trace_at_its_start(make_client, wired):
    assert post(make_client, body()).status_code == 200
    assert wired["experiments"] == ["exp-9"]
    tags = {"trigger_source": "hub-try", "requested_by": "ana@example.com"}
    # (trace, tags, agent runs started when the tags were set): each trace is tagged before its run starts.
    assert wired["tags"] == [
        ("tr-1", tags | {"mode": "with_skill"}, 0),
        ("tr-2", tags | {"mode": "without_skill"}, 1),
    ]
    spans = wired["spans"]
    assert [sp.ended for sp in spans] == [True, True]
    assert spans[0].inputs == {"prompt": "prep earnings"}
    assert spans[0].outputs == {"final_text": "with skill answer"}


def test_try_tags_the_trace_even_when_the_agent_fails(make_client, wired):
    wired["boom"] = RuntimeError("model down")
    assert post(make_client, body()).status_code == 502
    assert [(t[0], t[1]["mode"], t[1]["trigger_source"]) for t in wired["tags"]] == [
        ("tr-1", "with_skill", "hub-try")
    ]
    assert wired["spans"][0].error is wired["boom"]


def test_try_tagging_failure_never_fails_a_finished_run(make_client, wired, caplog):
    try_panel._load_engine().mlflow.tag_error = RuntimeError("tag store down for token-xyz")
    with caplog.at_level("WARNING", logger="skillhub.app.try"):
        r = post(make_client, body())
    assert r.status_code == 200
    assert r.json()[0]["trace_url"].endswith("selectedEvaluationId=tr-1")
    assert any("tagging failed: RuntimeError" in m for m in caplog.messages)
    assert not any("token-xyz" in m for m in caplog.messages)


def test_try_trace_url_is_null_without_a_trace(make_client, wired, monkeypatch):
    try_panel._load_engine().mlflow.no_op = True  # tracing is off: MLflow hands out no-op spans
    assert post(make_client, body()).json()[0]["trace_url"] is None


def test_try_dedupes_models(make_client, wired):
    r = post(make_client, body(models=["claude-sonnet", "claude-sonnet", "gemini-flash"]))
    assert [x["model"] for x in r.json()] == ["claude-sonnet", "gemini-flash"]
    assert len(wired["bridges"]) == 2


def test_try_bounds_each_model_call_and_closes_the_http_client(make_client, wired):
    r = post(make_client, body(models=["claude-sonnet", "gemini-flash"]))
    assert r.status_code == 200
    assert len(wired["model_kw"]) == 2
    for kw in wired["model_kw"]:
        assert kw["max_tries"] == 1 and 0 < kw["timeout"] <= try_panel.DEADLINE_S
    assert sorted(wired["closed"]) == ["claude-sonnet", "gemini-flash"]


def test_try_closes_the_http_client_when_the_run_fails(make_client, wired):
    wired["boom"] = RuntimeError("model down")
    r = post(make_client, body())
    assert r.status_code == 502
    assert wired["closed"] == ["claude-sonnet"]


def test_try_caps_distinct_models_at_three(make_client, wired, monkeypatch):
    cfg = make_cfg()
    cfg.models["m3"] = cfg.models["gemini-flash"]
    cfg.models["m4"] = cfg.models["gemini-flash"]
    c = make_client(user_w=user_w(), app_w=FakeWorkspace(), cfg=cfg)
    r = c.post("/api/try", json=body(models=["claude-sonnet", "gemini-flash", "m3", "m4"]), headers=HDR)
    assert r.status_code == 422 and r.json() == {"detail": {"reason": "too_many_models"}}
    assert wired["agent"] == []


def test_try_refuses_unreadable_skill(make_client, wired, monkeypatch):
    class _Deny:
        def __init__(self, w):
            pass

        def get(self, fqn):
            return None

    monkeypatch.setattr(authz, "UCSkills", _Deny)
    r = post(make_client, body())
    assert r.status_code == 404 and r.json() == {"detail": {"reason": "skill_not_found"}}
    assert wired["agent"] == []


def test_try_caps_the_fqn_length(make_client, wired):
    r = post(make_client, body(fqn="skill_hub.common." + "a" * 300))
    assert r.status_code == 422 and wired["agent"] == []


def test_try_unknown_model_is_422(make_client, wired):
    r = post(make_client, body(models=["nope"]))
    assert r.status_code == 422 and r.json() == {"detail": {"reason": "unknown_model"}}


def test_try_rejects_long_or_empty_prompt(make_client, wired):
    r = post(make_client, body(prompt="x" * 4001))
    assert r.status_code == 422 and r.json() == {"detail": {"reason": "prompt_too_long"}}
    assert post(make_client, body(prompt="")).status_code == 422
    assert wired["agent"] == []


def test_try_needs_the_user_token(make_client, wired):
    c = make_client(user_w=user_w(), app_w=FakeWorkspace(), cfg=make_cfg())
    r = c.post("/api/try", json=body())
    assert r.status_code == 403 and r.json() == {"detail": {"reason": "no_user_token"}}


def test_try_is_503_when_the_engine_is_missing(make_client, monkeypatch):
    monkeypatch.undo()  # drop the fake engine so the real lazy import runs
    monkeypatch.setenv("DATABRICKS_HOST", "https://ws.example.test")
    monkeypatch.setitem(sys.modules, "skillhub.agent.loop", None)  # makes the import fail
    monkeypatch.setattr(
        authz,
        "UCSkills",
        lambda w: SimpleNamespace(
            get=lambda fqn: SkillInfo(
                fqn=fqn,
                name="n",
                bundle_name=None,
                description=None,
                etag="e",
                finalize_time=None,
                comment=None,
            )
        ),
    )
    r = post(make_client, body())
    assert r.status_code == 503 and r.json() == {"detail": {"reason": "try_unavailable"}}


def test_try_failure_is_502_without_raw_text_and_still_closes_the_bridge(make_client, wired):
    wired["boom"] = RuntimeError("401 token abc for staged_skill_hub.x.y")
    r = post(make_client, body())
    assert r.status_code == 502 and r.json() == {"detail": {"reason": "try_failed"}}
    assert "abc" not in r.text
    assert [b.closed for b in wired["bridges"]] == [1]


def test_try_bridge_start_failure_is_502(make_client, wired, monkeypatch):
    eng = try_panel._load_engine()

    def bad(client):
        raise ConnectionError("gateway down at https://internal.example")

    eng.SkillsBridge = bad
    r = post(make_client, body())
    assert r.status_code == 502 and r.json() == {"detail": {"reason": "try_failed"}}
    assert "internal" not in r.text


def test_try_is_429_when_another_run_holds_the_lock(make_client, wired, monkeypatch):
    monkeypatch.setattr(try_panel, "LOCK_WAIT_S", 0.05)
    assert try_panel._TRY_LOCK.acquire(timeout=1)
    try:
        r = post(make_client, body())
    finally:
        try_panel._TRY_LOCK.release()
    assert r.status_code == 429 and r.json() == {"detail": {"reason": "try_busy"}}
    assert wired["agent"] == [] and wired["bridges"] == []


def test_try_is_504_at_the_deadline_and_the_lock_is_released_at_once(make_client, wired, monkeypatch):
    monkeypatch.setattr(try_panel, "DEADLINE_S", 0.2)
    gate = threading.Event()
    eng = try_panel._load_engine()
    inner = eng.run_agent

    def slow(case, **kw):
        if kw["skills"] is not None:
            gate.wait(5)
        return inner(case, **kw)

    eng.run_agent = slow
    try:
        r = post(make_client, body())
        assert r.status_code == 504 and r.json() == {"detail": {"reason": "try_timeout"}}
        # The worker is still blocked in its run, but the next request can take the lock now.
        assert try_panel._TRY_LOCK.acquire(timeout=0.5)
        try_panel._TRY_LOCK.release()
    finally:
        gate.set()  # let the worker finish; it must stop and close the bridge
    assert eventually(lambda: [b.closed for b in wired["bridges"]] == [1])
    time.sleep(0.05)
    assert [a["skills"] is not None for a in wired["agent"]] == [True]  # no baseline run after the timeout
    assert len(wired["spans"]) == 1  # and no new trace


class StepModel:
    """A model that asks for one skill tool call per turn, and blocks on `gate` in the turn named `block_turn`."""

    def __init__(self, steps, gate, block_turn):
        self.profile_name = "claude-sonnet"
        self.steps, self.gate, self.block_turn = steps, gate, block_turn

    def chat(self, messages, tools, *, max_tokens=None, trigger=False):
        from skillhub.gateway.models import ChatResult, ToolCall

        self.steps.append("chat")
        if self.steps.count("chat") == self.block_turn:
            self.gate.wait(5)
        if not tools:
            return ChatResult("plain", [], "stop", {}, {"role": "assistant", "content": "plain"})
        call = ToolCall(f"c{len(self.steps)}", tools[0].name, {})
        raw = {"id": call.id, "type": "function", "function": {"name": call.name, "arguments": "{}"}}
        return ChatResult(
            "", [call], "tool_calls", {}, {"role": "assistant", "content": None, "tool_calls": [raw]}
        )


class StepBridge(FakeBridge):
    """A skills bridge with one skill tool. Blocks on `gate` in the call_tool named `block_call`."""

    def __init__(self, client, steps, gate, block_call):
        super().__init__(client)
        self.steps, self.gate, self.block_call = steps, gate, block_call

    def list_tools(self):
        self.steps.append("list_tools")
        return [SimpleNamespace(name="skill_skill_hub.common.audit-xls", description="d", input_schema={})]

    def call_tool(self, name, arguments):
        self.steps.append("call_tool")
        if self.steps.count("call_tool") == self.block_call:
            self.gate.wait(5)
        return "skill text"


@pytest.fixture
def real_loop(wired, monkeypatch):
    """The real agent loop, with a stepping model and bridge. Only the loop's own span calls are faked."""
    from skillhub.agent import loop
    from skillhub.agent.fixtures import FixtureStore
    from skillhub.gateway.toolnames import ToolNameMap

    @contextlib.contextmanager
    def span(*a, **k):
        yield SimpleNamespace(
            set_inputs=lambda *_: None, set_outputs=lambda *_: None, set_attributes=lambda *_: None
        )

    monkeypatch.setattr(loop, "mlflow", SimpleNamespace(start_span=span))
    monkeypatch.setattr(try_panel, "DEADLINE_S", 0.3)
    state = SimpleNamespace(steps=[], gate=threading.Event(), block_turn=0, block_call=0, bridges=[])
    eng = try_panel._load_engine()
    eng.run_agent, eng.ToolNameMap, eng.FixtureStore = loop.run_agent, ToolNameMap, FixtureStore
    eng.model_client = lambda cfg, profile, headers, **kw: StepModel(
        state.steps, state.gate, state.block_turn
    )

    def bridge(client):
        b = StepBridge(client, state.steps, state.gate, state.block_call)
        state.bridges.append(b)
        return b

    eng.SkillsBridge = bridge
    yield state
    state.gate.set()


def _post_until_timeout(make_client, state):
    try:
        r = post(make_client, body())
        assert r.status_code == 504
        assert try_panel._TRY_LOCK.acquire(timeout=0.5)  # free at the 504
        try_panel._TRY_LOCK.release()
    finally:
        state.gate.set()
    assert eventually(lambda: [b.closed for b in state.bridges] == [1])
    time.sleep(0.05)


def test_the_agent_loop_stops_between_turns_after_the_timeout(make_client, real_loop):
    real_loop.block_turn = 1  # the first model call is still running at the deadline
    _post_until_timeout(make_client, real_loop)
    assert real_loop.steps == ["list_tools", "chat"]  # its tool call and the next turn never ran


def test_the_agent_loop_stops_between_tool_calls_after_the_timeout(make_client, real_loop):
    real_loop.block_call = 1  # the first skills call is still running at the deadline
    _post_until_timeout(make_client, real_loop)
    assert real_loop.steps == ["list_tools", "chat", "call_tool"]  # no further model turn or tool call


def test_a_timeout_during_experiment_setup_frees_the_lock_when_that_step_ends(
    make_client, wired, monkeypatch
):
    monkeypatch.setattr(try_panel, "DEADLINE_S", 0.2)
    gate = threading.Event()
    eng = try_panel._load_engine()

    def slow_experiment(cfg, dept, skill):
        gate.wait(5)
        return "exp-9"

    eng.ensure_experiment = slow_experiment
    try:
        r = post(make_client, body())
        assert r.status_code == 504
        # Creating an experiment also sets MLflow's active experiment, so the lock stays until that step ends.
        assert not try_panel._TRY_LOCK.acquire(timeout=0.1)
    finally:
        gate.set()
    assert try_panel._TRY_LOCK.acquire(timeout=5)
    try_panel._TRY_LOCK.release()
    time.sleep(0.05)
    assert wired["experiments"] == [] and wired["agent"] == [] and wired["bridges"] == []


def test_lease_releases_the_lock_once():
    lock = threading.Lock()
    lock.acquire()
    lease = try_panel._Lease(lock)
    lease.cancel()
    assert lock.acquire(timeout=0.1)  # another request now holds it
    lease.release()  # the worker's own release must not free the other request's lock
    assert not lock.acquire(timeout=0.05)
    lock.release()
    with pytest.raises(try_panel._Cancelled):
        lease.check()
    with pytest.raises(try_panel._Cancelled), lease.exclusive():
        pass
