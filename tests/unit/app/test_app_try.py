import contextlib
import re
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from app_fakes import FakeApi, FakeGrants, FakeSchemas, FakeTree, FakeWorkspace
from backend import authz
from backend.api import try_panel
from conftest import make_cfg

from skillhub.gateway.skills_mcp import SkillToolError
from skillhub.gateway.toolnames import ToolNameMap
from skillhub.naming import schema_base
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
        "looked_up": [],  # (dept, skill) of each experiment lookup
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
            {
                "skills": skills,
                "trigger": trigger,
                "system_prompt": system_prompt,
                "prompt": case.prompt,
                "names": names,
            }
        )
        if calls["boom"] and skills is not None:
            raise calls["boom"]
        if skills is not None and calls.get("probe"):
            calls["probe"](skills)
        if skills is not None:
            return SimpleNamespace(
                final_text="with skill answer",
                invoked_skills=calls.get("invoked", [FQN]),
                tool_calls=[],
                turns=2,
                error=None,
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
        SkillToolError=SkillToolError,
        ToolNameMap=ToolNameMap,
        load_packaged=lambda key: f"packaged {key}",
        eval_experiment=lambda cfg, dept, skill: calls["looked_up"].append((dept, skill)) or "exp-9",
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
    # The department first: the skills server offers a capped number of skill tools, in schema order.
    assert bridge.client.schemas == ["skill_hub.dept_equity_research", "skill_hub.common"]
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

    eng.eval_experiment = slow_experiment
    try:
        r = post(make_client, body())
        assert r.status_code == 504
        # The lookup and setting MLflow's active experiment are one exclusive step: the lock stays until it ends.
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


# --- Try on a skill under review (a candidate) -------------------------------------------------------------------

CROOT = "/Workspace/Shared/skill-hub/skill_hub/candidates"  # <workspace_root>/<catalog>/candidates
CKEY = "pr5_c7ecd3e_a1"
CURL = f"/api/candidates/{CKEY}/common/new-skill/try"
LIVE_SAME = "skill_hub.common.new-skill"  # the live skill the upload replaces
LIVE_OTHER = "skill_hub.common.audit-xls"
EFF = "/api/2.1/unity-catalog/effective-permissions/"


def pr_tree(files=None):
    base = f"{CROOT}/{CKEY}/skills/common/new-skill"
    out = {
        f"{CROOT}/{CKEY}/.uploaded_at": b"1790000000\n",
        f"{base}/SKILL.md": b"---\nname: new-skill\ndescription: new-skill helps analysts\n---\n# new-skill\n",
        f"{base}/references/guide.md": b"# Guide\n",
        f"{base}/evals/evals.json": b'{"skill_name": "new-skill", "evals": []}',
    }
    return FakeTree(out | (files or {}))


def cand_user(groups=("skillhub-reviewers",), name="ana@example.com"):
    return FakeWorkspace(api=FakeApi({ME: {"userName": name, "groups": [{"display": g} for g in groups]}}))


def cand_app(tree, *, readable=("common",), schemas=None, grants=None):
    """The app's client: effective permissions for the user, and the workspace tree of the upload."""
    responses = {
        f"{EFF}schema/skill_hub.{schema_base(d)}": {
            "privilege_assignments": [
                {
                    "principal": "ana@example.com",
                    "privileges": [
                        {"privilege": p}
                        for p in (("READ_VOLUME", "USE_SCHEMA") if d in readable else ("USE_SCHEMA",))
                    ],
                }
            ]
        }
        for d in ("common", "equity_research", "external")
    }
    responses[f"{EFF}catalog/skill_hub"] = {
        "privilege_assignments": [
            {"principal": "ana@example.com", "privileges": [{"privilege": "USE_CATALOG"}]}
        ]
    }
    return FakeWorkspace(
        api=FakeApi(responses),
        workspace=tree,
        schemas=schemas or FakeSchemas(),
        grants=grants or FakeGrants(),
    )


def cand_body(**over):
    return {"prompt": "draft the note", "models": ["claude-sonnet"]} | over


@pytest.fixture
def staging(wired, monkeypatch):
    """A candidate run with a fake UC for the app's staging. Each bridge lists the live skill of the same name, the
    other live skill, and the staged tool once it is opened from `ready_after` on (the bridges opened so far)."""
    from tests.fakes import FakeUCSkills

    uc = FakeUCSkills()
    monkeypatch.setattr(try_panel, "UCSkills", lambda w: uc)
    wired["ready_after"] = 0
    wired["invoked"] = [
        LIVE_SAME
    ]  # the agent loads the candidate under the name the model sees: the live FQN

    class _Listing(FakeBridge):
        def __init__(self, client):
            super().__init__(client)
            self.index = len(wired["bridges"])  # the bridges opened before this one

        def list_tools(self):
            names = [f"skill_{LIVE_SAME}", f"skill_{LIVE_OTHER}"]
            if self.index >= wired["ready_after"]:
                names += [f"skill_{s}.new-skill" for s in self.client.schemas if ".run_try_" in s]
            return [SimpleNamespace(name=n, description="d", input_schema={}) for n in names]

    def make_bridge(client):
        b = _Listing(client)
        wired["bridges"].append(b)
        return b

    eng = try_panel._load_engine()
    eng.SkillsBridge = make_bridge
    return SimpleNamespace(uc=uc)


def try_schema(app_w, before=()):
    """The one run schema this request dropped, other than those in `before`."""
    names = [s for s in app_w.schemas.deleted if ".run_try_" in s and s not in before]
    assert len(names) == 1, names
    return names[0]


def cpost(make_client, *, user=None, app=None, body=None, url=CURL):
    c = make_client(user_w=user or cand_user(), app_w=app or cand_app(pr_tree()), cfg=make_cfg())
    return c.post(url, json=body or cand_body(), headers=HDR)


def test_candidate_try_stages_the_upload_into_a_private_schema_and_grants_only_the_requester(
    make_client, wired, staging
):
    app = cand_app(pr_tree())
    r = cpost(make_client, app=app)
    assert r.status_code == 200, r.text
    assert r.json()[0]["with_skill"] == {"text": "with skill answer", "invoked": True}
    (schema,) = staging.uc.schemas
    assert re.fullmatch(r"skill_hub\.run_try_[0-9a-f]{10}_common", schema)
    staged = f"{schema}.new-skill"
    files = staging.uc.read_files(staged)
    assert sorted(files) == ["SKILL.md", "references/guide.md"]  # evals/ is never staged
    # Only the requester gets anything on the schema, and only read privileges.
    assert len(app.grants.updates) == 1
    securable, full_name, changes = app.grants.updates[0]
    assert (securable, full_name) == ("schema", schema)
    assert [(c.principal, [p.value for p in c.add]) for c in changes] == [
        ("ana@example.com", ["USE_SCHEMA", "READ_VOLUME"])
    ]
    # The agent's scope: the private schema first, then live dept and common, so the tool cap never drops it.
    assert wired["bridges"][-1].client.schemas == [schema, "skill_hub.common"]


def test_candidate_try_hides_the_live_skill_of_the_same_name_from_list_and_call(make_client, wired, staging):
    seen = {}

    def probe(skills):
        seen["tools"] = [t.name for t in skills.list_tools()]
        for key, name, args in [
            ("hidden call", f"skill_{LIVE_SAME}", {}),
            ("load_skill", "load_skill", {"full_name": LIVE_SAME}),
        ]:
            try:
                skills.call_tool(name, args)
                seen[key] = "allowed"
            except Exception as exc:  # the wrapper refuses with a tool error the model can read
                seen[key] = type(exc).__name__

    wired["probe"] = probe
    assert cpost(make_client).status_code == 200
    staged_tool = [t for t in seen["tools"] if ".run_try_" in t]
    assert (
        len(staged_tool) == 1 and f"skill_{LIVE_OTHER}" in seen["tools"]
    )  # the upload and the other live skill
    assert f"skill_{LIVE_SAME}" not in seen["tools"]
    assert seen["hidden call"] == "SkillToolError" and seen["load_skill"] == "SkillToolError"


def test_candidate_try_shows_the_agent_the_name_it_has_in_the_live_skills(make_client, wired, staging):
    assert cpost(make_client).status_code == 200
    names = wired["agent"][0]["names"]
    (schema,) = staging.uc.schemas
    assert names.to_production(f"{schema}.new-skill") == LIVE_SAME  # a staged FQN never reaches the model
    assert names.to_production("skill_" + f"{schema}.new-skill") == "skill_" + LIVE_SAME


def test_candidate_try_tags_the_trace_with_the_run_key(make_client, wired, staging):
    assert cpost(make_client).status_code == 200
    assert wired["tags"][0][1]["candidate_run_key"] == CKEY
    assert wired["tags"][0][1]["mode"] == "with_skill"


def test_candidate_try_names_its_skill_for_the_shared_unreleased_experiment(make_client, wired, staging):
    """A new skill has no experiment of its own yet: its traces go to the department's unreleased experiment, which
    the app finds but never creates, beside other skills' traces."""
    assert cpost(make_client).status_code == 200
    assert wired["looked_up"] == [("common", "new-skill")]
    assert {t[1]["candidate_skill"] for t in wired["tags"]} == {"common/new-skill"}


def test_candidate_try_drops_its_schema_on_success(make_client, wired, staging):
    app = cand_app(pr_tree())
    assert cpost(make_client, app=app).status_code == 200
    schema = try_schema(app)
    assert schema in staging.uc.schemas  # the fake UC keeps what it staged; the app's drop is what ran
    assert app.schemas.deleted == [schema]


def test_candidate_try_drops_its_schema_when_the_agent_fails(make_client, wired, staging):
    wired["boom"] = RuntimeError("401 token abc")
    app = cand_app(pr_tree())
    r = cpost(make_client, app=app)
    assert r.status_code == 502 and r.json() == {"detail": {"reason": "try_failed"}}
    assert app.schemas.deleted == [try_schema(app)]
    assert "abc" not in r.text


def test_candidate_try_drops_its_schema_after_the_worker_stops_on_timeout(
    make_client, wired, staging, monkeypatch
):
    monkeypatch.setattr(try_panel, "DEADLINE_S", 0.2)
    gate = threading.Event()
    eng = try_panel._load_engine()
    inner = eng.run_agent

    def slow(case, **kw):
        if kw["skills"] is not None:
            gate.wait(5)
        return inner(case, **kw)

    eng.run_agent = slow
    app = cand_app(pr_tree())
    try:
        r = cpost(make_client, app=app)
        assert r.status_code == 504 and r.json() == {"detail": {"reason": "try_timeout"}}
        assert app.schemas.deleted == []  # the worker is still running: nothing is dropped under it
        assert try_panel._TRY_LOCK.acquire(timeout=0.5)  # and the lock is free
        try_panel._TRY_LOCK.release()
    finally:
        gate.set()
    assert eventually(lambda: len(app.schemas.deleted) == 1)
    assert app.schemas.deleted == [try_schema(app)]
    assert eventually(lambda: [b.closed for b in wired["bridges"]] == [1])


def test_a_timeout_during_staging_frees_the_lock_at_once_and_staging_stops(
    make_client, wired, staging, monkeypatch
):
    from tests.fakes import FakeUCSkills

    monkeypatch.setattr(try_panel, "DEADLINE_S", 0.2)
    gate = threading.Event()

    class _SlowUC(FakeUCSkills):
        def ensure_schema(self, schema_fqn):
            gate.wait(5)
            super().ensure_schema(schema_fqn)

    monkeypatch.setattr(try_panel, "UCSkills", lambda w: _SlowUC())
    app = cand_app(pr_tree())
    try:
        assert cpost(make_client, app=app).status_code == 504
        # Staging does not hold the lock, so another Try can start while it winds down.
        assert try_panel._TRY_LOCK.acquire(timeout=1)
        try_panel._TRY_LOCK.release()
    finally:
        gate.set()
    # Staging stops at its next check: nothing is uploaded, and the schema it did create is dropped.
    assert eventually(lambda: len(app.schemas.deleted) == 1)
    assert wired["agent"] == []


def test_candidate_try_needs_create_schema_on_the_catalog(make_client, wired, staging, monkeypatch):
    from databricks.sdk.errors import PermissionDenied

    from tests.fakes import FakeUCSkills

    class _Denied(FakeUCSkills):
        def ensure_schema(self, schema_fqn):
            raise PermissionDenied("no CREATE SCHEMA")

    monkeypatch.setattr(try_panel, "UCSkills", lambda w: _Denied())
    r = cpost(make_client)
    assert r.status_code == 503 and r.json() == {"detail": {"reason": "try_setup_required"}}
    assert wired["agent"] == [] and wired["bridges"] == []
    assert "no CREATE" not in r.text


@pytest.mark.parametrize(
    "user, url, status, reason",
    [
        (cand_user(("skillhub-users",)), CURL, 403, "not_author"),  # neither author nor reviewer
        (cand_user(), "/api/candidates/hub_1a2b3c4d/common/new-skill/try", 404, "candidate_not_found"),
        (cand_user(), f"/api/candidates/{CKEY}/finance/new-skill/try", 404, "candidate_not_found"),
        (cand_user(), f"/api/candidates/{CKEY}/common/Bad_Name/try", 404, "candidate_not_found"),
        (cand_user(), "/api/candidates/pr5_deadbee_a1/common/new-skill/try", 404, "candidate_not_found"),
    ],
)
def test_candidate_try_refuses_before_any_staging(make_client, wired, staging, user, url, status, reason):
    app = cand_app(pr_tree())
    r = cpost(make_client, user=user, app=app, url=url)
    assert r.status_code == status and r.json() == {"detail": {"reason": reason}}
    assert staging.uc.schemas == set() and app.schemas.deleted == [] and wired["agent"] == []


def test_candidate_try_of_a_department_the_user_cannot_read_is_not_found(make_client, wired, staging):
    app = cand_app(pr_tree(), readable=())
    r = cpost(make_client, user=cand_user(("skillhub-authors",)), app=app)
    assert r.status_code == 404 and r.json() == {"detail": {"reason": "candidate_not_found"}}
    assert staging.uc.schemas == set() and wired["agent"] == []


def test_candidate_try_without_a_skill_md_is_not_found(make_client, wired, staging):
    tree = pr_tree()
    tree.files = {p: b for p, b in tree.files.items() if not p.endswith("/SKILL.md")}
    r = cpost(make_client, app=cand_app(tree))
    assert r.status_code == 404 and r.json() == {"detail": {"reason": "candidate_not_found"}}
    assert wired["agent"] == []


def test_candidate_try_checks_the_models_and_the_prompt_like_the_live_route(make_client, wired, staging):
    r = cpost(make_client, body=cand_body(models=["nope"]))
    assert r.status_code == 422 and r.json() == {"detail": {"reason": "unknown_model"}}
    r = cpost(make_client, body=cand_body(prompt="x" * 4001))
    assert r.status_code == 422 and r.json() == {"detail": {"reason": "prompt_too_long"}}
    assert wired["agent"] == [] and staging.uc.schemas == set()


def test_candidate_try_waits_for_the_gateway_to_list_the_staged_skill(
    make_client, wired, staging, monkeypatch
):
    monkeypatch.setattr(try_panel, "READY_STEP_S", 0.01)
    wired["ready_after"] = 1  # the first bridge does not list the staged skill yet, the second does
    app = cand_app(pr_tree())
    assert cpost(make_client, app=app).status_code == 200
    assert [b.closed for b in wired["bridges"]] == [1, 1]  # the first bridge was closed and reopened
    assert wired["agent"][0]["skills"] is not None


def test_candidate_try_fails_when_the_staged_skill_never_appears(make_client, wired, staging, monkeypatch):
    monkeypatch.setattr(try_panel, "READY_WAIT_S", 0.05)
    monkeypatch.setattr(try_panel, "READY_STEP_S", 0.01)
    wired["ready_after"] = 10**6
    app = cand_app(pr_tree())
    r = cpost(make_client, app=app)
    assert r.status_code == 502 and r.json() == {"detail": {"reason": "try_failed"}}
    assert wired["agent"] == []  # no agent run without the staged skill
    assert app.schemas.deleted == [try_schema(app)]
    assert all(b.closed == 1 for b in wired["bridges"])


def test_candidate_try_sweeps_only_its_own_old_try_schemas(make_client, wired, staging):
    now = int(time.time() * 1000)
    hour = 3600 * 1000
    listed = [
        SimpleNamespace(name="run_try_aaaaaaaaaa_common", owner="ana@example.com", created_at=now - 2 * hour),
        SimpleNamespace(name="run_try_bbbbbbbbbb_common", owner="ana@example.com", created_at=now - 600_000),
        SimpleNamespace(
            name="run_try_cccccccccc_common", owner="someone@example.com", created_at=now - 2 * hour
        ),
        SimpleNamespace(name="run_pr1_ab_common", owner="ana@example.com", created_at=now - 2 * hour),
    ]
    app = cand_app(pr_tree(), schemas=FakeSchemas(listed))
    assert cpost(make_client, app=app).status_code == 200
    # Only this user's own try schema from over an hour ago is swept, before the run; the run's schema comes after.
    old = "skill_hub.run_try_aaaaaaaaaa_common"
    run = try_schema(app, before=[old])
    assert app.schemas.deleted == [old, run]


def test_the_live_copy_is_hidden_whatever_the_case_of_its_name():
    class Inner:
        def list_tools(self):
            return [
                SimpleNamespace(name="skill_cat.dept_x.skill"),
                SimpleNamespace(name="skill_cat.dept_x.other"),
            ]

        def call_tool(self, name, arguments):
            return "ok"

    hidden = try_panel._Hidden(Inner(), frozenset({"cat.dept_x.skill"}), RuntimeError)
    assert [t.name for t in hidden.list_tools()] == ["skill_cat.dept_x.other"]
    for name, args in (("SKILL_CAT.DEPT_X.SKILL", {}), ("load_skill", {"full_name": "CAT.Dept_X.Skill"})):
        with pytest.raises(RuntimeError):
            hidden.call_tool(name, args)
    assert hidden.call_tool("load_skill", {"full_name": "cat.dept_x.other"}) == "ok"


def test_the_department_schema_comes_before_common_and_common_is_not_repeated():
    cfg = make_cfg()
    assert try_panel._live_schemas(cfg, "equity_research") == [
        "skill_hub.dept_equity_research",
        "skill_hub.common",
    ]
    assert try_panel._live_schemas(cfg, "common") == ["skill_hub.common"]
