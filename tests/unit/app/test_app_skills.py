import json
import threading
from collections import Counter
from datetime import datetime

import pytest
from app_fakes import FakeApi, FakeWorkspace
from backend import authz, skill_ref
from backend.data import SqlDenied
from conftest import make_cfg
from databricks.sdk.errors import PermissionDenied

from skillhub.registry.uc_skills import SkillInfo

ME = "/api/2.0/preview/scim/v2/Me"


def me_api(groups):
    return FakeApi({ME: {"userName": "ana@example.com", "groups": [{"display": g} for g in groups]}})


class FakeUserSql:
    """Stands in for UserSql. Routes SQL by table. `denied` lists dept keys whose evals schema the user cannot read.
    `datasets` maps a skill name to its ds_ rows, and `rubrics` maps it to rubrics rows, newest first.
    `calls` records each statement and its parameters; `gate`, when set, is called first by every query."""

    def __init__(self, journal=None, matrix=None, denied=(), datasets=None, rubrics=None):
        self.journal = journal or {}
        self.matrix = matrix or []
        self.denied = set(denied)
        self.datasets = datasets or {}
        self.rubrics = rubrics or {}
        self.calls: list[tuple[str, dict]] = []
        self.gate = None

    def query(self, statement, params=None):
        self.calls.append((statement, dict(params or {})))
        if self.gate is not None:
            self.gate()
        if any(f"{d}_evals" in statement for d in self.denied):
            raise SqlDenied("no SELECT")
        if "skill_versions" in statement:
            return list(self.journal.get(params["dept"], []))
        if "eval_matrix" in statement:
            skill = (params or {}).get("skill")
            return [r for r in self.matrix if skill is None or r["skill"] == skill]
        if ".rubrics" in statement:
            return list(self.rubrics.get(params["skill"], []))
        for skill, rows in self.datasets.items():
            if f"ds_{skill.replace('-', '_')}" in statement:
                return list(rows)
        return []


class FakeUC:
    """Stands in for UCSkills. `visible` maps schema -> [SkillInfo]. `files` maps fqn -> {path: bytes}.
    `deny_user_schemas` makes the user's listing of those schemas raise PermissionDenied, and `deny_user_skills`
    does the same for the user's get and read_files. `read_by` records the client of each read_files call, and
    `listed` the (client, schema) of each list call. `gate`, when set, is called first by list and read_files."""

    def __init__(self, visible=None, files=None, deny_user_schemas=(), deny_user_skills=()):
        self.visible = visible or {}
        self.files = files or {}
        self.deny_user_schemas = set(deny_user_schemas)
        self.deny_user_skills = set(deny_user_skills)
        self.read_by: list[object] = []
        self.listed: list[tuple[object, str]] = []
        self.gate = None

    def factory(self):
        outer = self

        class _UC:
            def __init__(self, w):
                self.w = w

            def list(self, schema):
                outer.listed.append((self.w, schema))
                if outer.gate is not None:
                    outer.gate()
                if getattr(self.w, "is_user", False) and schema in outer.deny_user_schemas:
                    raise PermissionDenied("Provided OAuth token does not have required scopes")
                return list(outer.visible.get(schema, []))

            def get(self, fqn):
                if getattr(self.w, "is_user", False) and fqn in outer.deny_user_skills:
                    raise PermissionDenied("Provided OAuth token does not have required scopes")
                for infos in outer.visible.values():
                    for info in infos:
                        if info.fqn == fqn:
                            return info
                return None

            def read_files(self, fqn):
                if outer.gate is not None:
                    outer.gate()
                if getattr(self.w, "is_user", False) and fqn in outer.deny_user_skills:
                    raise PermissionDenied("Provided OAuth token does not have required scopes")
                outer.read_by.append(self.w)
                return dict(outer.files.get(fqn, {}))

        return _UC


def skill(fqn, desc="desc"):
    return SkillInfo(
        fqn=fqn,
        name=fqn.rsplit(".", 1)[1],
        bundle_name=None,
        description=desc,
        etag="e",
        finalize_time=None,
        comment=None,
    )


def user_w(groups=("skillhub-users",)):
    w = FakeWorkspace(api=me_api(groups))
    w.is_user = True
    return w


def app_w(readable=()):
    """The app's client. `readable` lists the schemas where the effective-permissions answer shows READ VOLUME;
    every other schema answers with USE_SCHEMA only."""
    eff = "/api/2.1/unity-catalog/effective-permissions/schema/"
    responses = {
        eff + schema: {
            "privilege_assignments": [
                {
                    "principal": "ana@example.com",
                    "privileges": [
                        {"privilege": p}
                        for p in (("READ_VOLUME", "USE_SCHEMA") if schema in readable else ("USE_SCHEMA",))
                    ],
                }
            ]
        }
        for schema in ("skill_hub.common", "skill_hub.dept_equity_research", "skill_hub.external")
    }
    responses["/api/2.1/unity-catalog/effective-permissions/catalog/skill_hub"] = {
        "privilege_assignments": [
            {"principal": "ana@example.com", "privileges": [{"privilege": "USE_CATALOG"}]}
        ]
    }
    w = FakeWorkspace(api=FakeApi(responses))
    w.is_user = False
    return w


@pytest.fixture
def uc(monkeypatch):
    fake = FakeUC(
        visible={
            "skill_hub.common": [skill("skill_hub.common.audit-xls")],
            "skill_hub.dept_equity_research": [skill("skill_hub.dept_equity_research.earnings-preview")],
        },
        files={
            "skill_hub.common.audit-xls": {
                "SKILL.md": b"# Audit",
                "evals/evals.json": b"{}",
                "logo.png": b"\x89PNG\r\n",
            }
        },
    )
    cls = fake.factory()
    monkeypatch.setattr(authz, "UCSkills", cls)
    import backend.api.skills as api_skills

    monkeypatch.setattr(api_skills, "UCSkills", cls)
    return fake


def test_me_reports_reviewer(make_client):
    w = user_w(("skillhub-reviewers",))
    body = make_client(user_w=w, cfg=make_cfg()).get("/api/me").json()
    assert body == {"user": "ana@example.com", "groups": ["skillhub-reviewers"], "is_reviewer": True}


def test_catalog_groups_by_schema_and_takes_worst_verdict(make_client, uc):
    journal = {
        "common": [
            {
                "dept": "common",
                "skill": "audit-xls",
                "action": "switched",
                "version": None,
                "to_version": 2,
                "from_version": None,
                "ts": datetime(2026, 10, 28),
            }
        ]
    }
    matrix = [
        {
            "skill": "audit-xls",
            "skill_version": 2,
            "model_profile": "claude-sonnet",
            "mode": "trigger",
            "verdict": "pass",
        },
        {
            "skill": "audit-xls",
            "skill_version": 2,
            "model_profile": "claude-sonnet",
            "mode": "effectiveness",
            "verdict": "warn",
        },
    ]
    c = make_client(
        user_w=user_w(), app_w=app_w(), cfg=make_cfg(), sql=FakeUserSql(journal=journal, matrix=matrix)
    )
    rows = c.get("/api/skills").json()
    audit = next(r for r in rows if r["name"] == "audit-xls")
    assert audit["dept"] == "common" and audit["live_version"] == 2
    assert audit["verdicts"] == {"claude-sonnet": "warn"}


def test_catalog_hides_schema_user_cannot_list(make_client, uc):
    uc.deny_user_schemas = {"skill_hub.dept_equity_research"}
    c = make_client(user_w=user_w(), app_w=app_w(), cfg=make_cfg(), sql=FakeUserSql())
    names = [r["name"] for r in c.get("/api/skills").json()]
    assert names == ["audit-xls"]  # the fallback found no READ VOLUME on the refused schema


def test_catalog_without_the_skills_scope_lists_through_the_fallback(make_client, uc):
    uc.deny_user_schemas = {"skill_hub.common", "skill_hub.dept_equity_research", "skill_hub.external"}
    app = app_w(readable=("skill_hub.dept_equity_research",))
    c = make_client(user_w=user_w(), app_w=app, cfg=make_cfg(), sql=FakeUserSql())
    r = c.get("/api/skills")
    assert r.status_code == 200
    # Listed as the app, filtered by the user's effective READ VOLUME.
    assert [x["name"] for x in r.json()] == ["earnings-preview"]


def test_catalog_skips_a_department_whose_fallback_fails_and_lists_the_rest(make_client, uc):
    """One department's effective-permissions check fails; the catalog still loads the readable ones."""
    uc.deny_user_schemas = {"skill_hub.dept_equity_research", "skill_hub.external"}
    # The app client answers common's schema... but has no answer for the other departments (a KeyError).
    app = app_w(readable=("skill_hub.common",))
    for schema in ("dept_equity_research", "external"):
        del app.api_client.responses[
            "/api/2.1/unity-catalog/effective-permissions/schema/skill_hub." + schema
        ]
    c = make_client(user_w=user_w(), app_w=app, cfg=make_cfg(), sql=FakeUserSql())
    r = c.get("/api/skills")
    assert r.status_code == 200
    assert [x["name"] for x in r.json()] == ["audit-xls"]


def test_catalog_resolves_the_identity_once_per_request(make_client, uc):
    uc.deny_user_schemas = {"skill_hub.common", "skill_hub.dept_equity_research", "skill_hub.external"}
    user = user_w()
    c = make_client(user_w=user, app_w=app_w(), cfg=make_cfg(), sql=FakeUserSql())
    assert c.get("/api/skills").status_code == 200
    me_calls = [call for call in user.api_client.calls if call[1].endswith("/Me")]
    assert len(me_calls) == 1


class Overlap:
    """A gate that holds each call until `n` calls are in flight at once. Calls made one after another never get
    there: the first one waits 5 s, and the gate records that it broke."""

    def __init__(self, n):
        self._barrier = threading.Barrier(n, timeout=5)
        self.broken = False

    def __call__(self):
        try:
            self._barrier.wait()
        except threading.BrokenBarrierError:
            self.broken = True


SCHEMAS = ("skill_hub.common", "skill_hub.dept_equity_research", "skill_hub.external")


def test_catalog_calls_for_n_departments_on_the_fallback(make_client, uc):
    """Per department: one listing as the user (refused for want of the scope), one effective-permissions answer
    for the schema and one listing as the app, then two statements. Per request: the identity and the catalog's
    effective permissions, once each. (Before: two schema answers and one catalog answer per department.)"""
    uc.visible["skill_hub.external"] = [skill("skill_hub.external.ext-skill")]
    uc.deny_user_schemas = set(SCHEMAS)
    user, app, sql = user_w(), app_w(readable=SCHEMAS), FakeUserSql()
    c = make_client(user_w=user, app_w=app, cfg=make_cfg(), sql=sql)
    assert len(c.get("/api/skills").json()) == len(SCHEMAS)
    eff = "/api/2.1/unity-catalog/effective-permissions/"
    assert Counter(path for _, path, _ in app.api_client.calls) == {
        **{f"{eff}schema/{schema}": 1 for schema in SCHEMAS},
        f"{eff}catalog/skill_hub": 1,
    }
    assert [path for _, path, _ in user.api_client.calls] == [ME]
    assert Counter(w.is_user for w, _ in uc.listed) == {True: len(SCHEMAS), False: len(SCHEMAS)}
    assert len(sql.calls) == 2 * len(SCHEMAS)


def test_catalog_reads_the_departments_side_by_side(make_client, uc):
    uc.visible["skill_hub.external"] = [skill("skill_hub.external.ext-skill")]
    uc.gate = listings = Overlap(len(SCHEMAS))  # every department's listing is in flight at once
    sql = FakeUserSql()
    sql.gate = reads = Overlap(2 * len(SCHEMAS))  # and then every journal and verdict read
    c = make_client(user_w=user_w(), app_w=app_w(), cfg=make_cfg(), sql=sql)
    r = c.get("/api/skills")
    assert r.status_code == 200 and not listings.broken and not reads.broken
    assert [x["name"] for x in r.json()] == ["audit-xls", "earnings-preview", "ext-skill"]  # config order


def test_detail_reads_the_files_and_eval_tables_side_by_side(make_client, uc):
    sql = FakeUserSql()
    uc.gate = sql.gate = together = Overlap(5)  # the files, cases, rubric, journal and verdicts at once
    c = make_client(user_w=user_w(), app_w=app_w(), cfg=make_cfg(), sql=sql)
    assert c.get("/api/skills/skill_hub.common.audit-xls").status_code == 200
    assert not together.broken and len(sql.calls) == 4


def test_detail_without_the_skills_scope_reads_files_as_the_app_after_the_fallback(make_client, uc):
    uc.deny_user_skills = {"skill_hub.common.audit-xls"}
    app = app_w(readable=("skill_hub.common",))
    c = make_client(user_w=user_w(), app_w=app, cfg=make_cfg(), sql=FakeUserSql())
    r = c.get("/api/skills/skill_hub.common.audit-xls")
    assert r.status_code == 200 and r.json()["files"] == {"SKILL.md": "# Audit"}
    assert uc.read_by == [app]


def test_detail_without_the_skills_scope_or_read_volume_is_404(make_client, uc):
    uc.deny_user_skills = {"skill_hub.common.audit-xls"}
    c = make_client(user_w=user_w(), app_w=app_w(), cfg=make_cfg(), sql=FakeUserSql())
    r = c.get("/api/skills/skill_hub.common.audit-xls")
    assert r.status_code == 404 and r.json() == {"detail": {"reason": "skill_not_found"}}
    assert uc.read_by == []


def test_catalog_search_matches_name_or_description(make_client, uc):
    c = make_client(user_w=user_w(), app_w=app_w(), cfg=make_cfg(), sql=FakeUserSql())
    names = [r["name"] for r in c.get("/api/skills", params={"q": "EARN"}).json()]
    assert names == ["earnings-preview"]


def test_detail_returns_text_files_only(make_client, uc):
    c = make_client(user_w=user_w(), app_w=app_w(), cfg=make_cfg(), sql=FakeUserSql())
    body = c.get("/api/skills/skill_hub.common.audit-xls").json()
    assert body["files"] == {"SKILL.md": "# Audit"}  # logo.png is not UTF-8, and evals/ is always hidden
    assert body["dept"] == "common" and body["name"] == "audit-xls"
    assert body["evals"] is None and body["rubric"] is None  # no dataset rows and no rubric row
    assert body["live_version"] is None and body["verdicts"] == {}  # never switched live
    assert body["eval_access"] == "ok"


AUDIT_JOURNAL = {
    "common": [
        {
            "dept": "common",
            "skill": "audit-xls",
            "action": "switched",
            "version": None,
            "to_version": 2,
            "from_version": None,
            "ts": datetime(2026, 10, 28),
        }
    ]
}
AUDIT_MATRIX = [
    {"skill": "audit-xls", "skill_version": 2, "model_profile": "claude-sonnet", "mode": m, "verdict": v}
    for m, v in (("trigger", "pass"), ("with_skill", "warn"), ("without_skill", "fail"))
] + [{"skill": "audit-xls", "skill_version": 1, "model_profile": "gpt", "mode": "trigger", "verdict": "fail"}]


def test_detail_has_the_live_version_and_verdicts_the_catalog_shows(make_client, uc):
    sql = FakeUserSql(journal=AUDIT_JOURNAL, matrix=AUDIT_MATRIX)
    c = make_client(user_w=user_w(), app_w=app_w(), cfg=make_cfg(), sql=sql)
    detail = c.get("/api/skills/skill_hub.common.audit-xls").json()
    # The skill page reads only this skill's journal and verdict rows.
    narrowed = [p for st, p in sql.calls if "skill_versions" in st or "eval_matrix" in st]
    assert narrowed == [{"dept": "common", "skill": "audit-xls"}, {"skill": "audit-xls"}]
    # The baseline row and the older version's row do not count.
    assert (detail["live_version"], detail["verdicts"]) == (2, {"claude-sonnet": "warn"})
    listed = next(r for r in c.get("/api/skills").json() if r["fqn"] == detail["fqn"])
    assert (listed["live_version"], listed["verdicts"]) == (2, {"claude-sonnet": "warn"})


def test_detail_eval_access_also_covers_the_journal_and_verdict_reads(make_client, uc):
    from backend.data import SqlError

    class JournalDown(FakeUserSql):
        def query(self, statement, params=None):
            if "skill_versions" in statement:
                raise SqlError("x")
            return super().query(statement, params)

    sql = JournalDown(matrix=AUDIT_MATRIX)
    c = make_client(user_w=user_w(), app_w=app_w(), cfg=make_cfg(), sql=sql)
    body = c.get("/api/skills/skill_hub.common.audit-xls").json()
    assert body["eval_access"] == "unavailable"
    assert body["live_version"] is None and body["verdicts"] == {} and "SKILL.md" in body["files"]


def test_detail_unreadable_skill_is_404(make_client, uc):
    uc.visible = {}  # UC get returns None for the user
    c = make_client(user_w=user_w(), app_w=app_w(), cfg=make_cfg(), sql=FakeUserSql())
    r = c.get("/api/skills/skill_hub.common.audit-xls")
    assert r.status_code == 404 and r.json() == {"detail": {"reason": "skill_not_found"}}


def test_detail_unknown_catalog_is_404(make_client, uc):
    c = make_client(user_w=user_w(), app_w=app_w(), cfg=make_cfg(), sql=FakeUserSql())
    assert c.get("/api/skills/other_catalog.common.audit-xls").status_code == 404


def test_detail_check_error_fails_closed_403(make_client, monkeypatch):
    class Broken:
        def __init__(self, w):
            pass

        def get(self, fqn):
            raise RuntimeError("scope missing")  # user check unavailable -> effective-permissions fallback

    monkeypatch.setattr(authz, "UCSkills", Broken)
    c = make_client(user_w=user_w(), app_w=FakeWorkspace(api=FakeApi()), cfg=make_cfg(), sql=FakeUserSql())
    r = c.get("/api/skills/skill_hub.common.audit-xls")
    # fallback needs effective-permissions, which the empty FakeApi lacks -> refused, fail closed
    assert r.status_code == 403 and r.json() == {"detail": {"reason": "authz_check_failed"}}


def dataset_row(case_id, prompt, *, should_trigger=True, expected_skill="audit-xls", reviewed=True):
    exp = {"should_trigger": should_trigger, "reviewed": reviewed}
    if expected_skill is not None:
        exp["expected_skill"] = expected_skill
    return {
        "inputs": json.dumps({"prompt": prompt, "case_id": case_id}),
        "expectations": json.dumps(exp),
        "tags": json.dumps({"kind": "trigger", "origin": "human"}),
    }


RUBRIC = {
    "skill_name": "audit-xls",
    "source_hash": "sha256:x",
    "generated_by": "claude-opus",
    "pass_score": 0.7,
    "reviewed": True,
    "criteria": [
        {"id": f"c{i}", "text": f"Criterion {i}", "type": "task_quality", "weight": 3, "source": "generated"}
        for i in range(1, 6)
    ],
}


def test_detail_takes_cases_and_rubric_from_eval_store(make_client, uc):
    uc.files["skill_hub.common.audit-xls"]["evals/evals.json"] = (
        b'{"decoy": true}'  # a stray file is never read
    )
    sql = FakeUserSql(
        datasets={"audit-xls": [dataset_row(1, "Audit the Q3 workbook")]},
        rubrics={
            "audit-xls": [{"rubric_json": json.dumps(RUBRIC), "reviewed": True, "ts": "2026-10-28T00:00:00"}]
        },
    )
    c = make_client(user_w=user_w(), app_w=app_w(), cfg=make_cfg(), sql=sql)
    body = c.get("/api/skills/skill_hub.common.audit-xls").json()
    case = body["evals"]["evals"][0]
    assert case["prompt"] == "Audit the Q3 workbook" and case["expected_skill"] == "audit-xls"
    assert body["rubric"]["reviewed"] is True and len(body["rubric"]["criteria"]) == 5
    assert not any(path.startswith("evals/") for path in body["files"])


def test_detail_reads_an_empty_expected_skill_as_none(make_client, uc):
    """The runner writes expected_skill "" for "no skill expected" (the dataset API rejects nulls)."""
    sql = FakeUserSql(
        datasets={"audit-xls": [dataset_row(2, "Unrelated", should_trigger=False, expected_skill="")]}
    )
    c = make_client(user_w=user_w(), app_w=app_w(), cfg=make_cfg(), sql=sql)
    [case] = c.get("/api/skills/skill_hub.common.audit-xls").json()["evals"]["evals"]
    assert case["id"] == 2 and case["expected_skill"] is None


def test_detail_reads_dataset_rows_in_the_table_layout_with_wrapped_expectations(make_client, uc):
    """The MLflow dataset table stores each expectation as {"value": ...} and case_id as a float. These rows have
    the layout of a real ds_<skill> table."""
    rows = [
        {
            "inputs": '{"prompt":"Draft a polite email to my landlord.","case_id":15.0}',
            "expectations": '{"should_trigger":{"value":false},"expected_skill":{"value":""},"reviewed":{"value":true}}',
            "tags": '{"kind":"trigger","origin":"generated"}',
            "last_update_time": "2026-10-29T00:43:12.351Z",
        },
        {
            "inputs": '{"prompt":"Audit the cash flow sheet.","case_id":101.0}',
            "expectations": json.dumps(
                {
                    "expected_skill": {"value": "audit-xls"},
                    "reviewed": {"value": False},
                    "should_trigger": {"value": True},
                    "fixtures": {"value": ["northwind/model.xlsx"]},
                    "guidelines": {"value": ["Finds the sign error at CF!E7"]},
                    "bug_scope": {"value": ["wc_sign"]},
                }
            ),
            "tags": '{"kind":"effectiveness","origin":"generated"}',
            "last_update_time": "2026-10-29T00:43:12.351Z",
        },
    ]
    c = make_client(
        user_w=user_w(), app_w=app_w(), cfg=make_cfg(), sql=FakeUserSql(datasets={"audit-xls": rows})
    )
    body = c.get("/api/skills/skill_hub.common.audit-xls").json()
    assert body["eval_access"] == "ok"
    trigger, effect = body["evals"]["evals"]
    assert (trigger["id"], trigger["should_trigger"], trigger["expected_skill"]) == (15, False, None)
    assert trigger["kind"] == "trigger" and trigger["reviewed"] is True
    assert (effect["id"], effect["kind"], effect["should_trigger"]) == (101, "effectiveness", True)
    assert effect["expected_skill"] == "audit-xls" and effect["reviewed"] is False
    assert effect["assertions"] == ["Finds the sign error at CF!E7"]


def test_detail_shows_one_case_per_id_when_the_dataset_holds_an_edited_prompt(make_client, uc, caplog):
    """Dataset records are keyed by their inputs, so editing a case's prompt leaves two rows with one case_id.
    EvalFile refuses repeated ids, so the detail keeps one row per id instead of failing with a 500."""
    rows = [dataset_row(1, "Old prompt"), dataset_row(1, "New prompt"), dataset_row(2, "Other")]
    c = make_client(
        user_w=user_w(), app_w=app_w(), cfg=make_cfg(), sql=FakeUserSql(datasets={"audit-xls": rows})
    )
    r = c.get("/api/skills/skill_hub.common.audit-xls")
    assert r.status_code == 200, r.text
    assert [case["id"] for case in r.json()["evals"]["evals"]] == [1, 2]
    assert "1 case id(s)" in caplog.text


def test_repeated_case_id_shows_the_newest_row_whatever_the_table_order(make_client, uc):
    old = {**dataset_row(1, "Old prompt"), "last_update_time": "2026-10-01T00:00:00"}
    new = {**dataset_row(1, "New prompt"), "last_update_time": "2026-10-02T00:00:00"}
    for rows in ([old, new], [new, old]):
        c = make_client(
            user_w=user_w(), app_w=app_w(), cfg=make_cfg(), sql=FakeUserSql(datasets={"audit-xls": rows})
        )
        cases = c.get("/api/skills/skill_hub.common.audit-xls").json()["evals"]["evals"]
        assert [x["prompt"] for x in cases] == ["New prompt"]


def test_repeated_case_id_without_timestamps_is_still_deterministic(make_client, uc):
    a, b = dataset_row(1, "alpha"), dataset_row(1, "beta")
    for rows in ([a, b], [b, a]):
        c = make_client(
            user_w=user_w(), app_w=app_w(), cfg=make_cfg(), sql=FakeUserSql(datasets={"audit-xls": rows})
        )
        cases = c.get("/api/skills/skill_hub.common.audit-xls").json()["evals"]["evals"]
        assert [x["prompt"] for x in cases] == ["beta"]


def test_detail_denied_eval_store_is_empty_not_an_error(make_client, uc):
    c = make_client(user_w=user_w(), app_w=app_w(), cfg=make_cfg(), sql=FakeUserSql(denied={"common"}))
    r = c.get("/api/skills/skill_hub.common.audit-xls")
    assert r.status_code == 200
    assert r.json()["evals"] is None and r.json()["rubric"] is None
    assert r.json()["eval_access"] == "denied"
    assert "SKILL.md" in r.json()["files"]


def test_versions_returns_only_this_skill(make_client, uc):
    journal = {
        "common": [
            {
                "dept": "common",
                "skill": "audit-xls",
                "action": "published",
                "version": 1,
                "actor": "a@x.com",
                "ts": "2026-10-28T00:00:00",
            },
            {
                "dept": "common",
                "skill": "xlsx-author",
                "action": "published",
                "version": 1,
                "actor": "a@x.com",
                "ts": "2026-10-28T00:00:00",
            },
        ]
    }
    c = make_client(user_w=user_w(), app_w=app_w(), cfg=make_cfg(), sql=FakeUserSql(journal=journal))
    rows = c.get("/api/skills/skill_hub.common.audit-xls/versions").json()
    assert [r["skill"] for r in rows] == ["audit-xls"]


def test_versions_denied_evals_is_403(make_client, uc):
    c = make_client(user_w=user_w(), app_w=app_w(), cfg=make_cfg(), sql=FakeUserSql(denied={"common"}))
    r = c.get("/api/skills/skill_hub.common.audit-xls/versions")
    assert r.status_code == 403 and r.json() == {"detail": {"reason": "evals_not_readable"}}


def test_live_version_rules():
    rows = [
        {"action": "published", "version": 1, "to_version": None},
        {"action": "published", "version": 2, "to_version": None},
        {"action": "switch_started", "version": None, "to_version": 1},
        {"action": "switched", "version": None, "to_version": 1},
    ]
    assert skill_ref.live_version(rows) == 1
    assert skill_ref.live_version([{"action": "retired", "version": None, "to_version": None}]) is None


def test_worst_verdict_order():
    assert skill_ref.worst_verdict(["pass", "warn"]) == "warn"
    assert skill_ref.worst_verdict(["advisory", "inconclusive", "pass"]) == "inconclusive"
    assert skill_ref.worst_verdict(["pass", "fail", "warn"]) == "fail"


def test_live_version_published_never_makes_live_and_retired_clears():
    pub = {"action": "published", "version": 3, "to_version": None}
    assert skill_ref.live_version([pub]) is None
    sw = {"action": "switched", "version": None, "to_version": 3}
    assert skill_ref.live_version([pub, sw]) == 3
    assert skill_ref.live_version([sw, {"action": "retired", "to_version": None}]) is None
    assert skill_ref.live_version([sw, {"action": "retired"}, {"action": "switched", "to_version": 4}]) == 4


def test_worst_verdict_null_unknown_and_baseline_semantics():
    assert skill_ref.worst_verdict(["pass", None]) == "inconclusive"
    assert skill_ref.worst_verdict(["fail", "weird"]) == "weird"


def test_unknown_department_is_404_not_empty(make_client, uc, monkeypatch):
    import backend.api.skills as api_skills

    def boom(*a, **k):
        raise api_skills.queries.UnknownDepartment("x")

    monkeypatch.setattr(api_skills.queries, "journal_sql", boom)
    c = make_client(user_w=user_w(), app_w=app_w(), cfg=make_cfg(), sql=FakeUserSql())
    r = c.get("/api/skills/skill_hub.common.audit-xls/versions")
    assert r.status_code == 404 and r.json() == {"detail": {"reason": "department_not_found"}}


def test_sql_errors_map_without_raw_text(make_client, uc):
    from backend.data import SqlError

    class Failing(FakeUserSql):
        def query(self, statement, params=None):
            raise SqlError("secret server text")

    c = make_client(user_w=user_w(), app_w=app_w(), cfg=make_cfg(), sql=Failing())
    r = c.get("/api/skills/skill_hub.common.audit-xls/versions")
    assert r.status_code == 502 and r.json() == {"detail": {"reason": "sql_failed"}}
    assert "secret" not in r.text


def test_detail_with_a_sql_fault_still_shows_the_skill(make_client, uc, caplog):
    from backend.data import SqlError

    class Failing(FakeUserSql):
        def query(self, statement, params=None):
            raise SqlError("secret server text")

    c = make_client(user_w=user_w(), app_w=app_w(), cfg=make_cfg(), sql=Failing())
    with caplog.at_level("WARNING", logger="skillhub.app.skills"):
        r = c.get("/api/skills/skill_hub.common.audit-xls")
    assert r.status_code == 200
    body = r.json()
    assert body["files"] == {"SKILL.md": "# Audit"}
    assert body["evals"] is None and body["rubric"] is None and body["eval_access"] == "unavailable"
    assert "secret" not in r.text and any("unavailable" in m for m in caplog.messages)


def test_versions_without_a_journal_table_is_an_empty_history(make_client, uc):
    class NoJournal(FakeUserSql):
        def query(self, statement, params=None):
            if "skill_versions" in statement:
                raise SqlDenied("no table", not_found=True)
            return super().query(statement, params)

    c = make_client(user_w=user_w(), app_w=app_w(), cfg=make_cfg(), sql=NoJournal())
    r = c.get("/api/skills/skill_hub.common.audit-xls/versions")
    assert r.status_code == 200 and r.json() == []


def test_parse_fqn_refuses_a_trailing_newline():
    from fastapi import HTTPException

    cfg = make_cfg()
    assert skill_ref.parse_fqn(cfg, "skill_hub.common.audit-xls") == ("common", "audit-xls")
    with pytest.raises(HTTPException) as exc:
        skill_ref.parse_fqn(cfg, "skill_hub.common.audit-xls\n")
    assert exc.value.status_code == 404


def test_sql_denied_maps_to_403_reasons(monkeypatch):
    import backend.main as main
    from fastapi.testclient import TestClient

    real = main._include_routers

    def with_probe(app):
        real(app)

        @app.get("/api/_denied")
        def denied(nf: bool = False):
            raise SqlDenied("x", not_found=nf)

    monkeypatch.setattr(main, "_include_routers", with_probe)
    c = TestClient(main.create_app())
    assert c.get("/api/_denied").json() == {"detail": {"reason": "no_access"}}
    r = c.get("/api/_denied", params={"nf": "true"})
    assert r.status_code == 403 and r.json() == {"detail": {"reason": "results_unavailable"}}


def test_missing_eval_tables_mean_nothing_generated_yet(make_client, uc):
    class NoTables(FakeUserSql):
        def query(self, statement, params=None):
            if "ds_" in statement or ".rubrics" in statement:
                raise SqlDenied("gone", not_found=True)
            return super().query(statement, params)

    c = make_client(user_w=user_w(), app_w=app_w(), cfg=make_cfg(), sql=NoTables())
    body = c.get("/api/skills/skill_hub.common.audit-xls").json()
    assert body["eval_access"] == "ok" and body["evals"] is None and body["rubric"] is None


def test_catalog_marks_denied_and_unavailable_departments_only(make_client, uc):
    from backend.data import SqlError

    uc.visible["skill_hub.external"] = [skill("skill_hub.external.ext-skill")]

    class Mixed(FakeUserSql):
        def query(self, statement, params=None):
            if "common_evals" in statement:
                raise SqlDenied("no SELECT")
            if "dept_equity_research_evals" in statement:
                raise SqlError("secret server text")
            return super().query(statement, params)

    c = make_client(user_w=user_w(), app_w=app_w(), cfg=make_cfg(), sql=Mixed())
    r = c.get("/api/skills")
    assert r.status_code == 200
    access = {row["name"]: row["eval_access"] for row in r.json()}
    assert access == {"audit-xls": "denied", "earnings-preview": "unavailable", "ext-skill": "ok"}
    assert "secret" not in r.text


def test_catalog_sql_failure_logs_a_warning(make_client, uc, caplog):
    from backend.data import SqlError

    class Failing(FakeUserSql):
        def query(self, statement, params=None):
            raise SqlError("x")

    c = make_client(user_w=user_w(), app_w=app_w(), cfg=make_cfg(), sql=Failing())
    with caplog.at_level("WARNING", logger="skillhub.app.skills"):
        assert c.get("/api/skills").status_code == 200
    assert any("unavailable" in m for m in caplog.messages)


def test_dataset_cases_are_sorted_and_malformed_rows_skipped(make_client, uc):
    rows = [
        dataset_row(2, "second"),
        {"inputs": "{not json", "expectations": "{}", "tags": "{}"},
        dataset_row(1, "first"),
    ]
    c = make_client(
        user_w=user_w(), app_w=app_w(), cfg=make_cfg(), sql=FakeUserSql(datasets={"audit-xls": rows})
    )
    cases = c.get("/api/skills/skill_hub.common.audit-xls").json()["evals"]["evals"]
    assert [x["id"] for x in cases] == [1, 2]


def test_read_files_failures_map_to_404_and_502(make_client, uc, monkeypatch):
    import backend.api.skills as api_skills
    from databricks.sdk.errors import NotFound

    for exc, status, reason in (
        (NotFound("x"), 404, "skill_not_found"),
        (RuntimeError("secret"), 502, "uc_failed"),
    ):

        class Bad(FakeUC().factory()):
            def read_files(self, fqn, exc=exc):
                raise exc

            def get(self, fqn):
                return skill(fqn)

        monkeypatch.setattr(api_skills, "UCSkills", Bad)
        monkeypatch.setattr(authz, "UCSkills", Bad)
        c = make_client(user_w=user_w(), app_w=app_w(), cfg=make_cfg(), sql=FakeUserSql())
        r = c.get("/api/skills/skill_hub.common.audit-xls")
        assert r.status_code == status and r.json() == {"detail": {"reason": reason}}


def test_versions_rows_have_the_journal_shape(make_client, uc):
    journal = {
        "common": [
            {
                "dept": "common",
                "skill": "audit-xls",
                "action": "published",
                "version": 1,
                "actor": "a@x.com",
                "ts": datetime(2026, 10, 28),
            }
        ]
    }
    c = make_client(user_w=user_w(), app_w=app_w(), cfg=make_cfg(), sql=FakeUserSql(journal=journal))
    row = c.get("/api/skills/skill_hub.common.audit-xls/versions").json()[0]
    assert row["to_version"] is None and row["git_sha"] is None and row["ts"].startswith("2026-10-28")
