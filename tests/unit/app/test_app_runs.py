import re

import pytest
from app_fakes import FakeApi, FakeJobs, FakeWorkspace, run_obj
from backend import authz, jobs
from conftest import make_cfg
from databricks.sdk.errors import InternalError, PermissionDenied, ResourceConflict

from skillhub.registry.uc_skills import SkillInfo

ME = "/api/2.0/preview/scim/v2/Me"
FQN = "skill_hub.common.audit-xls"
BODY = {"skills": [FQN], "models": ["claude-sonnet"], "modes": ["trigger"]}


@pytest.fixture(autouse=True)
def _fresh_job_cache(monkeypatch):
    monkeypatch.delenv("SKILLHUB_JOB_ID_EVAL", raising=False)
    monkeypatch.delenv("SKILLHUB_JOB_ID_SWITCH", raising=False)
    jobs.clear_cache()
    yield
    jobs.clear_cache()


def user(name="ana@example.com", groups=("skillhub-users",)):
    return FakeWorkspace(api=FakeApi({ME: {"userName": name, "groups": [{"display": g} for g in groups]}}))


def reviewer():
    return user("rita@example.com", ("skillhub-reviewers",))


def readable(monkeypatch, ok=True, versions_ok=True):
    class _UC:
        def __init__(self, w):
            pass

        def get(self, fqn):
            if not ok or (not versions_ok and "_versions." in fqn):
                return None
            return SkillInfo(
                fqn=fqn,
                name="audit-xls",
                bundle_name=None,
                description=None,
                etag="e",
                finalize_time=None,
                comment=None,
            )

    monkeypatch.setattr(authz, "UCSkills", _UC)


class FakeUserSql:
    def __init__(self, rows):
        self.rows = rows

    def query(self, statement, params=None):
        return list(self.rows)


def history(*actions):
    return [
        {
            "skill": "audit-xls",
            "action": a,
            "version": v,
            "to_version": t,
            "from_version": rest[0] if rest else None,
            "ts": f"2026-10-28T00:00:{i:02d}",
        }
        for i, (a, v, t, *rest) in enumerate(actions, start=1)
    ]


PUB12 = history(("published", 1, None), ("published", 2, None), ("switched", None, 2))


def test_requested_by_is_token_identity(make_client, monkeypatch):
    readable(monkeypatch)
    app = FakeWorkspace(jobs=FakeJobs())
    c = make_client(user_w=user(), app_w=app, cfg=make_cfg(), sql=FakeUserSql([]))
    r = c.post("/api/eval-runs", json={**BODY, "requested_by": "mallory@example.com"})
    assert r.status_code == 200
    started = app.jobs.started[0]
    assert started["job_id"] == 11
    params = started["params"]
    assert params["requested_by"] == "ana@example.com"
    assert (
        params["skills"] == "common/audit-xls"
        and params["trigger_source"] == "hub"
        and params["source"] == "live"
    )
    # "" means unset.
    assert params["versions"] == "" and params["skills_root"] == "" and params["fixtures_root"] == ""
    assert None not in params.values()
    assert re.match(r"^hub_[0-9a-f]{8}$", r.json()["run_key"]) and params["run_key"] == r.json()["run_key"]
    assert r.json()["job_run_id"] == 99


def test_eval_run_403_when_skill_unreadable(make_client, monkeypatch):
    readable(monkeypatch, ok=False)
    app = FakeWorkspace(jobs=FakeJobs())
    c = make_client(user_w=user(), app_w=app, cfg=make_cfg(), sql=FakeUserSql([]))
    r = c.post("/api/eval-runs", json=BODY)
    assert r.status_code == 403 and r.json() == {"detail": {"reason": "skill_not_readable"}}
    assert app.jobs.started == []


def test_eval_run_unknown_model_is_422(make_client, monkeypatch):
    readable(monkeypatch)
    c = make_client(user_w=user(), app_w=FakeWorkspace(jobs=FakeJobs()), cfg=make_cfg(), sql=FakeUserSql([]))
    r = c.post("/api/eval-runs", json={**BODY, "models": ["no-such-model"]})
    assert r.status_code == 422 and r.json() == {"detail": {"reason": "unknown_model"}}


@pytest.mark.parametrize(
    ("exc", "status", "reason"),
    [
        (ResourceConflict("busy"), 409, "job_conflict"),
        (PermissionDenied("no CAN_MANAGE_RUN secret-detail"), 403, "job_forbidden"),
        (InternalError("boom secret-detail"), 502, "job_failed"),
    ],
)
def test_eval_run_job_errors_map_to_reasons(make_client, monkeypatch, exc, status, reason):
    readable(monkeypatch)
    app = FakeWorkspace(jobs=FakeJobs(run_now_raises=exc))
    c = make_client(user_w=user(), app_w=app, cfg=make_cfg(), sql=FakeUserSql([]))
    r = c.post("/api/eval-runs", json=BODY)
    assert r.status_code == status and r.json() == {"detail": {"reason": reason}}
    assert "secret-detail" not in r.text


def test_eval_run_missing_job_is_404(make_client, monkeypatch):
    readable(monkeypatch)
    app = FakeWorkspace(jobs=FakeJobs(job_ids={}))
    c = make_client(user_w=user(), app_w=app, cfg=make_cfg(), sql=FakeUserSql([]))
    r = c.post("/api/eval-runs", json=BODY)
    assert r.status_code == 404 and r.json() == {"detail": {"reason": "job_not_found"}}


def test_job_id_env_var_wins_and_skips_lookup(make_client, monkeypatch):
    readable(monkeypatch)
    monkeypatch.setenv("SKILLHUB_JOB_ID_EVAL", "777")
    app = FakeWorkspace(jobs=FakeJobs(job_ids={}))
    c = make_client(user_w=user(), app_w=app, cfg=make_cfg(), sql=FakeUserSql([]))
    assert c.post("/api/eval-runs", json=BODY).status_code == 200
    assert app.jobs.started[0]["job_id"] == 777 and app.jobs.list_calls == 0


def test_empty_job_id_env_falls_back_to_name_lookup_and_caches(make_client, monkeypatch):
    readable(monkeypatch)
    monkeypatch.setenv("SKILLHUB_JOB_ID_EVAL", "")
    app = FakeWorkspace(jobs=FakeJobs())
    c = make_client(user_w=user(), app_w=app, cfg=make_cfg(), sql=FakeUserSql([]))
    assert c.post("/api/eval-runs", json=BODY).status_code == 200
    assert c.post("/api/eval-runs", json=BODY).status_code == 200
    assert [s["job_id"] for s in app.jobs.started] == [11, 11] and app.jobs.list_calls == 1


def test_duplicate_job_name_is_not_found():
    class Dup(FakeJobs):
        def list(self, name=None, **_):
            return super().list(name=name) * 2

    with pytest.raises(jobs.JobNotFound):
        jobs.job_id(FakeWorkspace(jobs=Dup()), "skill-eval")


def test_malformed_job_id_env_is_job_not_found(make_client, monkeypatch):
    readable(monkeypatch)
    monkeypatch.setenv("SKILLHUB_JOB_ID_EVAL", "abc")
    c = make_client(user_w=user(), app_w=FakeWorkspace(jobs=FakeJobs()), cfg=make_cfg(), sql=FakeUserSql([]))
    r = c.post("/api/eval-runs", json=BODY)
    assert r.status_code == 404 and r.json() == {"detail": {"reason": "job_not_found"}}


def test_eval_run_pins_a_published_version(make_client, monkeypatch):
    readable(monkeypatch)
    app = FakeWorkspace(jobs=FakeJobs())
    c = make_client(user_w=user(), app_w=app, cfg=make_cfg(), sql=FakeUserSql(PUB12))
    r = c.post("/api/eval-runs", json={**BODY, "versions": {FQN: 1}})
    assert r.status_code == 200
    assert app.jobs.started[0]["params"]["versions"] == "common/audit-xls=1"


def test_eval_run_unpublished_version_is_404(make_client, monkeypatch):
    readable(monkeypatch)
    c = make_client(
        user_w=user(),
        app_w=FakeWorkspace(jobs=FakeJobs()),
        cfg=make_cfg(),
        sql=FakeUserSql(history(("published", 1, None))),
    )
    r = c.post("/api/eval-runs", json={**BODY, "versions": {FQN: 9}})
    assert r.status_code == 404 and r.json() == {"detail": {"reason": "version_not_found"}}


def test_eval_run_version_for_unrequested_skill_is_422(make_client, monkeypatch):
    readable(monkeypatch)
    c = make_client(
        user_w=user(), app_w=FakeWorkspace(jobs=FakeJobs()), cfg=make_cfg(), sql=FakeUserSql(PUB12)
    )
    r = c.post("/api/eval-runs", json={**BODY, "versions": {"skill_hub.common.other": 1}})
    assert r.status_code == 422 and r.json() == {"detail": {"reason": "version_not_requested"}}


def test_eval_run_pin_read_denied_is_403(make_client, monkeypatch):
    from backend.data import SqlDenied

    class Denied:
        def query(self, *_a, **_k):
            raise SqlDenied("denied")

    readable(monkeypatch)
    c = make_client(user_w=user(), app_w=FakeWorkspace(jobs=FakeJobs()), cfg=make_cfg(), sql=Denied())
    r = c.post("/api/eval-runs", json={**BODY, "versions": {FQN: 1}})
    assert r.status_code == 403 and r.json() == {"detail": {"reason": "evals_not_readable"}}


def test_status_visible_to_owner_and_reviewer_only(make_client):
    runs = {99: run_obj({"requested_by": "ana@example.com", "run_key": "hub_0123abcd"})}
    app = FakeWorkspace(jobs=FakeJobs(runs=runs))
    owner = make_client(user_w=user("ana@example.com"), app_w=app, cfg=make_cfg()).get("/api/eval-runs/99")
    assert owner.status_code == 200
    assert owner.json() == {"state": "TERMINATED", "result_state": "SUCCESS", "run_key": "hub_0123abcd"}
    other = make_client(user_w=user("bob@example.com"), app_w=app, cfg=make_cfg()).get("/api/eval-runs/99")
    assert other.status_code == 404 and other.json() == {"detail": {"reason": "run_not_found"}}
    rev = make_client(user_w=reviewer(), app_w=app, cfg=make_cfg()).get("/api/eval-runs/99")
    assert rev.status_code == 200


def test_status_running_has_null_result(make_client):
    runs = {5: run_obj({"requested_by": "ana@example.com", "run_key": "k"}, life="RUNNING", result=None)}
    c = make_client(user_w=user(), app_w=FakeWorkspace(jobs=FakeJobs(runs=runs)), cfg=make_cfg())
    assert c.get("/api/eval-runs/5").json() == {"state": "RUNNING", "result_state": None, "run_key": "k"}


def test_status_unknown_run_and_foreign_job_are_404(make_client):
    runs = {7: run_obj({"requested_by": "rita@example.com"}, job_id=555)}
    c = make_client(user_w=reviewer(), app_w=FakeWorkspace(jobs=FakeJobs(runs=runs)), cfg=make_cfg())
    assert c.get("/api/eval-runs/404").json() == {"detail": {"reason": "run_not_found"}}
    assert c.get("/api/eval-runs/7").status_code == 404  # not one of the Hub's Jobs, even for a reviewer


def test_switch_requires_reviewer(make_client, monkeypatch):
    readable(monkeypatch)
    app = FakeWorkspace(jobs=FakeJobs())
    c = make_client(user_w=user(), app_w=app, cfg=make_cfg(), sql=FakeUserSql(PUB12))
    r = c.post(f"/api/skills/{FQN}/switch", json={"to_version": 1})
    assert r.status_code == 403 and r.json() == {"detail": {"reason": "not_reviewer"}}
    assert app.jobs.started == []


def test_switch_starts_job_as_app_identity_with_actor(make_client, monkeypatch):
    readable(monkeypatch)
    app = FakeWorkspace(jobs=FakeJobs())
    c = make_client(user_w=reviewer(), app_w=app, cfg=make_cfg(), sql=FakeUserSql(PUB12))
    r = c.post(
        f"/api/skills/{FQN}/switch",
        json={"to_version": 1, "actor": "mallory@example.com", "dept": "external", "skill": "other"},
    )
    assert r.status_code == 200 and r.json() == {"job_run_id": 99}
    started = app.jobs.started[0]
    assert started["job_id"] == 12
    assert started["params"] == {
        "action": "switch",
        "dept": "common",
        "skill": "audit-xls",
        "version": "1",
        "git_sha": "",
        "eval_run_id": "",
        "actor": "rita@example.com",
    }


def test_switch_uses_switch_job_env_id(make_client, monkeypatch):
    readable(monkeypatch)
    monkeypatch.setenv("SKILLHUB_JOB_ID_SWITCH", "4242")
    app = FakeWorkspace(jobs=FakeJobs(job_ids={}))
    c = make_client(user_w=reviewer(), app_w=app, cfg=make_cfg(), sql=FakeUserSql(PUB12))
    assert c.post(f"/api/skills/{FQN}/switch", json={"to_version": 1}).status_code == 200
    assert app.jobs.started[0]["job_id"] == 4242


def test_switch_409_when_switch_job_run_is_active(make_client, monkeypatch):
    readable(monkeypatch)
    app = FakeWorkspace(jobs=FakeJobs(active=[{"dept": "common", "skill": "audit-xls"}]))
    c = make_client(user_w=reviewer(), app_w=app, cfg=make_cfg(), sql=FakeUserSql(PUB12))
    r = c.post(f"/api/skills/{FQN}/switch", json={"to_version": 1})
    assert r.status_code == 409 and r.json() == {"detail": {"reason": "switch_in_progress"}}
    assert app.jobs.started == []
    assert app.jobs.list_runs_calls == [{"job_id": 12, "active_only": True}]


def test_switch_of_another_skill_does_not_block(make_client, monkeypatch):
    readable(monkeypatch)
    app = FakeWorkspace(jobs=FakeJobs(active=[{"dept": "common", "skill": "other"}]))
    c = make_client(user_w=reviewer(), app_w=app, cfg=make_cfg(), sql=FakeUserSql(PUB12))
    assert c.post(f"/api/skills/{FQN}/switch", json={"to_version": 1}).status_code == 200


STARTED = PUB12 + history(("switch_started", None, 1, 2))
FAILED = PUB12 + history(("switch_failed", None, 1, 2))


@pytest.mark.parametrize("rows", [STARTED, FAILED])
def test_switch_recovery_reruns_interrupted_target_with_no_active_run(make_client, monkeypatch, rows):
    readable(monkeypatch)
    app = FakeWorkspace(jobs=FakeJobs())
    c = make_client(user_w=reviewer(), app_w=app, cfg=make_cfg(), sql=FakeUserSql(rows))
    assert c.post(f"/api/skills/{FQN}/switch", json={"to_version": 1}).status_code == 200  # to_version
    assert (
        c.post(f"/api/skills/{FQN}/switch", json={"to_version": 2}).status_code == 200
    )  # revert: from_version
    assert len(app.jobs.started) == 2


@pytest.mark.parametrize("rows", [STARTED, FAILED])
def test_switch_recovery_restricts_target(make_client, monkeypatch, rows):
    readable(monkeypatch)
    rows = [
        *rows[:-1],
        *history(("published", 3, None)),
        rows[-1],
    ]  # v3 exists; the tail stays the switch row
    app = FakeWorkspace(jobs=FakeJobs())
    c = make_client(user_w=reviewer(), app_w=app, cfg=make_cfg(), sql=FakeUserSql(rows))
    r = c.post(f"/api/skills/{FQN}/switch", json={"to_version": 3})
    assert r.status_code == 409 and r.json() == {"detail": {"reason": "finish_or_revert_switch"}}
    assert app.jobs.started == []


@pytest.mark.parametrize("rows", [STARTED, FAILED])
@pytest.mark.parametrize("later", ["publish_refused", "published", "drift"])
def test_switch_recovery_skips_journal_rows_that_are_not_switch_rows(make_client, monkeypatch, rows, later):
    """A publish refusal, a new version or a drift row after an unfinished switch does not hide it: the tail
    is the last switch row, as in reconcile."""
    readable(monkeypatch)
    rows = [
        *rows,
        *history(("published", 3, None)),
        *history((later, 4 if later == "published" else None, None)),
    ]
    app = FakeWorkspace(jobs=FakeJobs())
    c = make_client(user_w=reviewer(), app_w=app, cfg=make_cfg(), sql=FakeUserSql(rows))
    r = c.post(f"/api/skills/{FQN}/switch", json={"to_version": 3})
    assert r.status_code == 409 and r.json() == {"detail": {"reason": "finish_or_revert_switch"}}
    assert c.post(f"/api/skills/{FQN}/switch", json={"to_version": 1}).status_code == 200  # finish it
    assert len(app.jobs.started) == 1


def test_switch_409_when_already_live(make_client, monkeypatch):
    readable(monkeypatch)
    c = make_client(
        user_w=reviewer(), app_w=FakeWorkspace(jobs=FakeJobs()), cfg=make_cfg(), sql=FakeUserSql(PUB12)
    )
    r = c.post(f"/api/skills/{FQN}/switch", json={"to_version": 2})
    assert r.status_code == 409 and r.json() == {"detail": {"reason": "already_live"}}


def test_switch_unknown_or_unpublished_version_is_404(make_client, monkeypatch):
    readable(monkeypatch)
    c = make_client(
        user_w=reviewer(),
        app_w=FakeWorkspace(jobs=FakeJobs()),
        cfg=make_cfg(),
        sql=FakeUserSql(history(("published", 1, None), ("published", 3, None))),
    )
    for v in (2, 9):  # 2 is a gap in the published set
        r = c.post(f"/api/skills/{FQN}/switch", json={"to_version": v})
        assert r.status_code == 404 and r.json() == {"detail": {"reason": "version_not_found"}}


def test_switch_without_a_journal_table_is_version_not_found(make_client, monkeypatch):
    from backend.data import SqlDenied

    class NoJournal:
        def query(self, statement, params=None):
            raise SqlDenied("no table", not_found=True)

    readable(monkeypatch)
    c = make_client(user_w=reviewer(), app_w=FakeWorkspace(jobs=FakeJobs()), cfg=make_cfg(), sql=NoJournal())
    r = c.post(f"/api/skills/{FQN}/switch", json={"to_version": 1})
    assert r.status_code == 404 and r.json() == {"detail": {"reason": "version_not_found"}}


def test_eval_run_pin_with_a_trailing_newline_is_refused_before_any_query(make_client, monkeypatch):
    class NoQueries:
        def query(self, statement, params=None):
            raise AssertionError("no journal query for a malformed FQN")

    readable(monkeypatch)
    bad = FQN + "\n"
    c = make_client(user_w=user(), app_w=FakeWorkspace(jobs=FakeJobs()), cfg=make_cfg(), sql=NoQueries())
    r = c.post("/api/eval-runs", json={**BODY, "skills": [FQN, bad], "versions": {bad: 1}})
    assert r.status_code == 404 and r.json() == {"detail": {"reason": "skill_not_found"}}


def test_switch_unreadable_skill_is_403(make_client, monkeypatch):
    readable(monkeypatch, ok=False)
    c = make_client(
        user_w=reviewer(), app_w=FakeWorkspace(jobs=FakeJobs()), cfg=make_cfg(), sql=FakeUserSql(PUB12)
    )
    r = c.post(f"/api/skills/{FQN}/switch", json={"to_version": 1})
    assert r.status_code == 403 and r.json() == {"detail": {"reason": "skill_not_readable"}}


def test_eval_run_pin_unreadable_version_is_403(make_client, monkeypatch):
    readable(monkeypatch, versions_ok=False)  # the live skill is readable, `common_versions` is not
    app = FakeWorkspace(jobs=FakeJobs())
    c = make_client(user_w=user(), app_w=app, cfg=make_cfg(), sql=FakeUserSql(PUB12))
    r = c.post("/api/eval-runs", json={**BODY, "versions": {FQN: 1}})
    assert r.status_code == 403 and r.json() == {"detail": {"reason": "skill_not_readable"}}
    assert app.jobs.started == []


def test_eval_run_ignores_spoofed_body_fields(make_client, monkeypatch):
    readable(monkeypatch)
    app = FakeWorkspace(jobs=FakeJobs())
    c = make_client(user_w=user(), app_w=app, cfg=make_cfg(), sql=FakeUserSql([]))
    spoof = {"skills_root": "/evil", "fixtures_root": "/evil", "dept": "external", "source": "pr"}
    assert c.post("/api/eval-runs", json={**BODY, **spoof}).status_code == 200
    p = app.jobs.started[0]["params"]
    assert p["skills_root"] == "" and p["fixtures_root"] == "" and p["source"] == "live"
    assert p["skills"] == "common/audit-xls"


def test_eval_run_second_skill_unreadable_starts_nothing(make_client, monkeypatch):
    class _UC:
        def __init__(self, w):
            pass

        def get(self, fqn):
            if fqn.endswith("secret"):
                return None
            return SkillInfo(
                fqn=fqn,
                name="x",
                bundle_name=None,
                description=None,
                etag="e",
                finalize_time=None,
                comment=None,
            )

    monkeypatch.setattr(authz, "UCSkills", _UC)
    app = FakeWorkspace(jobs=FakeJobs())
    c = make_client(user_w=user(), app_w=app, cfg=make_cfg(), sql=FakeUserSql([]))
    r = c.post("/api/eval-runs", json={**BODY, "skills": [FQN, "skill_hub.common.secret"]})
    assert r.status_code == 403 and r.json() == {"detail": {"reason": "skill_not_readable"}}
    assert app.jobs.started == []


def test_eval_run_dedupes_and_caps(make_client, monkeypatch):
    readable(monkeypatch)
    app = FakeWorkspace(jobs=FakeJobs())
    c = make_client(user_w=user(), app_w=app, cfg=make_cfg(), sql=FakeUserSql([]))
    body = {"skills": [FQN, FQN], "models": ["claude-sonnet"] * 2, "modes": ["trigger", "trigger"]}
    assert c.post("/api/eval-runs", json=body).status_code == 200
    p = app.jobs.started[0]["params"]
    assert (p["skills"], p["models"], p["modes"]) == ("common/audit-xls", "claude-sonnet", "trigger")
    many = {**BODY, "skills": [f"skill_hub.common.s{i}" for i in range(26)]}
    assert c.post("/api/eval-runs", json=many).status_code == 422


class CountingJobs(FakeJobs):
    def __init__(self, raises, **kw):
        super().__init__(**kw)
        self.attempts = []
        self.raises = raises

    def run_now(self, job_id, job_parameters=None, **kw):
        self.attempts.append(job_id)
        raise self.raises


def test_invalid_parameter_on_a_configured_job_id_is_not_retried_and_is_not_job_not_found(
    make_client, monkeypatch
):
    from databricks.sdk.errors import InvalidParameterValue

    readable(monkeypatch)
    monkeypatch.setenv("SKILLHUB_JOB_ID_EVAL", "777")
    fj = CountingJobs(InvalidParameterValue("bad parameter secret-detail"))
    c = make_client(user_w=user(), app_w=FakeWorkspace(jobs=fj), cfg=make_cfg(), sql=FakeUserSql([]))
    r = c.post("/api/eval-runs", json=BODY)
    assert r.status_code == 502 and r.json() == {"detail": {"reason": "job_failed"}}
    assert fj.attempts == [777] and fj.list_calls == 0
    assert "secret-detail" not in r.text


def test_a_freshly_looked_up_job_id_is_not_retried():
    from databricks.sdk.errors import InvalidParameterValue

    fj = CountingJobs(InvalidParameterValue("bad parameter"))
    with pytest.raises(InvalidParameterValue):
        jobs.start_job(FakeWorkspace(jobs=fj), "skill-eval", {})
    assert fj.attempts == [11] and fj.list_calls == 1


def test_stale_cached_job_id_is_refreshed_once():
    from databricks.sdk.errors import NotFound

    class Stale(FakeJobs):
        def run_now(self, job_id, job_parameters=None, **kw):
            if job_id == 11:
                raise NotFound("gone")
            return super().run_now(job_id, job_parameters, **kw)

    fj = Stale()
    w = FakeWorkspace(jobs=fj)
    jobs.job_id(w, "skill-eval")  # caches 11
    fj.job_ids["skill-eval"] = 21  # the Job was recreated
    assert jobs.start_job(w, "skill-eval", {}) == 99
    assert fj.started[0]["job_id"] == 21


def test_status_run_id_must_be_positive(make_client):
    c = make_client(user_w=user(), app_w=FakeWorkspace(jobs=FakeJobs()), cfg=make_cfg())
    assert c.get("/api/eval-runs/0").status_code == 422


def test_status_get_run_denied_or_invalid_is_404(make_client):
    from databricks.sdk.errors import InvalidParameterValue

    class Bad(FakeJobs):
        def get_run(self, run_id, **_):
            raise PermissionDenied("x") if run_id == 1 else InvalidParameterValue("x")

    c = make_client(user_w=user(), app_w=FakeWorkspace(jobs=Bad()), cfg=make_cfg())
    for rid in (1, 2):
        r = c.get(f"/api/eval-runs/{rid}")
        assert r.status_code == 404 and r.json() == {"detail": {"reason": "run_not_found"}}


def test_status_one_missing_job_does_not_hide_the_other(make_client):
    runs = {99: run_obj({"actor": "rita@example.com", "run_key": ""}, job_id=12)}
    app = FakeWorkspace(jobs=FakeJobs(job_ids={"skill-switch": 12}, runs=runs))  # no skill-eval job
    c = make_client(user_w=reviewer(), app_w=app, cfg=make_cfg())
    assert c.get("/api/eval-runs/99").status_code == 200


def test_status_of_switch_run_owned_through_actor(make_client):
    runs = {99: run_obj({"actor": "rita@example.com"}, job_id=12)}
    app = FakeWorkspace(jobs=FakeJobs(runs=runs))
    ok = make_client(user_w=reviewer(), app_w=app, cfg=make_cfg()).get("/api/eval-runs/99")
    assert ok.status_code == 200 and ok.json()["run_key"] == ""
    other = make_client(user_w=user("bob@example.com"), app_w=app, cfg=make_cfg()).get("/api/eval-runs/99")
    assert other.status_code == 404
