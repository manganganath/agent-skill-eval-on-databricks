"""The releases that wait for approval (GET /api/releases/pending): reviewers only, read only, the approval itself is
made in GitHub. GitHub is FakeGitHub; the pull request's upload and eval rows are the candidates fakes."""

import logging
from datetime import UTC, datetime

import pytest
from app_fakes import FakeTree
from backend import github, release
from conftest import make_cfg
from test_app_candidates import KEY, SHA, RowsSql, pr_folder, row
from test_app_candidates import app as candidates_app
from test_app_drafts import author, reviewer, user
from test_app_pulls import gh as gh  # noqa: F401 - the fixture, with the pull request tests' autouse fixtures
from test_app_pulls import put_pull

RUN_URL = "https://github.test/acme/skills/actions/runs/9"
PATHS = ["skills/common/new-skill/SKILL.md"]
WRITES = {"create_blob", "create_tree", "create_commit", "create_ref", "update_ref", "create_pull", "merge"}


def waiting(number=9, sha="M5", updated=datetime(2026, 10, 29, 13, 30, tzinfo=UTC)):
    return github.WaitingRun(
        id=100 + number,
        url=f"https://github.test/acme/skills/actions/runs/{number}",
        number=number,
        sha=sha,
        created_at=datetime(2026, 10, 29, 12, 1, tzinfo=UTC),
        updated_at=updated,
    )


def put_jobs(gh, number=9, *, scan="success", scan_status="completed", began=None):
    """The jobs of run `number`: the plan job with its scan step, and the production job that waits since `began`."""
    steps = (github.Step(release.SCAN_STEP, scan_status, scan),) if scan != "absent" else ()
    gh.jobs[100 + number] = [
        github.Job(release.PLAN_JOB, "completed", "success", None, None, steps),
        github.Job(release.RELEASE_JOB, "waiting", None, began, began, ()),
    ]


def merged(gh, number=5, *, sha="M5", head=SHA, paths=PATHS, **fields):
    put_pull(
        gh,
        number,
        paths=paths,
        state="closed",
        merged=True,
        merged_at="2026-10-29T12:00:00Z",
        merge_sha=sha,
        head_sha=head,
        title="Add new-skill (common) from the Skill Hub",
        author="ana",
        merged_by="rita",
        **fields,
    )


def pending(make_client, *, user=None, tree=None, sql=None):
    tree = tree if tree is not None else FakeTree(pr_folder(skills=(("common", "new-skill"),)))
    c = make_client(
        user_w=user or reviewer(), app_w=candidates_app(tree), cfg=make_cfg(), sql=sql or RowsSql()
    )
    return c.get("/api/releases/pending")


def eval_rows(verdict="pass", skill="new-skill"):
    return {"common": [row(skill, "claude-sonnet", "trigger", verdict)]}


def test_a_reviewer_sees_the_waiting_release_with_its_pull_request_skills_verdicts_and_scan(make_client, gh):
    merged(gh)
    gh.waiting = [waiting()]
    put_jobs(gh, began=datetime(2026, 10, 29, 13, 40, tzinfo=UTC))
    r = pending(make_client, sql=RowsSql(eval_rows("pass")))
    assert r.status_code == 200, r.text
    assert r.json() == [
        {
            "run_url": RUN_URL,
            "run_number": 9,
            "sha": "M5",
            "started_at": "2026-10-29T12:01:00Z",
            "waiting_since": "2026-10-29T13:40:00Z",
            "pull": {
                "number": 5,
                "title": "Add new-skill (common) from the Skill Hub",
                "author": "ana",
                "merged_by": "rita",
                "url": "https://github.test/acme/skills/pull/5",
            },
            "details": "ok",
            "scan": "passed",
            "run_key": KEY,
            "skills": [
                {
                    "dept": "common",
                    "skill": "new-skill",
                    "verdicts": {"claude-sonnet": "pass"},
                    "eval_access": "ok",
                    "eval_source": "pull_request",
                }
            ],
        }
    ]


def test_the_eval_of_the_merged_head_is_the_one_shown_not_a_later_attempt_of_another_commit(make_client, gh):
    merged(gh)
    gh.waiting = [waiting()]
    other = "pr5_aaaaaaa_a2"
    tree = FakeTree(
        {**pr_folder(skills=(("common", "new-skill"),)), **pr_folder(other, skills=(("common", "x"),))}
    )
    rows = {"common": [row("new-skill", "claude-sonnet", "trigger", "fail", key=other)]}
    rows["common"].append(row("new-skill", "claude-sonnet", "trigger", "warn"))
    body = pending(make_client, tree=tree, sql=RowsSql(rows)).json()
    assert body[0]["run_key"] == KEY
    assert body[0]["skills"][0]["verdicts"] == {"claude-sonnet": "warn"}


def test_skills_are_listed_even_when_the_pull_request_upload_is_gone(make_client, gh):
    merged(gh)
    gh.waiting = [waiting()]
    body = pending(make_client, tree=FakeTree(), sql=RowsSql(eval_rows())).json()
    assert body[0]["run_key"] is None
    assert body[0]["skills"] == [
        {"dept": "common", "skill": "new-skill", "verdicts": {}, "eval_access": "ok", "eval_source": None}
    ]


MERGE_KEY = "merge_m5aaaaa_a1"


def merge_row(verdict, key=MERGE_KEY, **extra):
    return row("new-skill", "claude-sonnet", "trigger", verdict, key=key, **extra)


def test_the_merge_eval_of_the_commit_is_what_the_release_gates_on_when_it_has_rows(make_client, gh):
    merged(gh, sha="m5aaaaa" + "0" * 33)
    gh.waiting = [waiting(sha="m5aaaaa" + "0" * 33)]
    rows = {"common": [row("new-skill", "claude-sonnet", "trigger", "pass"), merge_row("fail")]}
    skill = pending(make_client, sql=RowsSql(rows)).json()[0]["skills"][0]
    assert skill["verdicts"] == {"claude-sonnet": "fail"} and skill["eval_source"] == "merge"


def test_the_newest_attempt_of_the_merge_eval_counts_and_a_hub_run_of_it_does_not(make_client, gh):
    merged(gh, sha="m5aaaaa" + "0" * 33)
    gh.waiting = [waiting(sha="m5aaaaa" + "0" * 33)]
    rows = {
        "common": [
            merge_row("fail", "merge_m5aaaaa_a1"),
            merge_row("warn", "merge_m5aaaaa_r7_a2"),
            merge_row("pass", "merge_m5aaaaa_r7_a2_h0123abcd"),
            merge_row("fail", "merge_bbbbbbb_a9"),
        ]
    }
    skill = pending(make_client, sql=RowsSql(rows)).json()[0]["skills"][0]
    assert skill["verdicts"] == {"claude-sonnet": "warn"} and skill["eval_source"] == "merge"


def test_the_waiting_run_scan_is_how_its_scan_step_ended(make_client, gh):
    merged(gh)
    gh.waiting = [waiting()]
    for conclusion, status, state in [
        ("success", "completed", "passed"),
        ("failure", "completed", "failed"),
        ("skipped", "completed", "not_run"),
        (None, "in_progress", "unknown"),
        ("cancelled", "completed", "unknown"),
        ("absent", "completed", "unknown"),
    ]:
        put_jobs(gh, scan=conclusion, scan_status=status)
        assert pending(make_client).json()[0]["scan"] == state


def test_without_jobs_the_scan_is_unknown_and_the_wait_is_the_runs_last_change(make_client, gh):
    merged(gh)
    gh.waiting = [waiting()]
    body = pending(make_client).json()[0]
    assert body["scan"] == "unknown" and body["waiting_since"] == "2026-10-29T13:30:00Z"


def test_failing_jobs_read_leaves_the_run_listed_with_an_unknown_scan(make_client, gh):
    merged(gh)
    gh.waiting = [waiting()]
    gh.fail["run_jobs"] = github.GitHubUnavailable(502)
    body = pending(make_client).json()[0]
    assert body["scan"] == "unknown" and body["pull"]["number"] == 5


def test_eval_results_the_reviewer_may_not_read_are_marked_denied(make_client, gh):
    merged(gh)
    gh.waiting = [waiting()]
    skill = pending(make_client, sql=RowsSql(eval_rows(), denied=["common"])).json()[0]["skills"][0]
    assert skill["eval_access"] == "denied" and skill["verdicts"] == {}


def test_a_waiting_run_with_no_pull_request_is_listed_with_what_is_known(make_client, gh):
    gh.waiting = [waiting(sha="D1")]  # pushed straight to main: no pull request has this merge commit
    body = pending(make_client).json()
    assert body == [
        {
            "run_url": RUN_URL,
            "run_number": 9,
            "sha": "D1",
            "started_at": "2026-10-29T12:01:00Z",
            "waiting_since": "2026-10-29T13:30:00Z",
            "pull": None,
            "details": "ok",
            "scan": "unknown",
            "run_key": None,
            "skills": [],
        }
    ]


def test_github_failing_for_one_run_leaves_the_others_listed(make_client, gh, caplog):
    merged(gh, 5, sha="M5")
    merged(gh, 6, sha="M6", head=SHA)
    gh.waiting = [waiting(9, "M5"), waiting(10, "M6", updated=datetime(2026, 10, 29, 14, 0, tzinfo=UTC))]
    real = gh.pull

    def pull(number):
        if number == 5:
            raise github.GitHubUnavailable(502)
        return real(number)

    gh.pull = pull
    with caplog.at_level(logging.WARNING, logger="skillhub.app.releases"):
        body = pending(make_client).json()
    assert [(r["run_number"], r["details"], r["pull"] and r["pull"]["number"]) for r in body] == [
        (9, "unavailable", None),
        (10, "ok", 6),
    ]
    assert "GitHubUnavailable" in caplog.text


def test_a_pull_request_too_large_to_read_lists_no_skills(make_client, gh):
    merged(gh)
    gh.waiting = [waiting()]
    gh.fail["pull_paths"] = github.GitHubRejected(422)
    body = pending(make_client).json()
    assert body[0]["details"] == "ok" and body[0]["skills"] == []


def test_the_release_that_began_to_wait_first_comes_first_by_its_production_job(make_client, gh):
    merged(gh, 5, sha="M5")
    merged(gh, 6, sha="M6")
    # Run 10 changed last, but its production job began to wait before run 9's.
    gh.waiting = [waiting(10, "M6"), waiting(9, "M5", updated=datetime(2026, 10, 29, 9, 0, tzinfo=UTC))]
    put_jobs(gh, 10, began=datetime(2026, 10, 29, 10, 0, tzinfo=UTC))
    put_jobs(gh, 9, began=datetime(2026, 10, 29, 11, 0, tzinfo=UTC))
    assert [r["run_number"] for r in pending(make_client).json()] == [10, 9]


def test_nothing_waiting_is_an_empty_list_and_nothing_else_is_read(make_client, gh):
    tree = FakeTree()
    r = pending(make_client, tree=tree)
    assert r.status_code == 200 and r.json() == []
    assert gh.calls == ["waiting_runs"]
    assert tree.calls == []


@pytest.mark.parametrize("who", [author, lambda: user(())])
def test_anyone_but_a_reviewer_is_403_before_anything_is_read(make_client, gh, who):
    gh.waiting = [waiting()]
    tree = FakeTree(pr_folder())
    sql = RowsSql(eval_rows())
    r = pending(make_client, user=who(), tree=tree, sql=sql)
    assert r.status_code == 403 and r.json()["detail"]["reason"] == "not_reviewer"
    assert gh.calls == [] and tree.calls == [] and sql.calls == []


def test_without_github_configured_it_is_503(make_client, monkeypatch):
    monkeypatch.delenv(github.REPO_ENV, raising=False)
    monkeypatch.delenv(github.SECRET_ENV, raising=False)
    r = pending(make_client)
    assert r.status_code == 503 and r.json()["detail"]["reason"] == "github_not_configured"


def test_github_failing_to_list_the_runs_is_502(make_client, gh):
    gh.fail["waiting_runs"] = github.GitHubDenied(403)
    r = pending(make_client)
    assert r.status_code == 502 and r.json()["detail"]["reason"] == "github_failed"


def test_the_list_only_reads_github_and_never_approves_or_writes(make_client, gh):
    merged(gh)
    gh.waiting = [waiting()]
    assert pending(make_client, sql=RowsSql(eval_rows())).status_code == 200
    assert WRITES.isdisjoint(gh.calls)
    assert set(gh.calls) <= {"waiting_runs", "merged_pull_number", "pull", "pull_paths", "run_jobs"}


def test_the_github_client_has_no_call_to_the_deployment_review_endpoints():
    import inspect

    source = inspect.getsource(github) + inspect.getsource(release)
    assert "pending_deployments" not in source and "deployment_protection_rule" not in source


def test_eval_key_picks_the_newest_attempt_of_the_merged_head():
    from backend.candidates import parse_key

    keys = [parse_key(k) for k in ("pr5_c7ecd3e_a1", "pr5_c7ecd3e_a3", "pr5_c7ecd3e_a2", "pr5_bbbbbbb_a9")]
    assert release.eval_key(keys, SHA).run_key == "pr5_c7ecd3e_a3"
    assert release.eval_key(keys, "f" * 40) is None


def test_the_workflow_still_has_the_job_and_step_names_the_list_matches_on():
    import yaml
    from conftest import REPO

    jobs = yaml.safe_load((REPO / ".github/workflows/skills-publish.yml").read_text())["jobs"]
    plan = next(j for j in jobs.values() if j.get("name") == release.PLAN_JOB)
    assert release.SCAN_STEP in [step.get("name") for step in plan["steps"]]
    prod = next(j for j in jobs.values() if j.get("name") == release.RELEASE_JOB)
    assert prod["environment"] == "production"
