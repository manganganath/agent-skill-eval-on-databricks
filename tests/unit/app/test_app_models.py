import re
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from app_fakes import FakeApi, FakeJobs, FakeWorkspace, ThreadSql, run_obj
from backend import jobs
from conftest import make_cfg

from skillhub.model_catalog import store
from skillhub.model_catalog.entry import CatalogEntry, ConnectionTest
from skillhub.probe import ProbeResult

ME = "/api/2.0/preview/scim/v2/Me"
NOW = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)
NOVA = {
    "name": "nova-lite",
    "kind": "external_model",
    "model_id": "amazon.nova-lite-v1:0",
    "model_class": "fast",
}
LLAMA = {
    "name": "llama",
    "kind": "ug_model",
    "endpoint": "databricks-llama-4-maverick",
    "model_class": "fast",
}
PROBE_JOB = 13


@pytest.fixture(autouse=True)
def _fresh_job_cache(monkeypatch):
    for env in ("SKILLHUB_JOB_ID_EVAL", "SKILLHUB_JOB_ID_SWITCH", "SKILLHUB_JOB_ID_PROBE"):
        monkeypatch.delenv(env, raising=False)
    jobs.clear_cache()
    yield
    jobs.clear_cache()


def user(name="ana@example.com", groups=("skillhub-users",)):
    return FakeWorkspace(api=FakeApi({ME: {"userName": name, "groups": [{"display": g} for g in groups]}}))


def reviewer():
    return user("rita@example.com", ("skillhub-reviewers",))


class ProbeJobs(FakeJobs):
    """FakeJobs with the probe Job and the output of its runs."""

    def __init__(self, logs="", **kw):
        super().__init__(job_ids={"skill-eval": 11, "skill-switch": 12, "skillhub-probe": PROBE_JOB}, **kw)
        self.logs = logs

    def get_run_output(self, run_id):
        return SimpleNamespace(logs=self.logs)


def catalog(*rows):
    cfg, sql = make_cfg(), ThreadSql()
    store.ensure_table(cfg, sql)
    for r in rows:
        store.insert_entry(sql, cfg, CatalogEntry(**r), "rita@example.com", NOW)
    return cfg, sql


def bedrock(**over):
    return {**NOVA, "route": "bedrock", "region": "us-east-1", **over}


def ug(**over):
    return {**LLAMA, "route": "serving", **over}


def passed_test(sql, cfg, row, job_run_id=7):
    entry = CatalogEntry(**row)
    store.record_test(
        sql, cfg, entry.name, entry.target(), ConnectionTest("passed", "ok", job_run_id).to_json(), NOW
    )


def client(make_client, cfg, sql, user_w=None, jobs_=None):
    return make_client(user_w=user_w or reviewer(), app_w=FakeWorkspace(jobs=jobs_ or ProbeJobs()), cfg=cfg,
                       app_sql=sql)  # fmt: skip


def writes(sql):
    return [s for s, _ in sql.calls if re.match(r"\s*(INSERT|UPDATE|DELETE|MERGE)", s)]


def test_everyone_reads_the_release_gate_models_first_then_the_catalog(make_client):
    cfg, sql = catalog(bedrock(enabled=True), ug())
    r = client(make_client, cfg, sql, user_w=user()).get(
        "/api/models", headers={"x-forwarded-access-token": "t"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["catalog_available"] is True and body["bedrock_region"] == "us-east-1"
    assert [(m["name"], m["kind"], m["release_gate"]) for m in body["models"]] == [
        ("claude-sonnet", "hub_yaml", True), ("gemini-flash", "hub_yaml", True),
        ("llama", "ug_model", False), ("nova-lite", "external_model", False),
    ]  # fmt: skip
    nova = body["models"][3]
    assert (nova["route"], nova["model_id"], nova["region"], nova["enabled"], nova["test"]) == (
        "bedrock", "amazon.nova-lite-v1:0", "us-east-1", True, None,
    )  # fmt: skip


def test_without_the_catalog_table_the_release_gate_models_are_still_listed(make_client):
    cfg = make_cfg()
    r = client(make_client, cfg, ThreadSql()).get("/api/models", headers={"x-forwarded-access-token": "t"})
    assert r.status_code == 200
    assert r.json()["catalog_available"] is False
    assert [m["name"] for m in r.json()["models"]] == ["claude-sonnet", "gemini-flash"]


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/api/models", NOVA),
        ("patch", "/api/models/nova-lite", {"enabled": False}),
        ("post", "/api/models/nova-lite/test", None),
        ("get", "/api/models/nova-lite/test/99", None),
        ("get", "/api/serving-endpoints", None),
    ],
)
def test_only_reviewers_change_or_test_models(make_client, method, path, body):
    cfg, sql = catalog(bedrock())
    before = len(writes(sql))
    c = client(make_client, cfg, sql, user_w=user())
    r = getattr(c, method)(path, **({"json": body} if body is not None else {}))
    assert r.status_code == 403 and r.json() == {"detail": {"reason": "not_reviewer"}}
    assert len(writes(sql)) == before


def test_a_reviewer_adds_a_bedrock_model_disabled_with_the_default_region(make_client):
    cfg, sql = catalog()
    r = client(make_client, cfg, sql).post("/api/models", json=NOVA)
    assert r.status_code == 201, r.json()
    out = r.json()
    assert (out["route"], out["region"], out["enabled"], out["harness"], out["updated_by"]) == (
        "bedrock", "us-east-1", False, "reference", "rita@example.com",
    )  # fmt: skip
    [row], _ = store.read_entries(sql, cfg)
    assert row.created_by == "rita@example.com" and not row.enabled


def test_a_reviewer_adds_a_model_on_unity_gateway(make_client):
    cfg, sql = catalog()
    r = client(make_client, cfg, sql).post("/api/models", json=LLAMA)
    assert r.status_code == 201
    assert (r.json()["route"], r.json()["endpoint"]) == ("serving", "databricks-llama-4-maverick")


@pytest.mark.parametrize(
    ("body", "status", "reason"),
    [
        ({**NOVA, "kind": "external_agent"}, 422, "kind_not_supported"),
        ({**NOVA, "name": "claude-sonnet"}, 409, "model_exists"),
        ({**NOVA, "model_id": None}, 422, "model_invalid"),
        ({**NOVA, "name": "default"}, 422, "model_invalid"),
        ({**LLAMA, "route": "ug-anthropic"}, 422, "model_invalid"),
        ({**NOVA, "harness": "codex"}, 422, "model_invalid"),
        ({**NOVA, "model_class": "huge"}, 422, "model_invalid"),
    ],
)
def test_a_model_that_cannot_be_registered_is_refused_and_nothing_is_written(
    make_client, body, status, reason
):
    cfg, sql = catalog()
    r = client(make_client, cfg, sql).post("/api/models", json=body)
    assert r.status_code == status and r.json()["detail"]["reason"] == reason
    assert writes(sql) == []


def test_an_invalid_model_lists_its_problems_in_plain_words(make_client):
    cfg, sql = catalog()
    r = client(make_client, cfg, sql).post("/api/models", json={**NOVA, "region": "Virginia", "name": "Nova"})
    errors = r.json()["detail"]["errors"]
    assert any(e.startswith("name:") for e in errors) and any(e.startswith("region:") for e in errors)


def test_a_taken_catalog_name_is_refused(make_client):
    cfg, sql = catalog(bedrock())
    r = client(make_client, cfg, sql).post("/api/models", json={**NOVA, "model_class": "frontier"})
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "model_exists"


def test_a_model_is_enabled_only_after_its_last_test_passed(make_client):
    cfg, sql = catalog(bedrock())
    c = client(make_client, cfg, sql)
    r = c.patch("/api/models/nova-lite", json={"enabled": True})
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "test_not_passed"
    passed_test(sql, cfg, bedrock())
    r = c.patch("/api/models/nova-lite", json={"enabled": True})
    assert r.status_code == 200 and r.json()["enabled"] is True
    assert store.read_entries(sql, cfg)[0][0].updated_by == "rita@example.com"


def test_a_target_change_disables_the_row_and_clears_its_test(make_client):
    cfg, sql = catalog(bedrock(enabled=True))
    passed_test(sql, cfg, bedrock())
    c = client(make_client, cfg, sql)
    r = c.patch("/api/models/nova-lite", json={"region": "us-west-2", "enabled": True})
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "test_not_passed"
    r = c.patch("/api/models/nova-lite", json={"region": "us-west-2"})
    assert r.status_code == 200
    assert (r.json()["region"], r.json()["enabled"], r.json()["test"]) == ("us-west-2", False, None)


def test_a_price_or_class_change_keeps_the_test_and_the_row_enabled(make_client):
    cfg, sql = catalog(bedrock(enabled=True))
    passed_test(sql, cfg, bedrock())
    r = client(make_client, cfg, sql).patch(
        "/api/models/nova-lite", json={"price_in": 0.06, "model_class": "frontier"}
    )
    assert r.status_code == 200
    assert (r.json()["enabled"], r.json()["test"]["status"], r.json()["price_in"]) == (True, "passed", 0.06)


@pytest.mark.parametrize(
    ("path", "body", "status", "reason"),
    [
        ("/api/models/gpt-x", {"enabled": False}, 404, "model_not_found"),
        ("/api/models/claude-sonnet", {"enabled": False}, 409, "release_gate_model"),
        ("/api/models/nova-lite", {"kind": "ug_model"}, 422, "model_invalid"),
    ],
)
def test_a_patch_that_names_no_catalog_row_or_a_fixed_field_is_refused(
    make_client, path, body, status, reason
):
    cfg, sql = catalog(bedrock())
    r = client(make_client, cfg, sql).patch(path, json=body)
    assert r.status_code == status and r.json()["detail"]["reason"] == reason


def test_a_test_starts_the_probe_job_with_the_rows_target(make_client):
    cfg, sql = catalog(bedrock())
    jobs_ = ProbeJobs(run_id=4242)
    r = client(make_client, cfg, sql, jobs_=jobs_).post("/api/models/nova-lite/test")
    assert r.status_code == 200 and r.json() == {"job_run_id": 4242}
    assert jobs_.started == [{"job_id": PROBE_JOB, "params": {
        "name": "nova-lite", "route": "bedrock", "endpoint": "", "model_id": "amazon.nova-lite-v1:0",
        "region": "us-east-1", "agent": "", "requested_by": "rita@example.com",
    }}]  # fmt: skip


def finished(params, result="SUCCESS", life="TERMINATED", job_id=PROBE_JOB):
    run = run_obj(params, life=life, result=result, job_id=job_id)
    run.run_id, run.tasks, run.end_time = 4242, [SimpleNamespace(run_id=4243)], 1_793_437_200_000
    return run


TARGET = {"name": "nova-lite", "route": "bedrock", "endpoint": "", "model_id": "amazon.nova-lite-v1:0",
          "region": "us-east-1", "requested_by": "rita@example.com"}  # fmt: skip


def test_a_finished_test_is_recorded_once_on_its_row(make_client):
    cfg, sql = catalog(bedrock())
    logs = "INFO start\n" + ProbeResult("nova-lite", "passed", "answered in 812 ms", 812).line() + "\n"
    jobs_ = ProbeJobs(logs=logs, runs={4242: finished(TARGET)})
    c = client(make_client, cfg, sql, jobs_=jobs_)
    r = c.get("/api/models/nova-lite/test/4242")
    assert r.status_code == 200
    assert r.json() == {
        "state": "passed",
        "message": "answered in 812 ms",
        "tested_at": "2026-10-31T09:00:00+00:00",
    }
    test = store.read_entries(sql, cfg)[0][0].last_test
    assert test == ConnectionTest("passed", "answered in 812 ms", 4242)
    assert c.get("/api/models/nova-lite/test/4242").status_code == 200
    assert store.read_entries(sql, cfg)[0][0].tested_at is not None


def test_a_failed_test_shows_its_message_and_without_output_says_where_to_look(make_client):
    cfg, sql = catalog(bedrock())
    fail = ProbeResult("nova-lite", "failed", "AccessDeniedException: not authorized to invoke").line()
    for logs, says in ((fail, "AccessDeniedException"), ("", "Open the skillhub-probe run in Jobs")):
        jobs_ = ProbeJobs(logs=logs, runs={4242: finished(TARGET, result="FAILED")})
        r = client(make_client, cfg, sql, jobs_=jobs_).get("/api/models/nova-lite/test/4242")
        assert r.json()["state"] == "failed" and says in r.json()["message"]


def test_a_running_test_records_nothing(make_client):
    cfg, sql = catalog(bedrock())
    jobs_ = ProbeJobs(runs={4242: finished(TARGET, result=None, life="RUNNING")})
    r = client(make_client, cfg, sql, jobs_=jobs_).get("/api/models/nova-lite/test/4242")
    assert r.json() == {"state": "running", "message": None, "tested_at": None}
    assert not [s for s in writes(sql) if s.lstrip().startswith("UPDATE")]


def test_a_result_for_a_target_the_row_no_longer_has_is_stale_and_not_recorded(make_client):
    cfg, sql = catalog(bedrock(region="us-west-2"))
    jobs_ = ProbeJobs(logs=ProbeResult("nova-lite", "passed", "ok").line(), runs={4242: finished(TARGET)})
    r = client(make_client, cfg, sql, jobs_=jobs_).get("/api/models/nova-lite/test/4242")
    assert r.json()["state"] == "stale"
    assert store.read_entries(sql, cfg)[0][0].last_test is None


@pytest.mark.parametrize(
    "run",
    [finished(TARGET, job_id=11), finished({**TARGET, "name": "llama"})],
    ids=["another-job", "another-model"],
)
def test_a_run_that_is_not_this_models_test_is_not_found(make_client, run):
    cfg, sql = catalog(bedrock())
    r = client(make_client, cfg, sql, jobs_=ProbeJobs(runs={4242: run})).get(
        "/api/models/nova-lite/test/4242"
    )
    assert r.status_code == 404 and r.json()["detail"]["reason"] == "run_not_found"


def test_no_request_ever_deletes_a_row(make_client):
    cfg, sql = catalog(bedrock())
    c = client(make_client, cfg, sql)
    c.post("/api/models", json=LLAMA)
    c.patch("/api/models/nova-lite", json={"region": "us-west-2"})
    c.patch("/api/models/llama", json={"enabled": False})
    assert not [s for s, _ in sql.calls if re.search(r"\b(DELETE|TRUNCATE|DROP)\b", s, re.I)]


def test_a_reviewer_lists_the_chat_endpoints_with_their_own_token(make_client):
    endpoints = [SimpleNamespace(name="databricks-llama-4-maverick", task="llm/v1/chat"),
                 SimpleNamespace(name="databricks-gte-large-en", task="llm/v1/embeddings"),
                 SimpleNamespace(name="custom-agent", task=None)]  # fmt: skip
    rita = reviewer()
    rita.serving_endpoints = SimpleNamespace(list=lambda: iter(endpoints))
    cfg, sql = catalog()
    r = client(make_client, cfg, sql, user_w=rita).get("/api/serving-endpoints")
    assert [e["name"] for e in r.json()] == ["custom-agent", "databricks-llama-4-maverick"]


@pytest.mark.parametrize("field", ["model_class", "enabled"])
def test_a_patch_cannot_null_a_required_field(make_client, field):
    cfg, sql = catalog(bedrock(enabled=True))
    passed_test(sql, cfg, bedrock())
    before = len(writes(sql))
    r = client(make_client, cfg, sql).patch("/api/models/nova-lite", json={field: None})
    assert r.status_code == 422 and r.json()["detail"]["reason"] == "model_invalid"
    assert len(writes(sql)) == before


def test_an_external_agent_is_refused_whatever_else_the_body_holds(make_client):
    cfg, sql = catalog()
    body = {"name": "my-agent", "kind": "external_agent", "agent": "x", "harness": "codex"}
    r = client(make_client, cfg, sql).post("/api/models", json=body)
    assert r.status_code == 422 and r.json()["detail"]["reason"] == "kind_not_supported"
    assert writes(sql) == []
