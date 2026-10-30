"""Pull requests from the Skill Hub: submit a draft, the release status of its pull request, and a reviewer's merge.
GitHub is FakeGitHub, an in-memory repository; the draft and the Hub's files are the drafts fakes."""

import json
import logging

import pytest
from app_fakes import FakeApi, FakeGitHub, FakeTree, FakeWorkspace
from backend import github, release
from conftest import make_cfg
from test_app_candidates import RowsSql, pr_folder, skill_md
from test_app_candidates import app as candidates_app
from test_app_drafts import ME, app, author, reviewer, skill_files, stored_draft, user
from test_app_github import TOKEN, app_with_secret, http_error, recording_opener

HUB = release.HUB_MARKER
FQN = "skill_hub.common.new-skill"  # the skill's name in Unity Catalog, as its live page is named
HEAD = "H5"
MAIN = {}  # a new skill: the repository has no folder for it
# What pull request 5 changes, unless a test says otherwise.
SKILL_PATHS = ["skills/common/new-skill/SKILL.md"]


@pytest.fixture(autouse=True)
def _no_github_by_accident(monkeypatch):
    monkeypatch.delenv(github.REPO_ENV, raising=False)
    monkeypatch.delenv(github.SECRET_ENV, raising=False)
    github._cache.clear()
    yield
    github._cache.clear()


@pytest.fixture(autouse=True)
def _no_waits(monkeypatch):
    """A merge reads an unknown mergeable state again after a pause; the tests count the reads, not the seconds."""
    monkeypatch.setattr(release, "MERGEABLE_WAIT_S", 0.0)


@pytest.fixture
def gh(monkeypatch):
    """The repository the Hub talks to: a FakeGitHub, which the connection returns whatever the configuration says."""
    fake = FakeGitHub(MAIN)
    monkeypatch.setattr(github, "connect", lambda app_w, cached=True: fake)
    return fake


def meta_of(tree, key):
    path = next(p for p in tree.files if p.endswith(f"/{key}/draft.json"))
    return json.loads(tree.files[path])


def draft_files(tree, key):
    return sorted(p for p in tree.files if f"/drafts/{key}/" in p)


def submit(make_client, tree, key, user=None, sql=None):
    c = make_client(user_w=user or author(), app_w=app(tree), cfg=make_cfg(), sql=sql or RowsSql())
    return c.post(f"/api/drafts/{key}/submit")


def skill_tree(fake, commit):
    return fake.snapshots[fake.commits[commit][1]]


# --- submit --------------------------------------------------------------------------------------------------------


def test_submit_opens_a_pull_request_that_adds_the_skill_as_the_draft_has_it(make_client, gh):
    tree, key = stored_draft(make_client)
    r = submit(make_client, tree, key)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pr_number"] == 5 and body["notes"] == []
    assert body["branch"] == f"skillhub/new-skill-{key[-8:]}"
    assert body["url"] == "https://github.test/acme/skills/pull/5"

    assert gh.titles[5] == "Add new-skill (common) from the Skill Hub"
    assert f"`{key}`" in gh.bodies[5] and gh.bodies[5].endswith(HUB)
    assert "CI generates" not in gh.bodies[5]  # the draft has a rubric

    message, tree_sha, parent = gh.commits[gh.refs[body["branch"]]]
    assert message == "feat(skills): new-skill from the Skill Hub\n\nSubmitted-by: ana@example.com"
    assert parent == "C0"  # from main's head
    files = skill_tree(gh, gh.refs[body["branch"]])
    assert sorted(p for p in files if p.startswith("skills/")) == sorted(
        f"skills/common/new-skill/{p}" for p in skill_files()
    )
    assert files["skills/common/new-skill/SKILL.md"] == skill_md("new-skill")
    assert tree_sha


def test_the_draft_records_its_pull_request(make_client, gh):
    tree, key = stored_draft(make_client)
    submit(make_client, tree, key)
    meta = meta_of(tree, key)
    assert meta["submitted_pr"] == 5
    assert meta["submitted_url"] == "https://github.test/acme/skills/pull/5"
    assert meta["submitted_by"] == "ana@example.com" and isinstance(meta["submitted_at"], int)
    assert meta["dept"] == "common" and meta["skill"] == "new-skill"  # the rest of draft.json is kept


def test_an_update_replaces_the_folder_and_deletes_the_files_the_draft_lacks(make_client, gh):
    gh.snapshots["S0"] = {
        "skills/common/new-skill/SKILL.md": b"old",
        "skills/common/new-skill/references/old.md": b"old guide",
        "skills/common/other/SKILL.md": b"other skill",
    }
    tree, key = stored_draft(make_client)
    r = submit(make_client, tree, key)
    assert r.status_code == 200, r.text
    assert gh.titles[5] == "Update new-skill (common) from the Skill Hub"
    files = skill_tree(gh, gh.refs[r.json()["branch"]])
    assert "skills/common/new-skill/references/old.md" not in files
    assert files["skills/common/new-skill/SKILL.md"] == skill_md("new-skill")
    assert files["skills/common/other/SKILL.md"] == b"other skill"  # another skill is untouched


def test_a_draft_that_main_already_has_is_409_and_nothing_is_written(make_client, gh):
    tree, key = stored_draft(make_client)
    gh.snapshots["S0"] = {f"skills/common/new-skill/{p}": d for p, d in skill_files().items()}
    r = submit(make_client, tree, key)
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "no_changes"
    assert not {"create_blob", "create_tree", "create_commit", "create_ref", "create_pull"} & set(gh.calls)
    assert "submitted_pr" not in meta_of(tree, key)


def test_a_top_level_fixtures_folder_stays_in_the_hub_and_the_response_says_so(make_client, gh):
    tree, key = stored_draft(make_client, fixtures=True)
    r = submit(make_client, tree, key)
    assert r.status_code == 200, r.text
    assert r.json()["notes"] == ["The draft's top-level fixtures/ folder was not submitted."]
    assert not [p for p in skill_tree(gh, gh.refs[r.json()["branch"]]) if "fixtures" in p]


def test_a_rubric_the_draft_lacks_is_left_to_ci_and_the_body_says_so(make_client, gh):
    tree, key = stored_draft(make_client, rubric=False)
    assert submit(make_client, tree, key).status_code == 200
    assert "CI generates the missing rubric" in gh.bodies[5]


def test_resubmitting_is_409_with_the_pull_request(make_client, gh):
    tree, key = stored_draft(make_client)
    assert submit(make_client, tree, key).status_code == 200
    r = submit(make_client, tree, key)
    assert r.status_code == 409 and r.json() == {"detail": {"reason": "already_submitted", "pr_number": 5}}
    assert gh.calls.count("create_pull") == 1


def test_another_author_cannot_submit_a_draft(make_client, gh):
    tree, key = stored_draft(make_client)
    r = submit(make_client, tree, key, user=author("bo@example.com"))
    assert r.status_code == 403 and r.json() == {"detail": {"reason": "not_draft_owner"}}
    assert gh.calls == []


def test_a_reviewer_submits_anyones_draft(make_client, gh):
    tree, key = stored_draft(make_client)
    assert submit(make_client, tree, key, user=reviewer()).status_code == 200


def test_a_user_who_is_neither_author_nor_reviewer_cannot_submit(make_client, gh):
    tree, key = stored_draft(make_client)
    r = submit(make_client, tree, key, user=user(("skillhub-users",)))
    assert r.status_code == 403 and r.json() == {"detail": {"reason": "not_author"}}
    assert gh.calls == []


def test_a_draft_that_is_gone_is_not_found(make_client, gh):
    tree, key = stored_draft(make_client)
    tree.files = {p: b for p, b in tree.files.items() if f"/drafts/{key}/" not in p}
    r = submit(make_client, tree, key)
    assert r.status_code == 404 and r.json() == {"detail": {"reason": "candidate_not_found"}}


def test_a_draft_without_evals_is_422(make_client, gh):
    tree, key = stored_draft(make_client)
    for p in [p for p in tree.files if p.endswith("/evals/evals.json")]:
        del tree.files[p]
    r = submit(make_client, tree, key)
    assert r.status_code == 422 and r.json() == {"detail": {"reason": "evals_missing"}}
    assert gh.calls == []


def test_without_github_configured_a_submit_is_503_and_changes_nothing(make_client, monkeypatch):
    tree, key = stored_draft(make_client)
    before = dict(tree.files)
    r = submit(make_client, tree, key)
    assert r.status_code == 503 and r.json() == {"detail": {"reason": "github_not_configured"}}
    assert tree.files == before


def test_a_github_failure_is_502_with_the_error_type_and_the_draft_is_unchanged(make_client, gh):
    gh.fail["create_blob"] = github.GitHubUnavailable(503)
    tree, key = stored_draft(make_client)
    r = submit(make_client, tree, key)
    assert r.status_code == 502
    assert r.json() == {"detail": {"reason": "github_failed", "error": "GitHubUnavailable"}}
    assert "submitted_pr" not in meta_of(tree, key)


def test_a_branch_that_already_has_a_pull_request_is_409_and_recorded(make_client, gh):
    tree, key = stored_draft(make_client)
    branch = f"skillhub/new-skill-{key[-8:]}"
    gh.refs[branch] = "C0"
    gh.create_pull("Add new-skill (common) from the Skill Hub", branch, "main", "earlier")
    r = submit(make_client, tree, key)
    assert r.status_code == 409 and r.json() == {"detail": {"reason": "already_submitted", "pr_number": 5}}
    assert meta_of(tree, key)["submitted_pr"] == 5


def test_a_branch_left_without_a_pull_request_is_started_again(make_client, gh):
    tree, key = stored_draft(make_client)
    gh.refs[f"skillhub/new-skill-{key[-8:]}"] = "C0"
    r = submit(make_client, tree, key)
    assert r.status_code == 200, r.text
    assert gh.refs[r.json()["branch"]] != "C0"


# --- the catalog lists a submitted draft as its pull request ----------------------------------------------------


def test_a_submitted_draft_is_left_out_of_the_catalog_once_its_pull_request_is_listed(make_client, gh):
    tree, key = stored_draft(make_client)
    assert submit(make_client, tree, key).status_code == 200  # the draft records pull request 5
    # Pull request 5 is listed as its candidate skills: the draft is listed as that pull request, so it is left out.
    both = FakeTree({**tree.files, **pr_folder()})
    c = make_client(user_w=reviewer(), app_w=candidates_app(both), cfg=make_cfg(), sql=RowsSql())
    listed = c.get("/api/candidates").json()
    assert [x["kind"] for x in listed] == ["pr", "pr", "pr"]
    assert all(x["pr_number"] == 5 for x in listed)


def test_a_submitted_draft_shows_its_pull_request_while_that_is_not_listed(make_client, gh):
    tree, key = stored_draft(make_client)
    assert submit(make_client, tree, key).status_code == 200
    c = make_client(user_w=author(), app_w=app(tree), cfg=make_cfg(), sql=RowsSql())
    listed = c.get("/api/candidates").json()
    assert [(x["kind"], x["submitted_pr"]) for x in listed] == [("draft", 5)]


# --- release status ----------------------------------------------------------------------------------------------


def put_pull(gh, number=5, *, paths=SKILL_PATHS, **fields):
    """A pull request in the fake repository that changes `paths`."""
    base = {
        "number": number,
        "url": f"https://github.test/acme/skills/pull/{number}",
        "state": "open",
        "merged": False,
        "merged_at": None,
        "merge_sha": None,
        "head_sha": HEAD,
        "head_branch": "skillhub/new-skill-00000000",
        "body": f"Submitted from the draft.\n\n{HUB}",
        "mergeable_state": "clean",
    }
    gh.pulls[number] = github.Pull(**{**base, **fields})
    gh.paths[number] = list(paths)


def status(make_client, gh, *, sql=None, tree=None, pr=5, dept="common", skill="new-skill", user=None):
    tree = tree if tree is not None else FakeTree()
    c = make_client(user_w=user or author(), app_w=app(tree), cfg=make_cfg(), sql=sql or RowsSql())
    return c.get(f"/api/release-status?pr={pr}&dept={dept}&skill={skill}")


def test_release_status_before_the_gate_has_run(make_client, gh):
    put_pull(gh)
    r = status(make_client, gh)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["opened"] == {"state": "open", "url": "https://github.test/acme/skills/pull/5"}
    assert body["gate"]["state"] == "none"
    assert body["merged"] == {"merged": False, "sha": None, "at": None}
    assert body["release"] == {"state": "not_started", "url": None}
    assert body["live"] == {"fqn": FQN, "version": None, "switched_after_merge": False, "eval_access": "ok"}


@pytest.mark.parametrize(
    ("check", "gate"),
    [
        ({"skills-pr": "pending"}, "pending"),
        ({"skills-pr": "passed"}, "passed"),
        ({"skills-pr": "failed"}, "failed"),
        ({"skills-pr": "cancelled"}, "cancelled"),
    ],
)
def test_release_status_reports_the_gate_of_the_head_commit(make_client, gh, check, gate):
    put_pull(gh)
    gh.checks[HEAD] = check
    assert status(make_client, gh).json()["gate"]["state"] == gate


def test_release_status_while_the_release_runs_and_waits_for_approval(make_client, gh):
    put_pull(gh, state="closed", merged=True, merged_at="2026-10-29T12:00:00Z", merge_sha="M5")
    gh.runs["M5"] = github.Run(url="https://run/1", status="in_progress", conclusion=None, waiting=False)
    assert status(make_client, gh).json()["release"] == {"state": "running", "url": "https://run/1"}
    gh.runs["M5"] = github.Run(url="https://run/1", status="in_progress", conclusion=None, waiting=True)
    body = status(make_client, gh).json()
    assert body["release"] == {"state": "waiting_approval", "url": "https://run/1"}
    assert body["merged"]["sha"] == "M5"


@pytest.mark.parametrize(
    ("conclusion", "state"), [("success", "succeeded"), ("failure", "failed"), ("cancelled", "cancelled")]
)
def test_release_status_when_the_release_has_finished(make_client, gh, conclusion, state):
    put_pull(gh, state="closed", merged=True, merged_at="2026-10-29T12:00:00Z", merge_sha="M5")
    gh.runs["M5"] = github.Run(url="https://run/1", status="completed", conclusion=conclusion, waiting=False)
    assert status(make_client, gh).json()["release"]["state"] == state


def test_release_status_shows_the_live_version_and_a_switch_after_the_merge(make_client, gh):
    put_pull(gh, state="closed", merged=True, merged_at="2026-10-29T12:00:00Z", merge_sha="M5")
    gh.runs["M5"] = github.Run(url="https://run/1", status="completed", conclusion="success", waiting=False)
    journal = [
        {
            "skill": "new-skill",
            "action": "published",
            "version": 2,
            "content_hash": "h2",
            "ts": "2026-10-29T12:30:00Z",
        },
        {
            "skill": "new-skill",
            "action": "switched",
            "to_version": 2,
            "content_hash": "h2",
            "ts": "2026-10-29T12:40:00Z",
        },
    ]
    body = status(make_client, gh, sql=RowsSql(journal={"common": journal})).json()
    assert body["live"] == {"fqn": FQN, "version": 2, "switched_after_merge": True, "eval_access": "ok"}


def test_a_switch_before_the_merge_is_not_a_switch_after_it(make_client, gh):
    put_pull(gh, state="closed", merged=True, merged_at="2026-10-29T12:00:00Z", merge_sha="M5")
    journal = [
        {
            "skill": "new-skill",
            "action": "switched",
            "to_version": 1,
            "content_hash": "h1",
            "ts": "2026-10-28T09:00:00Z",
        }
    ]
    body = status(make_client, gh, sql=RowsSql(journal={"common": journal})).json()
    assert body["live"] == {"fqn": FQN, "version": 1, "switched_after_merge": False, "eval_access": "ok"}


def test_the_journal_denied_hides_the_live_version_only(make_client, gh):
    put_pull(gh)
    body = status(make_client, gh, sql=RowsSql(denied=("common",))).json()
    assert body["live"] == {
        "fqn": FQN,
        "version": None,
        "switched_after_merge": False,
        "eval_access": "denied",
    }
    assert body["gate"]["state"] == "none"


def test_an_unknown_pull_request_is_404(make_client, gh):
    r = status(make_client, gh, pr=99)
    assert r.status_code == 404 and r.json() == {"detail": {"reason": "pull_not_found"}}


def test_a_department_the_author_cannot_read_is_not_found(make_client, gh):
    put_pull(gh)
    r = status(make_client, gh, dept="external")
    assert r.status_code == 404 and r.json() == {"detail": {"reason": "candidate_not_found"}}


def test_a_reader_gets_403_not_author(make_client, gh):
    put_pull(gh)
    r = status(make_client, gh, user=user(("skillhub-users",)))
    assert r.status_code == 403 and r.json() == {"detail": {"reason": "not_author"}}


def test_a_reviewer_reads_any_department(make_client, gh):
    put_pull(gh, paths=["skills/external/new-skill/SKILL.md"])
    assert status(make_client, gh, dept="external", user=reviewer()).status_code == 200


@pytest.mark.parametrize(
    "paths",
    [
        ["skills/equity_research/secret-skill/SKILL.md"],  # another department's skill
        ["skills/common/other-skill/SKILL.md"],  # another skill of the department
        ["README.md", ".github/workflows/platform.yml"],  # no skill at all
        ["skills/common/new-skill"],  # not a file under the folder
        [],
    ],
)
def test_a_pull_request_that_does_not_change_the_skill_is_not_found_and_nothing_of_it_leaks(
    make_client, gh, paths
):
    """The app reads any pull request of the repository with its own token, so the department the caller may see must
    be the one the pull request changes: otherwise the answer is that of a pull request that is not there."""
    put_pull(gh, paths=paths, state="closed", merged=True, merged_at="2026-10-29T12:00:00Z", merge_sha="M5")
    gh.checks[HEAD] = {"skills-pr": "passed"}
    gh.runs["M5"] = github.Run(url="https://run/1", status="completed", conclusion="success", waiting=False)
    r = status(make_client, gh)
    assert r.status_code == 404 and r.json() == {"detail": {"reason": "pull_not_found"}}
    assert r.json() == status(make_client, gh, pr=99).json()  # the same answer as a missing pull request
    assert "github.test" not in r.text and "M5" not in r.text
    assert "check_states" not in gh.calls and "publish_run" not in gh.calls


def test_a_pull_request_too_large_to_read_has_no_release_status(make_client, gh):
    put_pull(gh)
    gh.fail["pull_paths"] = github.GitHubRejected(422)
    r = status(make_client, gh)
    assert r.status_code == 404 and r.json() == {"detail": {"reason": "pull_not_found"}}


def test_a_pull_request_that_changes_the_skill_among_others_has_its_status(make_client, gh):
    put_pull(gh, paths=["skills/common/other-skill/SKILL.md", "skills/common/new-skill/evals/evals.json"])
    assert status(make_client, gh).status_code == 200


def test_release_status_and_merge_need_the_skill_pieces_to_be_valid(make_client, gh):
    put_pull(gh)
    assert status(make_client, gh, dept="finance").status_code == 404
    assert status(make_client, gh, skill="Bad_Name").status_code == 404


# --- merging from the Hub ---------------------------------------------------------------------------------------


def green(gh, **fields):
    """An open pull request from the Hub, with its gate and platform check passed and only skills/ changed."""
    put_pull(gh, **fields)
    gh.checks[HEAD] = {"skills-pr": "passed", "platform": "passed"}
    gh.paths[5] = ["skills/common/new-skill/SKILL.md"]


def merge(make_client, gh, *, user=None, number=5):
    c = make_client(user_w=user or reviewer(), app_w=app(FakeTree()), cfg=make_cfg(), sql=RowsSql())
    return c.post(f"/api/pulls/{number}/merge")


def test_a_reviewer_merges_a_green_pull_request_with_a_merge_commit(make_client, gh):
    green(gh)
    r = merge(make_client, gh)
    assert r.status_code == 200, r.text
    assert r.json() == {"pr_number": 5, "merged": True, "sha": "M5"}
    assert gh.merges == [(5, HEAD)]  # the head is named, so a later push is not merged unseen


def test_an_author_cannot_merge(make_client, gh):
    green(gh)
    r = merge(make_client, gh, user=author())
    assert r.status_code == 403 and r.json() == {"detail": {"reason": "not_reviewer"}}
    assert gh.calls == []


@pytest.mark.parametrize(
    ("fields", "checks", "paths", "unmet"),
    [
        ({"state": "closed"}, None, None, ["open"]),
        ({}, {"skills-pr": "pending", "platform": "passed"}, None, ["check:skills-pr"]),
        ({}, {"skills-pr": "failed", "platform": "passed"}, None, ["check:skills-pr"]),
        ({}, {"skills-pr": "passed"}, None, ["check:platform"]),
        ({}, {"skills-pr": "cancelled", "platform": "passed"}, None, ["check:skills-pr"]),
        ({"mergeable_state": "blocked"}, None, None, ["clean"]),
        ({"mergeable_state": "behind"}, None, None, ["clean"]),
        ({"mergeable_state": "unknown"}, None, None, ["mergeable_unknown"]),
        ({"mergeable_state": None}, None, None, ["mergeable_unknown"]),
        ({}, None, ["skills/common/new-skill/SKILL.md", "README.md"], ["only_skills"]),
        ({}, None, [], ["only_skills"]),
        ({}, None, ["skills/common/new-skill/SKILL.md", "skills/finance/x/SKILL.md"], ["only_skills"]),
    ],
)
def test_a_merge_is_refused_unless_every_rule_holds(make_client, gh, fields, checks, paths, unmet):
    green(gh, **fields)
    if checks is not None:
        gh.checks[HEAD] = checks
    if paths is not None:
        gh.paths[5] = paths
    r = merge(make_client, gh)
    assert r.status_code == 409, r.text
    assert r.json() == {"detail": {"reason": "not_mergeable", "unmet": unmet}}
    assert gh.merges == []


def test_a_merge_reads_the_pull_request_again_while_github_works_out_its_mergeable_state(make_client, gh):
    green(gh)
    gh.mergeable_reads[5] = ["unknown", None, "clean"]  # GitHub knows by the third read
    r = merge(make_client, gh)
    assert r.status_code == 200, r.text
    assert gh.calls.count("pull") == 3
    assert gh.merges == [(5, HEAD)]  # still the head commit, as read


def test_a_mergeable_state_still_unknown_after_the_reads_asks_the_reviewer_to_try_again(make_client, gh):
    green(gh, mergeable_state=None)
    r = merge(make_client, gh)
    assert r.status_code == 409
    assert r.json() == {"detail": {"reason": "not_mergeable", "unmet": ["mergeable_unknown"]}}
    assert gh.calls.count("pull") == 1 + release.MERGEABLE_READS  # bounded: then the answer comes
    assert gh.merges == []


def test_a_settled_mergeable_state_is_not_read_again(make_client, gh):
    green(gh, mergeable_state="blocked")
    assert merge(make_client, gh).json()["detail"]["unmet"] == ["clean"]
    assert gh.calls.count("pull") == 1


def test_settled_pull_pauses_between_reads_and_leaves_a_closed_pull_request_alone(gh):
    put_pull(gh)
    gh.mergeable_reads[5] = ["unknown", "unknown", "unknown", "unknown", "clean"]
    pauses: list[float] = []
    pull = release.settled_pull(gh, gh.pull(5), sleep=pauses.append)
    assert pull.mergeable_state == "unknown"  # three more reads, then the answer, settled or not
    assert len(pauses) == release.MERGEABLE_READS == 3

    put_pull(gh, state="closed", mergeable_state=None)
    gh.calls.clear()
    assert release.settled_pull(gh, gh.pulls[5], sleep=pauses.append) is gh.pulls[5]
    assert gh.calls == []


def test_a_pull_request_not_from_the_hub_may_merge_when_it_names_only_skills(make_client, gh):
    green(gh, body="A hand-written pull request.")
    assert merge(make_client, gh).status_code == 200


def test_a_pull_request_too_large_to_read_is_refused(make_client, gh):
    green(gh)
    gh.fail["pull_paths"] = github.GitHubRejected(422)
    r = merge(make_client, gh)
    assert r.status_code == 409 and r.json()["detail"]["unmet"] == ["only_skills"]


def test_github_refusing_the_merge_is_409_not_mergeable(make_client, gh):
    green(gh)
    gh.merge_refuse = True
    r = merge(make_client, gh)
    assert r.status_code == 409 and r.json() == {"detail": {"reason": "not_mergeable", "unmet": ["github"]}}


def test_an_unknown_pull_request_cannot_be_merged(make_client, gh):
    r = merge(make_client, gh, number=99)
    assert r.status_code == 404 and r.json() == {"detail": {"reason": "pull_not_found"}}


def test_the_whole_path_from_submit_to_the_waiting_release(make_client, gh):
    """A draft is submitted, its gate passes, a reviewer merges it, and the release waits for approval in GitHub."""
    tree, key = stored_draft(make_client)
    assert submit(make_client, tree, key).status_code == 200
    gh.checks[gh.pulls[5].head_sha] = {"skills-pr": "passed", "platform": "passed"}
    assert status(make_client, gh, tree=tree).json()["gate"]["state"] == "passed"
    assert merge(make_client, gh).status_code == 200
    gh.runs["M5"] = github.Run(url="https://run/9", status="in_progress", conclusion=None, waiting=True)
    body = status(make_client, gh, tree=tree).json()
    assert body["merged"]["merged"] is True
    assert body["release"] == {"state": "waiting_approval", "url": "https://run/9"}


def test_the_reviewer_me_call_is_made_with_the_user_token(make_client, gh):
    """The merge checks the caller's own role with their token, as every reviewer action does."""
    green(gh)
    user = reviewer()
    merge(make_client, gh, user=user)
    assert any(call[1] == ME for call in user.api_client.calls)


# --- the token and the app without GitHub -----------------------------------------------------------------


def test_a_token_in_a_github_error_never_reaches_the_browser_or_the_log(make_client, monkeypatch, caplog):
    monkeypatch.setenv(github.REPO_ENV, "acme/skills")
    monkeypatch.setenv(github.SECRET_ENV, "skillhub/github-token")
    """End to end: the real client, a GitHub that fails with the token in its body. Neither the answer nor the log has
    the token, and the browser gets the error type only."""
    monkeypatch.setattr(github.urllib.request, "urlopen", recording_opener(http_error(500)))
    c = make_client(user_w=reviewer(), app_w=app_with_secret(), cfg=make_cfg(), sql=RowsSql())
    with caplog.at_level(logging.DEBUG):
        r = c.post("/api/pulls/5/merge")
    assert r.status_code == 502
    assert r.json() == {"detail": {"reason": "github_failed", "error": "GitHubUnavailable"}}
    assert TOKEN not in r.text
    assert TOKEN not in caplog.text


def test_without_github_configured_a_merge_answers_503(make_client, monkeypatch):
    monkeypatch.delenv(github.REPO_ENV, raising=False)
    monkeypatch.delenv(github.SECRET_ENV, raising=False)
    app_w = FakeWorkspace(api=FakeApi(), workspace=FakeTree())
    c = make_client(user_w=reviewer(), app_w=app_w, cfg=make_cfg(), sql=RowsSql())
    r = c.post("/api/pulls/5/merge")
    assert r.status_code == 503 and r.json() == {"detail": {"reason": "github_not_configured"}}
