"""Candidate skills: skill content under review in a pull request, shown in the Skill Hub before it is released."""

import hashlib
import json
import re

import pytest
from app_fakes import FakeApi, FakeJobs, FakeTree, FakeWorkspace
from backend import candidates, jobs
from backend.data import SqlDenied
from conftest import make_cfg

from skillhub.naming import schema_base

ME = "/api/2.0/preview/scim/v2/Me"
EFF = "/api/2.1/unity-catalog/effective-permissions/"
ROOT = "/Workspace/Shared/skill-hub/skill_hub/candidates"  # <workspace_root>/<catalog>/candidates
KEY = "pr5_c7ecd3e_a1"
SHA = "c7ecd3e" + "0" * 33
CHANGED = (("common", "new-skill"), ("equity_research", "earnings-note"), ("external", "partner-tool"))
RUN = {
    "run_key": "pr5-c7ecd3e-a1",  # CI passes the label; the folder is named by make_run_key(label)
    "skills": "common/new-skill,equity_research/earnings-note,external/partner-tool",
    "source": f"git:{SHA}",
    "trigger_source": "pr",
}


@pytest.fixture(autouse=True)
def _fresh_job_cache(monkeypatch):
    monkeypatch.delenv("SKILLHUB_JOB_ID_EVAL", raising=False)
    jobs.clear_cache()
    yield
    jobs.clear_cache()


def skill_md(name: str) -> bytes:
    return f"---\nname: {name}\ndescription: {name} helps analysts\n---\n# {name}\n".encode()


def evals_json(name: str) -> bytes:
    cases = [
        {"id": 1, "prompt": f"Use {name} on this", "should_trigger": True},
        {"id": 2, "prompt": "Book a flight", "should_trigger": False},
    ]
    return json.dumps({"skill_name": name, "evals": cases}).encode()


def rubric_json(name: str) -> bytes:
    criteria = [
        {"id": f"c{i}", "text": f"Criterion {i}", "type": "task_quality", "weight": 1, "source": "SKILL.md"}
        for i in range(1, 6)
    ]
    return json.dumps(
        {"skill_name": name, "source_hash": "sha256:x", "generated_by": "claude-sonnet", "criteria": criteria}
    ).encode()


def pr_folder(key=KEY, skills=(*CHANGED, ("common", "audit-xls")), uploaded=1_790_000_000, fixtures=True):
    """A CI upload: the whole skills/ tree of the checkout, the fixtures and the .uploaded_at marker."""
    files = {f"{ROOT}/{key}/.uploaded_at": f"{uploaded}\n".encode()}
    for dept, name in skills:
        base = f"{ROOT}/{key}/skills/{dept}/{name}"
        files[f"{base}/SKILL.md"] = skill_md(name)
        files[f"{base}/references/guide.md"] = b"# Guide\n"
        files[f"{base}/evals/evals.json"] = evals_json(name)
        files[f"{base}/evals/rubric.json"] = rubric_json(name)
    if fixtures:
        files[f"{ROOT}/{key}/fixtures/index.json"] = b"{}"
    return files


def user(groups=("skillhub-authors",), name="ana@example.com"):
    return FakeWorkspace(api=FakeApi({ME: {"userName": name, "groups": [{"display": g} for g in groups]}}))


def author():
    return user(("skillhub-authors", "dept-equity-research"))


def reviewer():
    return user(("skillhub-reviewers",), "rita@example.com")


def app(tree, readable=(), runs=(RUN,), principal="ana@example.com"):
    """The app's client. `readable` lists the departments whose live schema shows READ VOLUME, USE SCHEMA and
    USE CATALOG for the user in the effective-permissions answer; the others show USE SCHEMA only."""
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
    return FakeWorkspace(api=FakeApi(responses), jobs=FakeJobs(active=list(runs)), workspace=tree)


def row(skill, model, mode, verdict, key=KEY, at="2026-10-28T10:00:00", **extra):
    """One eval_runs child row. Each child run has its own run ID."""
    return {
        "run_key": key,
        "skill": skill,
        "skill_version": 0,
        "content_hash": "sha256:c1",
        "model_profile": model,
        "mode": mode,
        "verdict": verdict,
        "git_sha": SHA,
        "trigger_source": "pr",
        "run_id": hashlib.md5(f"{key}|{skill}|{model}|{mode}|{at}".encode()).hexdigest(),
        "experiment_id": "17",
        "n_cases": 4,
        "pass_rate": 0.75,
        "error_rate": 0.0,
        "accuracy": 1.0,
        "started_at": at,
        **extra,
    }


class RowsSql:
    """Stands in for UserSql: `rows` maps a department to its eval_runs rows. It returns every row of the
    department (narrowed by `skill`), so the route itself must keep only the candidate's run keys."""

    def __init__(self, rows=None, denied=(), journal=None):
        self.rows = rows or {}
        self.denied = set(denied)
        self.journal = journal or {}  # a department to its skill_versions rows
        self.calls: list[tuple[str, dict]] = []

    def query(self, statement, params=None):
        params = dict(params or {})
        self.calls.append((statement, params))
        for dept in self.denied:
            if f".{schema_base(dept)}_evals." in statement:
                raise SqlDenied("no SELECT")
        if "skill_versions" in statement:
            return list(self.journal.get(params.get("dept"), []))
        for dept, rows in self.rows.items():
            if f"skill_hub.{schema_base(dept)}_evals.eval_runs" in statement:
                return [r for r in rows if params.get("skill") in (None, r["skill"])]
        return []


def switched(skill, content_hash, version=1):
    """One skill_versions row that makes a version live, with its content hash."""
    return {"skill": skill, "action": "switched", "to_version": version, "content_hash": content_hash}


def names(body):
    return [(c["dept"], c["name"]) for c in body]


# --- run keys -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("folder", "pr", "sha", "attempt"),
    [
        ("pr5_c7ecd3e_a1", 5, "c7ecd3e", 1),
        ("pr123_0abcdef", 123, "0abcdef", None),
        ("pr9_1234567_a12", 9, "1234567", 12),
    ],
)
def test_a_pr_run_key_names_the_pull_request_and_commit(folder, pr, sha, attempt):
    key = candidates.parse_key(folder)
    assert (key.run_key, key.pr_number, key.sha7, key.attempt) == (folder, pr, sha, attempt)


@pytest.mark.parametrize(
    "folder",
    [
        "hub_1a2b3c4d",
        "merge_c7ecd3e_a1",
        "pr0_c7ecd3e_a1",
        "pr5_C7ECD3E_a1",
        "pr5_c7ecd3",
        "pr5_c7ecd3e_a1_h0a1b2c3d",
        "pr5_c7ecd3e_a1/../x",
        "pr5_c7ecd3e_a1\n",
        "",
        "draft_x",
    ],
)
def test_anything_else_is_not_a_candidate_key(folder):
    assert candidates.parse_key(folder) is None


def test_a_hub_run_of_a_candidate_gets_a_fresh_key_that_the_candidate_owns():
    first, second = candidates.hub_run_key(KEY), candidates.hub_run_key(KEY)
    assert re.fullmatch(rf"{KEY}_h[0-9a-f]{{8}}", first) and first != second and len(first) <= 40
    assert candidates.owns_run_key(KEY, KEY) and candidates.owns_run_key(KEY, first)
    # Another attempt, another pull request or a longer key that merely starts the same is not this candidate's.
    for other in ("pr5_c7ecd3e_a10", "pr5_c7ecd3e_a1_hzz", "pr55_c7ecd3e_a1", "pr5_c7ecd3e_a1_h0a1b2c3d4"):
        assert not candidates.owns_run_key(KEY, other)


# --- listing --------------------------------------------------------------------------------------------


def test_a_user_who_is_neither_author_nor_reviewer_sees_no_candidates_and_nothing_is_read(make_client):
    tree = FakeTree(pr_folder())
    a = app(tree, readable=("common", "equity_research", "external"))
    sql = RowsSql()
    c = make_client(user_w=user(("skillhub-users", "dept-equity-research")), app_w=a, cfg=make_cfg(), sql=sql)
    r = c.get("/api/candidates")
    assert r.status_code == 200 and r.json() == []
    # The app principal reads nothing for this user: no folder, no permissions, no Job runs, no SQL.
    assert tree.calls == [] and a.api_client.calls == [] and a.jobs.list_runs_calls == [] and sql.calls == []


def test_me_tells_the_ui_that_a_reader_is_not_an_author(make_client):
    body = make_client(user_w=user(("skillhub-users",)), cfg=make_cfg()).get("/api/me").json()
    assert body["is_author"] is False and body["is_reviewer"] is False


def test_a_reviewer_sees_the_changed_skills_of_every_department(make_client):
    tree = FakeTree(pr_folder())
    a = app(tree)  # no READ VOLUME anywhere: reviewers see every department regardless
    c = make_client(user_w=reviewer(), app_w=a, cfg=make_cfg(), sql=RowsSql())
    body = c.get("/api/candidates").json()
    # audit-xls is in the upload (CI uploads the whole tree) but the pull request did not change it.
    assert sorted(names(body)) == sorted(CHANGED)
    first = next(x for x in body if x["name"] == "new-skill")
    assert first["description"] == "new-skill helps analysts"
    assert (first["kind"], first["pr_number"], first["run_key"], first["git_sha"]) == ("pr", 5, KEY, SHA)
    assert first["uploaded_at"].startswith("2026-09-21")
    assert first["verdicts"] == {} and first["content_hash"] is None and first["eval_access"] == "ok"
    assert not [call for call in a.api_client.calls if "effective-permissions" in call[1]]


def test_an_author_sees_only_departments_they_can_read_live(make_client):
    tree = FakeTree(pr_folder())
    a = app(tree, readable=("common",))
    c = make_client(user_w=author(), app_w=a, cfg=make_cfg(), sql=RowsSql())
    assert names(c.get("/api/candidates").json()) == [("common", "new-skill")]
    # Nothing of a department the author cannot read is opened, not even its SKILL.md.
    opened = [p for m, p in tree.calls if m == "download"]
    assert not [p for p in opened if "/equity_research/" in p or "/external/" in p]


def test_an_author_whose_permission_check_fails_sees_none_of_that_department(make_client):
    tree = FakeTree(pr_folder())
    a = app(tree, readable=("common", "equity_research"))
    del a.api_client.responses[f"{EFF}schema/skill_hub.dept_equity_research"]  # the check errors: fail closed
    c = make_client(user_w=author(), app_w=a, cfg=make_cfg(), sql=RowsSql())
    assert names(c.get("/api/candidates").json()) == [("common", "new-skill")]


def test_the_listing_joins_the_latest_eval_rows_of_the_candidates_run_key(make_client):
    rows = {
        "common": [
            row("new-skill", "claude-sonnet", "trigger", "fail", at="2026-10-28T09:00:00"),
            # A Hub re-run of this candidate (its own fresh key) is newer, so it is the latest trigger result.
            row(
                "new-skill",
                "claude-sonnet",
                "trigger",
                "pass",
                key=f"{KEY}_h0a1b2c3d",
                at="2026-10-28T11:00:00",
            ),
            row("new-skill", "claude-sonnet", "with_skill", "warn"),
            row("new-skill", "claude-sonnet", "without_skill", "fail"),  # the baseline never counts
            row("new-skill", "gemini-flash", "trigger", "pass"),
            # Another pull request, and another attempt of this one, are not this candidate's results.
            row(
                "new-skill", "gemini-flash", "trigger", "fail", key="pr6_1234567_a1", at="2026-10-29T00:00:00"
            ),
            row(
                "new-skill",
                "gemini-flash",
                "trigger",
                "fail",
                key="pr5_c7ecd3e_a10",
                at="2026-10-29T00:00:00",
            ),
        ]
    }
    tree = FakeTree(pr_folder())
    sql = RowsSql(rows)
    c = make_client(user_w=reviewer(), app_w=app(tree), cfg=make_cfg(), sql=sql)
    new = next(x for x in c.get("/api/candidates").json() if x["name"] == "new-skill")
    assert new["verdicts"] == {"claude-sonnet": "warn", "gemini-flash": "pass"}
    assert new["content_hash"] == "sha256:c1"
    runs = [(s, p) for s, p in sql.calls if "skill_versions" not in s]  # the journal is read on its own
    assert runs and all(".eval_runs" in s and "requested_by" not in s for s, _ in runs)
    assert all(KEY in p.values() for _, p in runs)  # the key is a parameter, never part of the SQL text


def test_a_pull_request_skill_already_released_with_the_same_content_is_left_out(make_client):
    tree = FakeTree(pr_folder())
    sql = RowsSql(
        {"common": [row("new-skill", "claude-sonnet", "trigger", "pass")]},
        journal={"common": [switched("new-skill", "sha256:c1")]},
    )
    c = make_client(user_w=reviewer(), app_w=app(tree), cfg=make_cfg(), sql=sql)
    body = c.get("/api/candidates").json()
    assert ("common", "new-skill") not in names(body)
    assert sorted(names(body)) == sorted(CHANGED[1:])  # the other changed skills still list
    # The candidate page of a released skill still answers, from the same upload.
    assert c.get(f"/api/candidates/{KEY}/common/new-skill").status_code == 200


def test_a_pull_request_skill_whose_content_differs_from_the_live_version_stays_listed(make_client):
    sql = RowsSql(
        {"common": [row("new-skill", "claude-sonnet", "trigger", "pass")]},
        journal={"common": [switched("new-skill", "sha256:other")]},
    )
    c = make_client(user_w=reviewer(), app_w=app(FakeTree(pr_folder())), cfg=make_cfg(), sql=sql)
    assert ("common", "new-skill") in names(c.get("/api/candidates").json())


def test_a_candidate_with_no_eval_rows_or_a_retired_skill_is_not_left_out(make_client):
    # No eval rows: the candidate's content hash is not known, so it stays. A retired skill has no live content.
    journal = {"common": [switched("new-skill", "sha256:c1"), {"skill": "new-skill", "action": "retired"}]}
    c = make_client(
        user_w=reviewer(), app_w=app(FakeTree(pr_folder())), cfg=make_cfg(), sql=RowsSql(journal=journal)
    )
    assert ("common", "new-skill") in names(c.get("/api/candidates").json())
    sql = RowsSql({"common": [row("new-skill", "claude-sonnet", "trigger", "pass")]}, journal=journal)
    c = make_client(user_w=reviewer(), app_w=app(FakeTree(pr_folder())), cfg=make_cfg(), sql=sql)
    assert ("common", "new-skill") in names(c.get("/api/candidates").json())


def test_the_journal_is_read_once_per_visible_department(make_client):
    sql = RowsSql()
    make_client(user_w=reviewer(), app_w=app(FakeTree(pr_folder())), cfg=make_cfg(), sql=sql).get(
        "/api/candidates"
    )
    reads = [p["dept"] for s, p in sql.calls if "skill_versions" in s]
    assert sorted(reads) == ["common", "equity_research", "external"]


def test_an_author_reads_the_journal_of_only_the_departments_they_see(make_client):
    sql = RowsSql()
    c = make_client(
        user_w=author(), app_w=app(FakeTree(pr_folder()), readable=("common",)), cfg=make_cfg(), sql=sql
    )
    c.get("/api/candidates")
    assert [p["dept"] for s, p in sql.calls if "skill_versions" in s] == ["common"]


def test_skills_with_eval_rows_are_candidates_even_when_the_job_run_is_no_longer_listed(make_client):
    tree = FakeTree(pr_folder())
    sql = RowsSql({"common": [row("new-skill", "claude-sonnet", "trigger", "pass")]})
    c = make_client(user_w=reviewer(), app_w=app(tree, runs=()), cfg=make_cfg(), sql=sql)
    body = c.get("/api/candidates").json()
    assert names(body) == [("common", "new-skill")] and body[0]["verdicts"] == {"claude-sonnet": "pass"}


def test_eval_rows_the_user_cannot_read_mark_the_candidate_and_hide_nothing_else(make_client):
    tree = FakeTree(pr_folder())
    sql = RowsSql(denied=("common",))
    c = make_client(user_w=reviewer(), app_w=app(tree), cfg=make_cfg(), sql=sql)
    new = next(x for x in c.get("/api/candidates").json() if x["name"] == "new-skill")
    assert new["eval_access"] == "denied" and new["verdicts"] == {}


def test_only_the_newest_upload_of_a_pull_request_is_listed(make_client):
    old = pr_folder("pr5_aaaaaaa_a1", skills=CHANGED[:1], uploaded=1_790_000_000)
    new = pr_folder("pr5_bbbbbbb_a1", skills=CHANGED[:1], uploaded=1_790_009_000)
    other = pr_folder("pr7_ccccccc_a2", skills=CHANGED[:1], uploaded=1_789_000_000)
    runs = [
        {**RUN, "run_key": k, "skills": "common/new-skill"}
        for k in ("pr5-aaaaaaa-a1", "pr5-bbbbbbb-a1", "pr7-ccccccc-a2")
    ]
    c = make_client(
        user_w=reviewer(),
        app_w=app(FakeTree({**old, **new, **other}), runs=runs),
        cfg=make_cfg(),
        sql=RowsSql(),
    )
    body = c.get("/api/candidates").json()
    assert [(x["pr_number"], x["run_key"]) for x in body] == [(5, "pr5_bbbbbbb_a1"), (7, "pr7_ccccccc_a2")]
    assert body[0]["git_sha"] == "bbbbbbb"  # no row or run names the full commit: the key's short sha


def test_the_listing_opens_one_skill_md_per_candidate_and_no_other_skill_file(make_client):
    tree = FakeTree(pr_folder())
    c = make_client(user_w=reviewer(), app_w=app(tree), cfg=make_cfg(), sql=RowsSql())
    assert c.get("/api/candidates").status_code == 200
    opened = sorted(p for m, p in tree.calls if m == "download")
    base = ROOT.removeprefix("/Workspace") + f"/{KEY}"
    assert opened == sorted(
        [f"{base}/.uploaded_at", *(f"{base}/skills/{d}/{n}/SKILL.md" for d, n in CHANGED)]
    )
    # One listing of each root: the pull request uploads and the drafts. No skill folder is listed.
    assert sorted(p for m, p in tree.calls if m == "list") == sorted(
        [ROOT.removeprefix("/Workspace"), ROOT.removeprefix("/Workspace").replace("/candidates", "/drafts")]
    )


def test_folders_that_are_not_pull_request_runs_are_ignored(make_client):
    files = {
        **pr_folder(),
        **pr_folder("hub_1a2b3c4d"),
        **pr_folder("draft_0001"),
        f"{ROOT}/notes.txt": b"x",
    }
    runs = [RUN, {**RUN, "run_key": "hub_1a2b3c4d"}]
    c = make_client(user_w=reviewer(), app_w=app(FakeTree(files), runs=runs), cfg=make_cfg(), sql=RowsSql())
    assert {x["run_key"] for x in c.get("/api/candidates").json()} == {KEY}


def test_a_changed_skill_outside_the_configured_departments_or_with_a_bad_name_is_skipped(make_client):
    files = pr_folder(skills=(("common", "new-skill"), ("finance", "ledger"), ("common", "Bad_Name")))
    runs = [{**RUN, "skills": "common/new-skill,finance/ledger,common/Bad_Name"}]
    c = make_client(user_w=reviewer(), app_w=app(FakeTree(files), runs=runs), cfg=make_cfg(), sql=RowsSql())
    assert names(c.get("/api/candidates").json()) == [("common", "new-skill")]


def test_no_candidates_folder_lists_nothing(make_client):
    c = make_client(user_w=reviewer(), app_w=app(FakeTree({})), cfg=make_cfg(), sql=RowsSql())
    r = c.get("/api/candidates")
    assert r.status_code == 200 and r.json() == []


def test_me_reports_author(make_client):
    body = make_client(user_w=author(), cfg=make_cfg()).get("/api/me").json()
    assert body["is_author"] is True and body["is_reviewer"] is False


# --- detail ---------------------------------------------------------------------------------------------


def detail_url(key=KEY, dept="common", name="new-skill"):
    return f"/api/candidates/{key}/{dept}/{name}"


def test_candidate_detail_has_the_files_cases_rubric_results_and_candidate_block(make_client):
    tree = FakeTree(pr_folder())
    sql = RowsSql(
        {
            "common": [
                row("new-skill", "claude-sonnet", "trigger", "pass"),
                row("audit-xls", "gpt", "trigger", "fail"),
            ]
        }
    )
    c = make_client(user_w=author(), app_w=app(tree, readable=("common",)), cfg=make_cfg(), sql=sql)
    r = c.get(detail_url())
    assert r.status_code == 200
    body = r.json()
    assert (body["dept"], body["name"], body["description"]) == (
        "common",
        "new-skill",
        "new-skill helps analysts",
    )
    assert sorted(body["files"]) == ["SKILL.md", "references/guide.md"]  # evals/ never shows as a file
    assert [case["id"] for case in body["evals"]["evals"]] == [1, 2]
    assert len(body["rubric"]["criteria"]) == 5
    assert body["candidate"] == {
        "kind": "pr",
        "pr_number": 5,
        "run_key": KEY,
        "git_sha": SHA,
        "uploaded_at": body["candidate"]["uploaded_at"],
        "uploaded_by": None,
        "submitted_pr": None,
        "submitted_url": None,
    } and body["candidate"]["uploaded_at"].startswith("2026-09-21")
    assert body["verdicts"] == {"claude-sonnet": "pass"}
    assert [(x["skill"], x["run_key"], x["verdict"]) for x in body["results"]] == [("new-skill", KEY, "pass")]
    assert body["content_hash"].startswith("sha256:") and body["eval_access"] == "ok"
    assert all(p.get("skill") == "new-skill" for _, p in sql.calls)


def test_candidate_detail_is_refused_to_a_user_who_is_neither_author_nor_reviewer(make_client):
    tree = FakeTree(pr_folder())
    c = make_client(
        user_w=user(("skillhub-users",)), app_w=app(tree, readable=("common",)), cfg=make_cfg(), sql=RowsSql()
    )
    r = c.get(detail_url())
    assert r.status_code == 403 and r.json() == {"detail": {"reason": "not_author"}}
    assert tree.calls == []


def test_candidate_detail_of_a_department_the_author_cannot_read_is_not_found(make_client):
    tree = FakeTree(pr_folder())
    c = make_client(user_w=author(), app_w=app(tree, readable=("common",)), cfg=make_cfg(), sql=RowsSql())
    r = c.get(detail_url(dept="equity_research", name="earnings-note"))
    assert r.status_code == 404 and r.json() == {"detail": {"reason": "candidate_not_found"}}
    assert tree.calls == []


def test_a_reviewer_reads_a_candidate_of_any_department(make_client):
    tree = FakeTree(pr_folder())
    c = make_client(user_w=reviewer(), app_w=app(tree), cfg=make_cfg(), sql=RowsSql())
    assert c.get(detail_url(dept="external", name="partner-tool")).status_code == 200


@pytest.mark.parametrize(
    "url",
    [
        detail_url(key="pr5_deadbee_a1"),  # no such folder
        detail_url(name="no-such-skill"),
        detail_url(key="hub_1a2b3c4d"),  # not a candidate key
        detail_url(dept="finance"),  # not a configured department
        detail_url(name="Bad_Name"),
    ],
)
def test_candidate_detail_of_anything_else_is_not_found(make_client, url):
    c = make_client(user_w=reviewer(), app_w=app(FakeTree(pr_folder())), cfg=make_cfg(), sql=RowsSql())
    r = c.get(url)
    assert r.status_code == 404 and r.json() == {"detail": {"reason": "candidate_not_found"}}


# --- run eval -------------------------------------------------------------------------------------------


def run_body(skills=("common/new-skill",), key=KEY, **extra):
    return {
        "candidate": {"run_key": key, "skills": list(skills)},
        "models": ["claude-sonnet"],
        "modes": ["trigger"],
        **extra,
    }


def test_running_an_eval_on_a_candidate_points_the_job_at_its_folder_with_a_fresh_key(make_client):
    a = app(FakeTree(pr_folder()), readable=("common",))
    c = make_client(user_w=author(), app_w=a, cfg=make_cfg(), sql=RowsSql())
    first = c.post("/api/eval-runs", json=run_body())
    second = c.post("/api/eval-runs", json=run_body())
    assert first.status_code == 200 and second.status_code == 200
    p = a.jobs.started[0]["params"]
    assert p == {
        "run_key": first.json()["run_key"],
        "skills": "common/new-skill",
        "models": "claude-sonnet",
        "modes": "trigger",
        "versions": "",
        "source": "git:c7ecd3e",
        "trigger_source": "manual",
        "requested_by": "ana@example.com",
        "skills_root": f"{ROOT}/{KEY}/skills",
        "fixtures_root": f"{ROOT}/{KEY}/fixtures",
    }
    # Never the PR gate's key, and never the same twice.
    keys = [s["params"]["run_key"] for s in a.jobs.started]
    assert keys[0] != keys[1] and KEY not in keys and all(candidates.owns_run_key(KEY, k) for k in keys)


def test_a_candidate_upload_without_fixtures_runs_with_the_default_fixtures(make_client):
    a = app(FakeTree(pr_folder(fixtures=False)))
    c = make_client(user_w=reviewer(), app_w=a, cfg=make_cfg(), sql=RowsSql())
    assert c.post("/api/eval-runs", json=run_body()).status_code == 200
    assert a.jobs.started[0]["params"]["fixtures_root"] == ""


def test_running_an_eval_on_a_candidate_is_refused_to_a_user_who_is_neither_author_nor_reviewer(make_client):
    a = app(FakeTree(pr_folder()), readable=("common",))
    c = make_client(user_w=user(("skillhub-users",)), app_w=a, cfg=make_cfg(), sql=RowsSql())
    r = c.post("/api/eval-runs", json=run_body())
    assert r.status_code == 403 and r.json() == {"detail": {"reason": "not_author"}}
    assert a.jobs.started == [] and a.workspace.calls == []


def test_running_an_eval_on_a_candidate_of_a_department_the_author_cannot_read_is_not_found(make_client):
    a = app(FakeTree(pr_folder()), readable=("common",))
    c = make_client(user_w=author(), app_w=a, cfg=make_cfg(), sql=RowsSql())
    r = c.post("/api/eval-runs", json=run_body(skills=("common/new-skill", "equity_research/earnings-note")))
    assert r.status_code == 404 and r.json() == {"detail": {"reason": "candidate_not_found"}}
    assert a.jobs.started == []


@pytest.mark.parametrize(
    "body",
    [
        run_body(key="pr5_deadbee_a1"),
        run_body(skills=("common/no-such-skill",)),
        run_body(skills=("new-skill",)),  # dept/name only
        run_body(key="hub_1a2b3c4d"),
    ],
)
def test_running_an_eval_on_a_missing_candidate_is_not_found(make_client, body):
    a = app(FakeTree(pr_folder()))
    c = make_client(user_w=reviewer(), app_w=a, cfg=make_cfg(), sql=RowsSql())
    r = c.post("/api/eval-runs", json=body)
    assert r.status_code == 404 and r.json() == {"detail": {"reason": "candidate_not_found"}}
    assert a.jobs.started == []


@pytest.mark.parametrize(
    "extra",
    [{"skills": ["skill_hub.common.audit-xls"]}, {"versions": {"skill_hub.common.new-skill": 1}}],
)
def test_a_candidate_run_takes_no_live_skills_and_no_pinned_versions(make_client, extra):
    a = app(FakeTree(pr_folder()))
    c = make_client(user_w=reviewer(), app_w=a, cfg=make_cfg(), sql=RowsSql())
    r = c.post("/api/eval-runs", json={**run_body(), **extra})
    assert r.status_code == 422 and r.json() == {"detail": {"reason": "mixed_sources"}}
    assert a.jobs.started == []


def test_a_run_with_neither_skills_nor_a_candidate_is_422(make_client):
    c = make_client(user_w=reviewer(), app_w=app(FakeTree({})), cfg=make_cfg(), sql=RowsSql())
    assert (
        c.post("/api/eval-runs", json={"models": ["claude-sonnet"], "modes": ["trigger"]}).status_code == 422
    )


def test_a_skill_name_longer_than_unity_catalog_allows_is_not_a_candidate_name():
    from backend import candidates

    assert candidates.valid_skill_name("a" * 64)
    assert not candidates.valid_skill_name("a" * 65)
