"""Skill Hub drafts: a skill uploaded in the Hub to evaluate it before any pull request. Drafts are never published."""

import base64
import io
import json
import re
import stat
import zipfile

import pytest
from app_fakes import FakeApi, FakeJobs, FakeTree, FakeWorkspace
from backend import jobs
from conftest import make_cfg
from databricks.sdk.errors import PermissionDenied
from databricks.sdk.service.workspace import ImportFormat
from test_app_candidates import RowsSql, asserted_evals, evals_json, row, rubric_json, skill_md

from skillhub.naming import schema_base

ME = "/api/2.0/preview/scim/v2/Me"
EFF = "/api/2.1/unity-catalog/effective-permissions/"
DRAFTS = (
    "/Shared/skill-hub/skill_hub/drafts"  # <workspace_root>/<catalog>/drafts, as the Workspace API answers
)
DRAFT_KEY = re.compile(r"draft_[0-9a-f]{8}[0-9a-f]{16}")


@pytest.fixture(autouse=True)
def _fresh_job_cache(monkeypatch):
    monkeypatch.delenv("SKILLHUB_JOB_ID_EVAL", raising=False)
    jobs.clear_cache()
    yield
    jobs.clear_cache()


def user(groups=("skillhub-authors",), name="ana@example.com"):
    return FakeWorkspace(api=FakeApi({ME: {"userName": name, "groups": [{"display": g} for g in groups]}}))


def author(name="ana@example.com"):
    return user(("skillhub-authors",), name)


def reviewer():
    return user(("skillhub-reviewers",), "rita@example.com")


def app(tree, readable=("common",), principal="ana@example.com"):
    """The app's client: effective permissions show READ VOLUME on the `readable` departments for `principal`."""
    responses = {
        f"{EFF}schema/skill_hub.{schema_base(d)}": {
            "privilege_assignments": [
                {
                    "principal": principal,
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
        "privilege_assignments": [{"principal": principal, "privileges": [{"privilege": "USE_CATALOG"}]}]
    }
    return FakeWorkspace(api=FakeApi(responses), jobs=FakeJobs(active=[]), workspace=tree)


def skill_files(name="new-skill", rubric=True, extra=None):
    files = {
        "SKILL.md": skill_md(name),
        "references/guide.md": b"# Guide\n",
        "evals/evals.json": evals_json(name),
    }
    if rubric:
        files["evals/rubric.json"] = rubric_json(name)
    return {**files, **(extra or {})}


def zip_of(files, prefix="", symlinks=()):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, data in files.items():
            zf.writestr(prefix + path, data)
        for path, target in symlinks:
            info = zipfile.ZipInfo(prefix + path)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, target)
    return buf.getvalue()


def archive_body(files=None, dept="common", prefix="new-skill/", **kw):
    data = zip_of(skill_files() if files is None else files, prefix, **kw)
    return {"dept": dept, "archive": base64.b64encode(data).decode()}


def files_body(files=None, dept="common", prefix="new-skill/"):
    files = skill_files() if files is None else files
    return {
        "dept": dept,
        "files": [{"path": prefix + p, "content": base64.b64encode(d).decode()} for p, d in files.items()],
    }


def upload(make_client, body, *, user_w=None, tree=None, readable=("common",)):
    tree = tree if tree is not None else FakeTree()
    a = app(tree, readable=readable)
    c = make_client(user_w=user_w or author(), app_w=a, cfg=make_cfg(), sql=RowsSql())
    return c.post("/api/drafts", json=body), tree, a, c


def errors_of(r):
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["reason"] == "draft_invalid"
    return " | ".join(detail["errors"])


# --- upload and storage ---------------------------------------------------------------------------------


@pytest.mark.parametrize("make_body", [archive_body, files_body])
def test_an_author_uploads_a_draft_stored_with_its_marker_and_uploader(make_client, make_body):
    r, tree, _, _ = upload(make_client, make_body())
    assert r.status_code == 201, r.text
    body = r.json()
    key = body["run_key"]
    assert DRAFT_KEY.fullmatch(key) and (body["dept"], body["name"], body["has_rubric"]) == (
        "common",
        "new-skill",
        True,
    )
    base = f"{DRAFTS}/{key}"
    stored = {p[len(base) + 1 :] for p in tree.files if p.startswith(base + "/")}
    assert stored == {
        ".uploaded_at",
        "draft.json",
        "skills/common/new-skill/SKILL.md",
        "skills/common/new-skill/references/guide.md",
        "skills/common/new-skill/evals/evals.json",
        "skills/common/new-skill/evals/rubric.json",
    }
    assert (
        tree.files[f"{base}/.uploaded_at"].decode().strip().isdecimal()
    )  # the cleanup dates the folder by it
    meta = json.loads(tree.files[f"{base}/draft.json"])
    assert (meta["uploaded_by"], meta["dept"], meta["skill"], meta["has_rubric"]) == (
        "ana@example.com",
        "common",
        "new-skill",
        True,
    )
    assert set(tree.formats.values()) == {ImportFormat.RAW}  # files, never notebooks
    # Nothing is written outside drafts/: never in candidates/.
    assert all(p.startswith(base + "/") for p in tree.files)


def test_draft_ids_are_unguessable_and_sortable_by_time(make_client):
    first = upload(make_client, archive_body())[0].json()["run_key"]
    second = upload(make_client, archive_body())[0].json()["run_key"]
    assert first != second and first[6:14] <= second[6:14]  # 8 hex digits of the upload time, then 16 random


def test_a_zip_with_skill_md_at_the_top_is_accepted(make_client):
    r, tree, _, _ = upload(make_client, archive_body(prefix=""))
    assert r.status_code == 201, r.text
    assert any(p.endswith("/skills/common/new-skill/SKILL.md") for p in tree.files)


def test_fixtures_in_the_upload_are_stored_beside_the_skill_not_in_it(make_client):
    files = skill_files(extra={"fixtures/web_index.json": b"{}", "fixtures/docs/a.md": b"a"})
    r, tree, _, _ = upload(make_client, archive_body(files))
    key = r.json()["run_key"]
    assert f"{DRAFTS}/{key}/fixtures/docs/a.md" in tree.files
    assert not [p for p in tree.files if "/skills/" in p and "fixtures" in p]


def test_macos_archive_extras_are_ignored(make_client):
    files = skill_files(extra={".DS_Store": b"x"})
    data = zip_of(files, "new-skill/")
    buf = io.BytesIO(data)
    with zipfile.ZipFile(buf, "a") as zf:
        zf.writestr("__MACOSX/new-skill/._SKILL.md", b"x")
    r, tree, _, _ = upload(
        make_client, {"dept": "common", "archive": base64.b64encode(buf.getvalue()).decode()}
    )
    assert r.status_code == 201, r.text
    assert not [p for p in tree.files if "MACOSX" in p or "DS_Store" in p]


@pytest.mark.parametrize(
    ("files", "message"),
    [
        ({"references/guide.md": b"x", "evals/evals.json": evals_json("new-skill")}, "SKILL.md is missing"),
        ({"SKILL.md": skill_md("new-skill")}, "evals/evals.json is missing"),
        ({**skill_files(), "SKILL.md": b"# no frontmatter\n"}, "frontmatter"),
        ({**skill_files(), "SKILL.md": b"---\nname: new-skill\n---\n# x\n"}, "description"),
        ({**skill_files(), "SKILL.md": skill_md("other-name")}, "must equal folder name"),
        ({**skill_files(), "evals/evals.json": b"{not json"}, "evals/evals.json"),
        ({**skill_files(), "evals/rubric.json": b"{}"}, "evals/rubric.json"),
        (
            {
                **skill_files(),
                "evals/evals.json": json.dumps({"skill_name": "new-skill", "evals": []}).encode(),
            },
            "no cases",
        ),
    ],
)
def test_an_invalid_skill_is_refused_with_the_lint_messages(make_client, files, message):
    r, tree, _, _ = upload(make_client, archive_body(files))
    assert message in errors_of(r)
    assert tree.files == {} and tree.made == set()  # nothing is stored


def test_a_threshold_that_loosens_the_gate_is_refused(make_client):
    cases = json.loads(evals_json("new-skill"))
    cases["criteria"] = {"fail_rate": 0.1, "warn_rate": 0.2}  # looser than hub.yaml defaults
    r, _, _, _ = upload(
        make_client, archive_body({**skill_files(), "evals/evals.json": json.dumps(cases).encode()})
    )
    assert "may only tighten the gate" in errors_of(r)


@pytest.mark.parametrize(
    "bad", ["../evil.md", "/etc/passwd", "references/../../x", "C:/x.md", "a\\b.md", "x//y.md"]
)
def test_paths_that_escape_or_are_not_plain_relative_paths_are_refused(make_client, bad):
    for body in (
        archive_body(skill_files(extra={bad: b"x"}), prefix=""),
        files_body(skill_files(extra={bad: b"x"}), prefix=""),
    ):
        r, tree, _, _ = upload(make_client, body)
        assert "path" in errors_of(r) and tree.files == {}


def test_a_symlink_in_the_archive_is_refused(make_client):
    r, tree, _, _ = upload(make_client, archive_body(symlinks=[("references/link.md", "/etc/passwd")]))
    assert "symbolic link" in errors_of(r) and tree.files == {}


def test_an_archive_that_is_not_a_zip_is_refused(make_client):
    r, _, _, _ = upload(make_client, {"dept": "common", "archive": base64.b64encode(b"plain text").decode()})
    assert "zip" in errors_of(r)


def test_a_body_with_both_an_archive_and_files_is_refused(make_client):
    r, _, _, _ = upload(make_client, {**archive_body(), "files": files_body()["files"]})
    assert "archive or files" in errors_of(r)


def test_too_big_an_upload_is_refused(make_client):
    big = skill_files(
        extra={"references/big.md": b"x" * (5 * 1024 * 1024)}
    )  # compresses well: a zip bomb's shape
    r, tree, _, _ = upload(make_client, archive_body(big))
    assert r.status_code == 413 and r.json() == {"detail": {"reason": "draft_too_large"}}
    assert tree.files == {}


def test_too_many_files_are_refused(make_client):
    many = skill_files(extra={f"references/f{i}.md": b"x" for i in range(250)})
    r, _, _, _ = upload(make_client, archive_body(many))
    assert r.status_code == 413 and r.json() == {"detail": {"reason": "draft_too_large"}}


def test_a_request_body_over_the_cap_is_refused_before_it_is_parsed(make_client):
    r, _, _, _ = upload(make_client, {"dept": "common", "archive": "A" * (9 * 1024 * 1024)})
    assert r.status_code == 413 and r.json() == {"detail": {"reason": "draft_too_large"}}


def test_a_user_who_is_neither_author_nor_reviewer_cannot_upload(make_client):
    r, tree, a, _ = upload(make_client, archive_body(), user_w=user(("skillhub-users",)))
    assert r.status_code == 403 and r.json() == {"detail": {"reason": "not_author"}}
    assert tree.calls == [] and a.api_client.calls == []


def test_an_author_cannot_upload_to_a_department_they_cannot_read(make_client):
    r, tree, _, _ = upload(make_client, archive_body(dept="equity_research"), readable=("common",))
    assert r.status_code == 403 and r.json() == {"detail": {"reason": "department_not_allowed"}}
    assert tree.files == {}


def test_a_reviewer_uploads_to_any_department(make_client):
    r, _, _, _ = upload(make_client, archive_body(dept="external"), user_w=reviewer(), readable=())
    assert r.status_code == 201, r.text


def test_an_unknown_department_is_refused(make_client):
    r, _, _, _ = upload(make_client, archive_body(dept="finance"))
    assert "unknown department" in errors_of(r)


def test_a_store_the_app_may_not_write_is_reported_and_nothing_is_left(make_client):
    tree = FakeTree()
    made = tree.mkdirs

    def refuse(path):  # no CAN_MANAGE on drafts/: the draft's folder cannot be created
        if "/drafts/" in path:
            raise PermissionDenied("no CAN_MANAGE on drafts")
        made(path)

    tree.mkdirs = refuse
    r, _, _, _ = upload(make_client, archive_body(), tree=tree)
    assert r.status_code == 502 and r.json() == {"detail": {"reason": "draft_store_failed"}}
    assert tree.files == {}


# --- listing, detail and run ----------------------------------------------------------------------------


def stored_draft(
    make_client, *, user_w=None, dept="common", rubric=True, fixtures=False, readable=("common",)
):
    extra = {"fixtures/index.json": b"{}"} if fixtures else None
    tree = FakeTree()
    r, _, _, _ = upload(
        make_client,
        archive_body(skill_files(rubric=rubric, extra=extra), dept=dept),
        user_w=user_w or author(),
        tree=tree,
        readable=readable,
    )
    assert r.status_code == 201, r.text
    return tree, r.json()["run_key"]


def test_drafts_are_listed_badged_as_drafts_for_their_uploader(make_client):
    tree, key = stored_draft(make_client)
    c = make_client(user_w=author(), app_w=app(tree), cfg=make_cfg(), sql=RowsSql())
    [d] = c.get("/api/candidates").json()
    assert (d["kind"], d["run_key"], d["dept"], d["name"]) == ("draft", key, "common", "new-skill")
    assert d["pr_number"] is None and d["git_sha"] is None
    assert d["uploaded_by"] == "ana@example.com" and d["has_rubric"] is True
    assert d["description"] == "new-skill helps analysts" and d["uploaded_at"]


def test_the_uploader_always_sees_their_own_draft(make_client):
    tree, key = stored_draft(make_client)
    # Later the uploader can no longer read common's live skills: their own draft still shows.
    c = make_client(user_w=author(), app_w=app(tree, readable=()), cfg=make_cfg(), sql=RowsSql())
    assert [d["run_key"] for d in c.get("/api/candidates").json()] == [key]
    assert c.get(f"/api/candidates/{key}/common/new-skill").status_code == 200


def test_another_author_sees_a_draft_only_in_a_department_they_can_read(make_client):
    tree, key = stored_draft(make_client)
    other = author("bo@example.com")
    blind = make_client(
        user_w=other, app_w=app(tree, readable=(), principal="bo@example.com"), cfg=make_cfg(), sql=RowsSql()
    )
    assert blind.get("/api/candidates").json() == []
    assert blind.get(f"/api/candidates/{key}/common/new-skill").status_code == 404
    seeing = make_client(
        user_w=author("bo@example.com"),
        app_w=app(tree, readable=("common",), principal="bo@example.com"),
        cfg=make_cfg(),
        sql=RowsSql(),
    )
    assert [d["run_key"] for d in seeing.get("/api/candidates").json()] == [key]


def test_a_reader_sees_no_drafts(make_client):
    tree, _ = stored_draft(make_client)
    c = make_client(user_w=user(("skillhub-users",)), app_w=app(tree), cfg=make_cfg(), sql=RowsSql())
    assert c.get("/api/candidates").json() == []


def test_draft_detail_has_the_draft_block_and_its_results(make_client):
    tree, key = stored_draft(make_client)
    sql = RowsSql({"common": [row("new-skill", "claude-sonnet", "trigger", "pass", key=f"{key}_h0a1b2c3d")]})
    c = make_client(user_w=author(), app_w=app(tree), cfg=make_cfg(), sql=sql)
    body = c.get(f"/api/candidates/{key}/common/new-skill").json()
    assert body["candidate"]["kind"] == "draft" and body["candidate"]["uploaded_by"] == "ana@example.com"
    assert body["candidate"]["pr_number"] is None and body["candidate"]["git_sha"] is None
    assert body["verdicts"] == {"claude-sonnet": "pass"} and len(body["rubric"]["criteria"]) == 5


def test_draft_detail_under_another_department_or_name_is_not_found(make_client):
    tree, key = stored_draft(make_client)
    c = make_client(user_w=reviewer(), app_w=app(tree), cfg=make_cfg(), sql=RowsSql())
    assert c.get(f"/api/candidates/{key}/equity_research/new-skill").status_code == 404
    assert c.get(f"/api/candidates/{key}/common/other-skill").status_code == 404


def run_body(key, modes=("trigger",), skills=("common/new-skill",)):
    return {
        "candidate": {"run_key": key, "skills": list(skills)},
        "models": ["claude-sonnet"],
        "modes": list(modes),
    }


def test_running_an_eval_on_a_draft_points_the_job_at_the_draft(make_client):
    tree, key = stored_draft(make_client)
    a = app(tree)
    c = make_client(user_w=author(), app_w=a, cfg=make_cfg(), sql=RowsSql())
    r = c.post("/api/eval-runs", json=run_body(key, modes=("trigger", "effectiveness")))
    assert r.status_code == 200, r.text
    p = a.jobs.started[0]["params"]
    assert p["source"] == f"draft:{key.removeprefix('draft_')}" and p["trigger_source"] == "manual"
    assert (
        p["skills_root"] == f"/Workspace{DRAFTS}/{key}/skills" and p["fixtures_root"] == ""
    )  # default fixtures
    assert (
        p["skills"] == "common/new-skill" and p["versions"] == "" and p["requested_by"] == "ana@example.com"
    )
    assert re.fullmatch(rf"{key}_h[0-9a-f]{{8}}", p["run_key"]) and len(p["run_key"]) <= 40


def test_a_draft_with_fixtures_runs_with_its_own_fixtures(make_client):
    tree, key = stored_draft(make_client, fixtures=True)
    a = app(tree)
    c = make_client(user_w=author(), app_w=a, cfg=make_cfg(), sql=RowsSql())
    assert c.post("/api/eval-runs", json=run_body(key)).status_code == 200
    assert a.jobs.started[0]["params"]["fixtures_root"] == f"/Workspace{DRAFTS}/{key}/fixtures"


def test_a_draft_without_a_rubric_runs_trigger_mode_only(make_client):
    tree, key = stored_draft(make_client, rubric=False)
    a = app(tree)
    c = make_client(user_w=author(), app_w=a, cfg=make_cfg(), sql=RowsSql())
    r = c.post("/api/eval-runs", json=run_body(key, modes=("trigger", "effectiveness")))
    assert r.status_code == 422 and r.json() == {"detail": {"reason": "rubric_required"}}
    assert c.post("/api/eval-runs", json=run_body(key)).status_code == 200
    assert len(a.jobs.started) == 1


def test_a_draft_whose_effectiveness_cases_are_assertion_cases_runs_effectiveness_without_a_rubric(
    make_client,
):
    tree = FakeTree()
    files = {**skill_files(rubric=False), "evals/evals.json": asserted_evals("new-skill")}
    r, _, _, _ = upload(make_client, archive_body(files), tree=tree)
    assert r.status_code == 201, r.text
    key = r.json()["run_key"]
    assert r.json()["has_rubric"] is False
    assert json.loads(tree.files[f"{DRAFTS}/{key}/draft.json"])["rubric_optional"] is True
    a = app(tree)
    c = make_client(user_w=author(), app_w=a, cfg=make_cfg(), sql=RowsSql())
    [listed] = c.get("/api/candidates").json()
    assert (listed["has_rubric"], listed["rubric_optional"]) == (False, True)
    detail = c.get(f"/api/candidates/{key}/common/new-skill").json()
    assert detail["rubric"] is None and detail["rubric_optional"] is True
    r = c.post("/api/eval-runs", json=run_body(key, modes=("trigger", "effectiveness")))
    assert r.status_code == 200, r.text
    assert a.jobs.started[0]["params"]["modes"] == "trigger,effectiveness"


def test_a_draft_with_an_effectiveness_case_still_needs_a_rubric(make_client):
    tree = FakeTree()
    files = {**skill_files(rubric=False), "evals/evals.json": asserted_evals("new-skill", effectiveness=True)}
    r, _, _, _ = upload(make_client, archive_body(files), tree=tree)
    key = r.json()["run_key"]
    assert json.loads(tree.files[f"{DRAFTS}/{key}/draft.json"])["rubric_optional"] is False
    c = make_client(user_w=author(), app_w=app(tree), cfg=make_cfg(), sql=RowsSql())
    assert c.get("/api/candidates").json()[0]["rubric_optional"] is False
    r = c.post("/api/eval-runs", json=run_body(key, modes=("trigger", "effectiveness")))
    assert r.status_code == 422 and r.json() == {"detail": {"reason": "rubric_required"}}


def test_a_draft_stored_before_rubric_optional_was_kept_reads_as_needing_a_rubric(make_client):
    tree, key = stored_draft(make_client, rubric=False)
    path = f"{DRAFTS}/{key}/draft.json"
    meta = json.loads(tree.files[path])
    del meta["rubric_optional"]
    tree.files[path] = json.dumps(meta).encode()
    c = make_client(user_w=author(), app_w=app(tree), cfg=make_cfg(), sql=RowsSql())
    assert c.get("/api/candidates").json()[0]["rubric_optional"] is False


def test_a_draft_run_names_only_the_drafts_own_skill(make_client):
    tree, key = stored_draft(make_client)
    c = make_client(user_w=author(), app_w=app(tree), cfg=make_cfg(), sql=RowsSql())
    r = c.post("/api/eval-runs", json=run_body(key, skills=("common/other-skill",)))
    assert r.status_code == 404 and r.json() == {"detail": {"reason": "candidate_not_found"}}


def test_another_author_who_cannot_read_the_department_cannot_run_the_draft(make_client):
    tree, key = stored_draft(make_client)
    a = app(tree, readable=(), principal="bo@example.com")
    c = make_client(user_w=author("bo@example.com"), app_w=a, cfg=make_cfg(), sql=RowsSql())
    r = c.post("/api/eval-runs", json=run_body(key))
    assert r.status_code == 404 and a.jobs.started == []


def _draft_paths(tree, key):
    return [f for f in tree.files if f"/drafts/{key}/" in f]


def test_the_uploader_discards_their_draft_and_its_files_go(make_client):
    tree, key = stored_draft(make_client)
    assert _draft_paths(tree, key)
    c = make_client(user_w=author(), app_w=app(tree), cfg=make_cfg(), sql=RowsSql())
    r = c.delete(f"/api/drafts/{key}")
    assert r.status_code == 200 and r.json() == {"discarded": key}
    assert _draft_paths(tree, key) == []
    assert c.get(f"/api/candidates/{key}/common/new-skill").status_code == 404


def test_a_reviewer_discards_someone_elses_draft(make_client):
    tree, key = stored_draft(make_client)
    c = make_client(
        user_w=reviewer(), app_w=app(tree, principal="rita@example.com"), cfg=make_cfg(), sql=RowsSql()
    )
    assert c.delete(f"/api/drafts/{key}").status_code == 200
    assert _draft_paths(tree, key) == []


def test_another_author_cannot_discard_a_draft(make_client):
    tree, key = stored_draft(make_client)
    other = author("bo@example.com")
    c = make_client(user_w=other, app_w=app(tree, principal="bo@example.com"), cfg=make_cfg(), sql=RowsSql())
    r = c.delete(f"/api/drafts/{key}")
    assert r.status_code == 403 and r.json() == {"detail": {"reason": "not_draft_owner"}}
    assert _draft_paths(tree, key)  # nothing removed


@pytest.mark.parametrize("bad", ["pr5_c7ecd3e_a1", "draft_0000000000000000000000ff", "not-a-key"])
def test_only_an_existing_draft_can_be_discarded(make_client, bad):
    tree, _ = stored_draft(make_client)
    c = make_client(
        user_w=reviewer(), app_w=app(tree, principal="rita@example.com"), cfg=make_cfg(), sql=RowsSql()
    )
    r = c.delete(f"/api/drafts/{bad}")
    assert r.status_code == 404 and r.json() == {"detail": {"reason": "candidate_not_found"}}
