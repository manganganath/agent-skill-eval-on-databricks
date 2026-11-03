"""The releases that wait for approval (GET /api/releases/pending): reviewers only, read only, the approval itself is
made in GitHub. GitHub is FakeGitHub; the pull request's upload and eval rows are the candidates fakes."""

import json
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


def note(title, value):
    """An annotation: `value` is the JSON the workflow writes (a dict), or the raw message (a string)."""
    return github.Annotation(
        title, value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
    )


def put_jobs(
    gh,
    number=9,
    *,
    scan="success",
    scan_status="completed",
    began=None,
    outcome="success",
    plan=None,
    plan_status="completed",
    evaluated=None,
    eval_status="completed",
    eval_conclusion="success",
    more=(),
):
    """The jobs of run `number`: the plan job with its scan step, which GitHub reports as `scan` (it never fails the
    job) and whose record step annotated `outcome` (None: no annotation), and the production job that waits since
    `began`. `scan` "absent" leaves the step out. `plan` is the release plan annotation, `evaluated` the eval one
    (None: none; a list: several), and `more` other annotations of the plan job. `eval_status` None leaves the
    re-evaluation job out."""
    steps = (github.Step(release.SCAN_STEP, scan_status, scan),) if scan != "absent" else ()
    plan_id, eval_id = 200 + number, 400 + number
    gh.jobs[100 + number] = [
        github.Job(plan_id, release.PLAN_JOB, plan_status, "success", None, None, steps),
        github.Job(300 + number, release.RELEASE_JOB, "waiting", None, began, began, ()),
    ]
    if eval_status is not None:
        concluded = eval_conclusion if eval_status == "completed" else None
        gh.jobs[100 + number].insert(
            1, github.Job(eval_id, release.EVAL_JOB, eval_status, concluded, None, None, ())
        )
    as_list = lambda v: [] if v is None else v if isinstance(v, list) else [v]  # noqa: E731
    gh.annotations[plan_id] = [github.Annotation("other", "x"), *more]
    if outcome:
        gh.annotations[plan_id].append(github.Annotation(release.SCAN_TITLE, outcome))
    gh.annotations[plan_id] += [note(release.PLAN_TITLE, p) for p in as_list(plan)]
    gh.annotations[eval_id] = [note(release.EVAL_TITLE, e) for e in as_list(evaluated)]


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
            "skills_source": "pull_request",
            "eval_basis": "inferred",
            "earlier_changes": False,
            "run_key": KEY,
            "skills": [
                {
                    "dept": "common",
                    "skill": "new-skill",
                    "action": "publish",
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
        {
            "dept": "common",
            "skill": "new-skill",
            "action": "publish",
            "verdicts": {},
            "eval_access": "ok",
            "eval_source": None,
        }
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


@pytest.mark.parametrize(
    ("kw", "state"),
    [
        # GitHub reports the step as passed whatever it found (continue-on-error): the annotation says what it found.
        ({"scan": "success", "outcome": "success"}, "passed"),
        ({"scan": "success", "outcome": "failure"}, "failed"),
        ({"scan": "success", "outcome": None}, "unknown"),  # a run from before the record step
        ({"scan": "success", "outcome": "cancelled"}, "unknown"),
        ({"scan": "skipped", "outcome": None}, "not_run"),
        ({"scan": "absent", "outcome": None}, "not_run"),
        ({"scan": None, "scan_status": "in_progress", "outcome": None}, "unknown"),
    ],
)
def test_the_scan_is_the_outcome_the_record_step_annotated(make_client, gh, kw, state):
    merged(gh)
    gh.waiting = [waiting()]
    put_jobs(gh, **kw)
    assert pending(make_client).json()[0]["scan"] == state


def test_the_annotations_of_a_job_are_read_once_and_only_when_the_job_is_completed(make_client, gh):
    merged(gh)
    gh.waiting = [waiting()]
    put_jobs(gh, plan_status="in_progress", eval_status="in_progress")
    assert pending(make_client).json()[0]["scan"] == "unknown"
    assert "job_annotations" not in gh.calls
    put_jobs(gh, eval_status=None)  # one completed job: the plan job answers the scan and the plan
    pending(make_client)
    assert gh.calls.count("job_annotations") == 1
    gh.calls.clear()
    put_jobs(gh)  # the re-evaluation job too: one read each
    pending(make_client)
    assert gh.calls.count("job_annotations") == 2
    gh.calls.clear()
    put_jobs(gh, eval_conclusion="skipped")  # a skipped job has none
    pending(make_client)
    assert gh.calls.count("job_annotations") == 1


def test_annotations_that_cannot_be_read_leave_the_scan_unknown_and_the_wait_known(make_client, gh):
    merged(gh)
    gh.waiting = [waiting()]
    put_jobs(gh, began=datetime(2026, 10, 29, 13, 40, tzinfo=UTC))
    gh.fail["job_annotations"] = github.GitHubUnavailable(502)
    body = pending(make_client).json()[0]
    assert body["scan"] == "unknown" and body["waiting_since"] == "2026-10-29T13:40:00Z"


def test_without_a_plan_job_the_scan_did_not_run_and_the_wait_is_the_runs_last_change(make_client, gh):
    merged(gh)
    gh.waiting = [waiting()]
    body = pending(make_client).json()[0]
    assert body["scan"] == "not_run" and body["waiting_since"] == "2026-10-29T13:30:00Z"


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
            "scan": "not_run",
            "skills_source": "pull_request",
            "eval_basis": "inferred",
            "earlier_changes": False,
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


# --- the release's own facts: the plan and the eval the workflow annotated ---

LONG_SHA = "m5aaaaa" + "0" * 33


def plan_of(publish=(), retire=()):
    return {
        "publish": [{"dept": d, "skill": n} for d, n in publish],
        "retire": [{"dept": d, "skill": n} for d, n in retire],
    }


def listed(body, release_index=0):
    return [(sk["dept"], sk["skill"], sk["action"]) for sk in body[release_index]["skills"]]


def test_the_skills_are_what_the_release_plan_publishes_and_retires_not_the_last_pull_requests(
    make_client, gh
):
    merged(gh, paths=["skills/common/last-one/SKILL.md"])  # pull request #5 changed one skill only
    gh.waiting = [waiting()]
    plan = plan_of(publish=[("common", "earlier"), ("common", "last-one")], retire=[("common", "gone")])
    put_jobs(gh, plan=plan, evaluated=None, eval_status=None)
    body = pending(make_client).json()
    assert listed(body) == [
        ("common", "earlier", "publish"),
        ("common", "last-one", "publish"),
        ("common", "gone", "retire"),
    ]
    assert body[0]["skills_source"] == "release" and body[0]["pull"]["number"] == 5
    assert (
        body[0]["earlier_changes"] is True
    )  # the release carries a skill that pull request #5 did not change


def test_a_release_of_one_merge_does_not_claim_earlier_changes(make_client, gh):
    merged(gh)
    gh.waiting = [waiting()]
    put_jobs(gh, plan=plan_of(publish=[("common", "new-skill")]))
    body = pending(make_client).json()
    assert body[0]["skills_source"] == "release" and body[0]["earlier_changes"] is False


def test_a_retirement_is_a_retirement_with_no_eval_to_look_for(make_client, gh):
    merged(gh, paths=["skills/common/gone/SKILL.md"])
    gh.waiting = [waiting()]
    put_jobs(gh, plan=plan_of(retire=[("common", "gone")]), eval_status=None)
    sql = RowsSql(eval_rows(skill="gone"))
    body = pending(make_client, sql=sql).json()
    assert body[0]["skills"] == [
        {
            "dept": "common",
            "skill": "gone",
            "action": "retire",
            "verdicts": {},
            "eval_access": "ok",
            "eval_source": None,
        }
    ]
    assert sql.calls == []  # nothing was evaluated, so no eval table is read for it


def test_a_direct_push_lists_the_skills_the_release_publishes(make_client, gh):
    gh.waiting = [waiting(sha="D1")]  # no pull request has this merge commit
    put_jobs(gh, plan=plan_of(publish=[("common", "new-skill")]))
    body = pending(make_client).json()
    assert body[0]["pull"] is None and listed(body) == [("common", "new-skill", "publish")]
    assert body[0]["skills_source"] == "release" and body[0]["earlier_changes"] is False


def test_the_eval_the_run_annotated_is_the_one_shown_whatever_other_runs_of_the_commit_left(make_client, gh):
    merged(gh, sha=LONG_SHA)
    gh.waiting = [waiting(sha=LONG_SHA)]
    mine = "merge_m5aaaaa_r98_a1"
    put_jobs(
        gh,
        plan=plan_of(publish=[("common", "new-skill")]),
        evaluated={"run_key": mine, "trigger_source": "merge"},
    )
    rows = {
        "common": [
            merge_row("fail", "merge_m5aaaaa_r102_a1"),
            merge_row("warn", mine),
            merge_row("pass", "merge_m5aaaaa_a2"),
        ]
    }
    body = pending(make_client, sql=RowsSql(rows)).json()
    skill = body[0]["skills"][0]
    assert skill["verdicts"] == {"claude-sonnet": "warn"} and skill["eval_source"] == "merge"
    assert body[0]["eval_basis"] == "release"


def test_the_trigger_source_of_the_eval_note_says_where_the_verdicts_come_from(make_client, gh):
    merged(gh)
    gh.waiting = [waiting()]
    put_jobs(
        gh,
        plan=plan_of(publish=[("common", "new-skill")]),
        evaluated={"run_key": KEY, "trigger_source": "pr"},
    )
    body = pending(make_client, sql=RowsSql(eval_rows("warn"))).json()
    assert body[0]["skills"][0]["eval_source"] == "pull_request"
    assert body[0]["skills"][0]["verdicts"] == {"claude-sonnet": "warn"}


def test_an_eval_note_with_no_rows_still_says_what_the_release_gates_on(make_client, gh):
    merged(gh, sha=LONG_SHA)
    gh.waiting = [waiting(sha=LONG_SHA)]
    put_jobs(
        gh,
        plan=plan_of(publish=[("common", "new-skill")]),
        evaluated={"run_key": MERGE_KEY, "trigger_source": "merge"},
    )
    skill = pending(make_client, sql=RowsSql({"common": []})).json()[0]["skills"][0]
    assert skill["verdicts"] == {} and skill["eval_source"] == "merge"


@pytest.mark.parametrize(
    "plan",
    [
        "not json",
        "[]",
        {"publish": [{"dept": "common", "skill": "x"}]},  # no retire list
        {"publish": [{"dept": "nowhere", "skill": "x"}], "retire": []},  # a department the Hub does not have
        {"publish": [{"dept": "common", "skill": "../x"}], "retire": []},
        {"publish": [{"dept": "common"}], "retire": []},
        {"publish": [], "retire": "all"},
    ],
)
def test_a_plan_annotation_that_is_not_the_expected_shape_is_not_trusted(make_client, gh, plan):
    merged(gh)
    gh.waiting = [waiting()]
    put_jobs(gh, plan=plan)
    body = pending(make_client).json()
    assert body[0]["skills_source"] == "unknown" and listed(body) == [("common", "new-skill", "publish")]


def test_two_plan_annotations_are_never_trusted_and_fall_back_to_the_pull_request(make_client, gh):
    merged(gh)
    gh.waiting = [waiting()]
    forged = plan_of(publish=[("common", "forged")])
    put_jobs(gh, plan=[forged, plan_of(publish=[("common", "new-skill")])])
    body = pending(make_client).json()
    assert body[0]["skills_source"] == "unknown" and listed(body) == [("common", "new-skill", "publish")]


@pytest.mark.parametrize(
    "evaluated",
    [
        [
            {"run_key": MERGE_KEY, "trigger_source": "merge"},
            {"run_key": "merge_m5aaaaa_a9", "trigger_source": "merge"},
        ],
        {"run_key": MERGE_KEY, "trigger_source": "other"},
        {"run_key": "merge_bbbbbbb_a1", "trigger_source": "merge"},  # the key of another commit
        {"run_key": "pr5_c7ecd3e_a1", "trigger_source": "merge"},  # a key that does not fit its source
        {"run_key": "merge_m5aaaaa_a1\n::x", "trigger_source": "merge"},
        "not json",
    ],
)
def test_an_eval_note_that_is_duplicated_or_does_not_fit_the_run_is_not_trusted(make_client, gh, evaluated):
    merged(gh, sha=LONG_SHA)
    gh.waiting = [waiting(sha=LONG_SHA)]
    put_jobs(gh, plan=plan_of(publish=[("common", "new-skill")]), evaluated=evaluated)
    rows = {"common": [merge_row("fail", "merge_m5aaaaa_a3")]}
    body = pending(make_client, sql=RowsSql(rows)).json()
    assert body[0]["eval_basis"] == "unknown"
    # The page falls back to its own guess: the newest merge eval of the commit.
    assert body[0]["skills"][0]["verdicts"] == {"claude-sonnet": "fail"}


def test_a_run_from_before_the_notes_falls_back_to_the_pull_request_and_the_guessed_eval(make_client, gh):
    merged(gh, sha=LONG_SHA)
    gh.waiting = [waiting(sha=LONG_SHA)]
    put_jobs(gh)
    body = pending(make_client, sql=RowsSql({"common": [merge_row("warn")]})).json()
    assert (body[0]["skills_source"], body[0]["eval_basis"]) == ("pull_request", "inferred")
    assert body[0]["skills"][0]["eval_source"] == "merge"


def test_notes_that_cannot_be_read_are_unknown_not_absent(make_client, gh):
    merged(gh)
    gh.waiting = [waiting()]
    put_jobs(gh, plan=plan_of(publish=[("common", "other")]))
    gh.fail["job_annotations"] = github.GitHubUnavailable(502)
    body = pending(make_client).json()
    assert (body[0]["skills_source"], body[0]["eval_basis"], body[0]["scan"]) == (
        "unknown",
        "unknown",
        "unknown",
    )
    assert listed(body) == [("common", "new-skill", "publish")]


@pytest.mark.parametrize(
    ("notes", "state"),
    [
        (["success"], "passed"),
        (["success", "success"], "passed"),
        (["success", "failure"], "failed"),  # a forged success first does not hide the real failure
        (["failure", "success"], "failed"),
        (["failure"], "failed"),
        (["success", "cancelled"], "unknown"),
        (["success", "success\n"], "unknown"),
        ([], "unknown"),
    ],
)
def test_the_scan_passes_only_when_every_scan_annotation_says_success(make_client, gh, notes, state):
    merged(gh)
    gh.waiting = [waiting()]
    put_jobs(gh, outcome=None, more=[github.Annotation(release.SCAN_TITLE, n) for n in notes])
    assert pending(make_client).json()[0]["scan"] == state


READS = ("waiting_runs", "merged_pull_number", "pull", "pull_paths", "run_jobs", "job_annotations")


def test_nothing_the_list_runs_can_approve_or_write():
    """The source of every module the list runs, and of each GitHub method it calls: no deployment review endpoint, no
    raw request and no verb but GET. The Hub's pull request writes live in other methods of the client."""
    import ast
    import inspect
    import re
    import textwrap

    from backend.api import releases

    pages = {"api.releases": inspect.getsource(releases), "release": inspect.getsource(release)}
    methods = {name: inspect.getsource(getattr(github.GitHubClient, name)) for name in READS}
    for where, source in {**pages, **methods}.items():
        assert "pending_deployments" not in source and "deployment_protection_rule" not in source, where
        assert "._request" not in source, where  # a raw request could be any verb; reads go through get()
        verbs = {
            n.value
            for n in ast.walk(ast.parse(textwrap.dedent(source)))
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        }
        assert not verbs & {"POST", "PUT", "PATCH", "DELETE"}, where
    assert all("self.get(" in source for name, source in methods.items() if name != "pull"), methods.keys()
    # The page module calls the client only through these reads.
    assert set(re.findall(r"\bgh\.(\w+)\(", pages["api.releases"])) <= set(READS)
    assert "pending_deployments" not in inspect.getsource(github) + inspect.getsource(release)


class Transport:
    """urlopen for a real GitHubClient: answers GitHub's read endpoints for one waiting run and records each request."""

    def __init__(self):
        self.requests: list[tuple[str, str]] = []
        self.plan = plan_of(publish=[("common", "new-skill")])

    def __call__(self, req, timeout=None):
        from urllib.parse import urlsplit

        self.requests.append((req.get_method(), req.full_url))
        path = urlsplit(req.full_url).path
        answers = {
            "/workflows/skills-publish.yml/runs": {
                "workflow_runs": [{"id": 109, "html_url": RUN_URL, "run_number": 9, "head_sha": "M5"}]
            },
            "/commits/M5/pulls": [{"number": 5, "merge_commit_sha": "M5"}],
            "/pulls/5": {
                "number": 5,
                "title": "t",
                "merged": True,
                "head": {"sha": SHA},
                "user": {"login": "ana"},
            },
            "/pulls/5/files": [{"filename": PATHS[0]}],
            "/actions/runs/109/jobs": {
                "jobs": [
                    {"id": 7, "name": release.PLAN_JOB, "status": "completed", "conclusion": "success"},
                    {"id": 8, "name": release.EVAL_JOB, "status": "completed", "conclusion": "success"},
                    {"id": 9, "name": release.RELEASE_JOB, "status": "waiting"},
                ]
            },
            "/check-runs/7/annotations": [
                {"title": release.PLAN_TITLE, "message": json.dumps(self.plan)},
                {"title": release.SCAN_TITLE, "message": "success"},
            ],
            "/check-runs/8/annotations": [
                {"title": release.EVAL_TITLE, "message": json.dumps({"run_key": KEY, "trigger_source": "pr"})}
            ],
        }
        suffix = next((k for k in answers if path.endswith(k)), None)
        assert suffix is not None, f"unexpected request {path}"
        body = json.dumps(answers[suffix]).encode()

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return body

        return Response()


@pytest.fixture
def real_client(monkeypatch):
    """A real GitHubClient over a recording transport, in place of the fake repository."""
    transport = Transport()
    github._cache.clear()
    monkeypatch.setattr(
        github,
        "connect",
        lambda app_w, cached=True: github.GitHubClient("acme/skills", "t", cached=cached, opener=transport),
    )
    yield transport
    github._cache.clear()


def test_every_request_made_to_build_the_list_is_a_get(make_client, real_client):
    body = pending(make_client, sql=RowsSql(eval_rows())).json()
    assert body[0]["skills_source"] == "release" and body[0]["eval_basis"] == "release"
    assert real_client.requests and {method for method, _ in real_client.requests} == {"GET"}


def test_a_refresh_costs_two_requests_once_the_settled_answers_are_kept(
    make_client, real_client, monkeypatch
):
    pending(make_client)
    cold = len(real_client.requests)
    assert cold == 7  # runs, commit pulls, pull, files, jobs, plan job notes, eval job notes
    real_client.requests.clear()
    github._cache.clear()  # a different worker, or a restart, starts cold again
    assert len(pending(make_client).json()) == 1 and len(real_client.requests) == cold
    real_client.requests.clear()
    later = github.time.monotonic() + github.ANSWER_S + 1
    monkeypatch.setattr(github.time, "monotonic", lambda: later)
    pending(make_client)
    # Only the run list and the jobs of the run, which hold the waiting state, are read again.
    assert [u.split("/repos/acme/skills")[1].split("?")[0] for _, u in real_client.requests] == [
        "/actions/workflows/skills-publish.yml/runs",
        "/actions/runs/109/jobs",
    ]


def test_the_list_only_reads_github_and_never_approves_or_writes(make_client, gh):
    merged(gh)
    gh.waiting = [waiting()]
    assert pending(make_client, sql=RowsSql(eval_rows())).status_code == 200
    assert WRITES.isdisjoint(gh.calls)
    assert set(gh.calls) <= set(READS)


def test_eval_key_picks_the_newest_attempt_of_the_merged_head():
    from backend.candidates import parse_key

    keys = [parse_key(k) for k in ("pr5_c7ecd3e_a1", "pr5_c7ecd3e_a3", "pr5_c7ecd3e_a2", "pr5_bbbbbbb_a9")]
    assert release.eval_key(keys, SHA, 5).run_key == "pr5_c7ecd3e_a3"
    assert release.eval_key(keys, "f" * 40, 5) is None


def test_eval_key_ignores_the_upload_of_another_pull_request_at_the_same_head():
    from backend.candidates import parse_key

    # #8 was closed and its branch reopened as #12 at the same head: each has its own upload folder.
    keys = [parse_key(k) for k in ("pr8_c7ecd3e_a1", "pr12_c7ecd3e_a1")]
    assert release.eval_key(keys, SHA, 12).run_key == "pr12_c7ecd3e_a1"
    assert release.eval_key(keys, SHA, 8).run_key == "pr8_c7ecd3e_a1"
    assert release.eval_key(keys, SHA, 9) is None


def test_the_workflow_still_has_the_job_and_step_names_the_list_matches_on():
    import yaml
    from conftest import REPO

    jobs = yaml.safe_load((REPO / ".github/workflows/skills-publish.yml").read_text())["jobs"]
    plan = next(j for j in jobs.values() if j.get("name") == release.PLAN_JOB)
    steps = {step.get("name"): step for step in plan["steps"]}
    scan = steps[release.SCAN_STEP]
    assert scan["id"] == release.SCAN_STEP_ID and scan["continue-on-error"] is True
    record = steps[release.RECORD_STEP]
    # A failed scan leaves the job successful, so the implicit success() lets this step run after it; it does not run
    # when the scan was skipped or never reached. The outcome goes in by env.
    assert f"steps.{release.SCAN_STEP_ID}.outcome != ''" in record["if"]
    assert f"steps.{release.SCAN_STEP_ID}.outcome != 'skipped'" in record["if"]
    assert record["env"]["OUTCOME"] == f"${{{{ steps.{release.SCAN_STEP_ID}.outcome }}}}"
    assert record["run"] == f'echo "::notice title={release.SCAN_TITLE}::$OUTCOME"'
    assert list(steps).index(release.RECORD_STEP) == list(steps).index(release.SCAN_STEP) + 1
    prod = next(j for j in jobs.values() if j.get("name") == release.RELEASE_JOB)
    assert prod["environment"] == "production"
