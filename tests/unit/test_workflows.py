from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from skillhub.naming import candidates_root
from tests.fakes import make_cfg

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
SECRET_REF = re.compile(r"secrets\.[A-Za-z_]+")


def load_workflow(name: str) -> dict:
    doc = yaml.safe_load((WORKFLOWS / name).read_text())
    # PyYAML reads the bare `on:` key as boolean True.
    doc["on"] = doc.pop(True, doc.get("on"))
    return doc


def all_steps(doc: dict):
    for job in doc["jobs"].values():
        yield from job.get("steps", [])


def step_named(doc: dict, prefix: str) -> dict:
    return next(s for s in all_steps(doc) if s.get("name", "").startswith(prefix))


def test_skills_pr_triggers_permissions_and_concurrency():
    wf = load_workflow("skills-pr.yml")
    assert "paths" not in (wf["on"]["pull_request"] or {})  # a required check must always start
    assert "workflow_dispatch" in wf["on"]
    assert "pull_request_target" not in wf["on"]
    assert wf["concurrency"]["cancel-in-progress"] is True
    assert "skills-pr-" in wf["concurrency"]["group"]
    assert "inputs.pr_number" in wf["concurrency"]["group"]
    assert "permissions" not in wf
    assert wf["jobs"]["skills-pr"]["permissions"] == {
        "contents": "write",
        "pull-requests": "write",
        "actions": "write",
    }


def test_skills_pr_job_is_the_required_check_name():
    wf = load_workflow("skills-pr.yml")
    assert [job["name"] for job in wf["jobs"].values()] == ["skills-pr"]


def test_every_checkout_has_full_history_and_no_persisted_credentials():
    wf = load_workflow("skills-pr.yml")
    checkouts = [s for s in all_steps(wf) if s.get("uses", "").startswith("actions/checkout")]
    assert checkouts
    for s in checkouts:
        assert s["with"]["fetch-depth"] == 0
        assert s["with"]["persist-credentials"] is False


def test_skills_pr_fork_guard():
    """Forks never reach Databricks, never push, and get a clear failure."""
    wf = load_workflow("skills-pr.yml")
    gen = step_named(wf, "2. Generate missing evals and rubrics")
    ev = step_named(wf, "Start skill-eval Job and wait")
    assert "is_fork == 'false'" in gen["if"]
    assert "is_fork == 'false'" in step_named(wf, "2b.")["if"]
    assert "is_fork == 'false'" in step_named(wf, "Detect generated")["if"]
    assert "is_fork == 'false'" in ev["if"]
    assert "is_fork == 'false'" in step_named(wf, "Commit generated files")["if"]
    assert "is_fork == 'false'" in step_named(wf, "Install Databricks CLI")["if"]
    assert ev["id"] == "eval"
    status = step_named(wf, "Status")
    assert "IS_FORK" in status["run"] and "exit 1" in status["run"]
    assert "GITHUB_STEP_SUMMARY" in status["run"]


def test_skills_pr_generate_commit_and_redispatch():
    wf = load_workflow("skills-pr.yml")
    gen = step_named(wf, "2. Generate missing evals and rubrics")
    assert 'skillhub generate --skills "$SKILLS"' in gen["run"]
    assert "--rubric-only" not in gen["run"]
    commit = step_named(wf, "Commit generated files")
    assert "steps.generate.outputs.generated == 'true'" in commit["if"]
    run = commit["run"]
    assert "gh auth setup-git" not in run
    assert "credential.helper=!gh auth git-credential' push" in run  # scoped to the push
    assert "gh workflow run skills-pr.yml" in run and "pr_number" in run
    assert "Co-authored-by: Isaac" in run
    for name in ("Install Databricks CLI", "Start skill-eval Job and wait"):
        assert "generated != 'true'" in step_named(wf, name)["if"]
    assert "non_deleted != ''" in step_named(wf, "Start skill-eval Job and wait")["if"]


def test_skills_pr_eval_step_contract():
    wf = load_workflow("skills-pr.yml")
    run = step_named(wf, "Start skill-eval Job and wait")["run"]
    # Skills run in parallel workers, but a large PR still takes hours: the full gate, not a 30 min cut-off.
    assert "--timeout 300m" in run and "databricks jobs run-now" in run
    assert step_named(wf, "Start skill-eval Job and wait")["name"].endswith("(5 h)")
    assert wf["jobs"]["skills-pr"]["timeout-minutes"] == 330  # the wait plus the steps around it
    assert "-t prod" not in run and "SKILLHUB_JOB_ID_EVAL" in run
    assert "workspace import-dir skills" in run and "--overwrite" in run
    assert 'root="/Workspace$ws_root/$SKILLHUB_CATALOG/candidates/$key"' in run
    assert step_named(wf, "Start skill-eval Job and wait")["env"]["SKILLHUB_WORKSPACE_ROOT"] == (
        "${{ vars.SKILLHUB_WORKSPACE_ROOT }}"
    )
    assert "/Shared/skill-hub" not in run
    assert 'databricks workspace import "$root/.uploaded_at"' in run  # the cleanup's upload marker
    match = re.search(r"job_parameters: \{(.*?)\}\}'", run, re.S)
    assert match
    assert set(re.findall(r"(\w+): ", match.group(1))) == {
        "run_key", "skills", "models", "modes", "source", "trigger_source",
        "requested_by", "skills_root", "fixtures_root",
    }  # fmt: skip
    assert '.state.result_state == "SUCCESS"' in run and "run.json" in run and "-o json" in run
    assert "pr${PR_NUMBER}-${HEAD_SHA:0:7}-a${RUN_ATTEMPT}" in step_named(wf, "Find changed")["run"]


def test_skills_pr_comment_always_runs_then_check_verdict_gates():
    wf = load_workflow("skills-pr.yml")
    steps = list(all_steps(wf))
    comment = next(s for s in steps if s.get("id") == "pr-comment")
    gate = next(s for s in steps if s.get("id") == "check-verdict")
    assert comment["if"].startswith("${{ !cancelled()")
    assert "skillhub pr-comment" in comment["run"]
    assert "skillhub check-verdict" in gate["run"]
    assert "continue-on-error" not in gate
    assert steps.index(comment) < steps.index(gate) < steps.index(step_named(wf, "Status"))
    assert comment["env"]["GITHUB_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"


def test_every_action_is_pinned_to_a_full_sha():
    wf = load_workflow("skills-pr.yml")
    uses = [s["uses"] for s in all_steps(wf) if "uses" in s]
    assert len(uses) == 3
    for u in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", u), u
    text = (WORKFLOWS / "skills-pr.yml").read_text()
    assert "setup-cli@9b64c94a6d2b2e830592d80e9f14c26d32b42ad7 # v1.20.0" in text


def test_no_token_at_workflow_or_job_level_env():
    wf = load_workflow("skills-pr.yml")
    for env in [wf.get("env") or {}] + [j.get("env") or {} for j in wf["jobs"].values()]:
        assert not {"GH_TOKEN", "GITHUB_TOKEN"} & set(env)
    allowed = {"Resolve pull request", "Commit generated files to the PR branch", "pr-comment"}
    for step in all_steps(wf):
        if {"GH_TOKEN", "GITHUB_TOKEN"} & set(step.get("env") or {}):
            assert step["name"] in allowed


def test_secrets_only_in_env_maps():
    wf = load_workflow("skills-pr.yml")
    for step in all_steps(wf):
        run = step.get("run", "")
        assert not SECRET_REF.search(run), step.get("name")
        assert "github.event" not in run, step.get("name")  # untrusted values go through env
        assert "set -x" not in run
        for value in (step.get("env") or {}).values():
            assert isinstance(value, str)


def test_generation_only_for_skills_that_need_it():
    wf = load_workflow("skills-pr.yml")
    cases = step_named(wf, "2. Generate")
    assert "needs_cases != ''" in cases["if"]
    assert cases["env"]["SKILLS"] == "${{ steps.changed.outputs.needs_cases }}"
    rubric = step_named(wf, "2b.")
    assert "needs_rubric_only != ''" in rubric["if"]
    assert rubric["env"]["SKILLS"] == "${{ steps.changed.outputs.needs_rubric_only }}"
    assert 'generate --rubric-only --skills "$SKILLS"' in rubric["run"]
    jq = step_named(wf, "Find changed")["run"]
    assert ".needs_cases" in jq and "(.needs_cases | not)" in jq  # rubric-only excludes needs_cases


def test_loop_guard_only_pull_request_runs_commit():
    wf = load_workflow("skills-pr.yml")
    commit = step_named(wf, "Commit generated files")
    assert "github.event_name == 'pull_request'" in commit["if"]
    guard = step_named(wf, "Loop guard")
    assert "github.event_name != 'pull_request'" in guard["if"]
    assert "::error::" in guard["run"] and "exit 1" in guard["run"]
    assert "git commit" not in guard["run"]
    steps = list(all_steps(wf))
    assert steps.index(guard) < steps.index(commit)


def test_dispatch_pr_number_validation_and_binding():
    wf = load_workflow("skills-pr.yml")
    resolve = step_named(wf, "Resolve pull request")
    run = resolve["run"]
    assert "^[1-9][0-9]*$" in run
    error_line = next(ln for ln in run.splitlines() if "pull request number must be" in ln)
    assert "PR_NUMBER" not in error_line  # a bad value is never echoed
    assert "refs/heads/" in run and "GITHUB_SHA" in run and "sleep 10" in run
    assert resolve["env"]["EVENT_HEAD_SHA"] == "${{ github.event.pull_request.head.sha }}"


def test_fork_rejected_on_dispatch_before_checkout():
    wf = load_workflow("skills-pr.yml")
    steps = list(all_steps(wf))
    resolve = step_named(wf, "Resolve pull request")
    checkout = next(s for s in steps if s.get("uses", "").startswith("actions/checkout"))
    assert steps.index(resolve) < steps.index(checkout)
    assert "isCrossRepository" in resolve["run"] and "Dispatch is not allowed for fork" in resolve["run"]
    assert checkout["with"]["ref"] == "${{ steps.pr.outputs.head_sha }}"


def test_no_expression_in_any_run_block():
    wf = load_workflow("skills-pr.yml")
    for step in all_steps(wf):
        assert "${{" not in step.get("run", ""), step.get("name")


def test_pinned_actions_carry_version_comments():
    for line in (WORKFLOWS / "skills-pr.yml").read_text().splitlines():
        if "uses:" in line:
            assert re.search(r"@[0-9a-f]{40} # v\d+\.\d+\.\d+$", line), line


def test_final_status_does_not_depend_on_the_comment_step():
    wf = load_workflow("skills-pr.yml")
    status = step_named(wf, "Status")
    assert "COMMENT_OUTCOME" not in status["run"] and "COMMENT_OUTCOME" not in status["env"]


def test_every_run_block_is_valid_bash():
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for step in all_steps(load_workflow(path.name)):
            if "run" in step:
                proc = subprocess.run(["bash", "-n"], input=step["run"], capture_output=True, text=True)
                assert proc.returncode == 0, f"{path.name}: {step.get('name')}: {proc.stderr}"


def test_status_message_on_dispatch_reports_the_loop_guard():
    wf = load_workflow("skills-pr.yml")
    status = step_named(wf, "Status")
    assert status["env"]["EVENT_NAME"] == "${{ github.event_name }}"
    assert "loop guard stopped this run" in status["run"]
    assert status["run"].index('"$EVENT_NAME" = "pull_request"') < status["run"].index("were committed")


def test_lint_runs_before_changed_skills():
    wf = load_workflow("skills-pr.yml")
    steps = list(all_steps(wf))
    assert steps.index(step_named(wf, "1. Lint")) < steps.index(step_named(wf, "Find changed"))


def test_skill_lists_use_dept_slash_name():
    """Every list built from changed-skills rows is dept/skill, never a bare name."""
    run = step_named(load_workflow("skills-pr.yml"), "Find changed")["run"]
    lists = [ln for ln in run.splitlines() if ln.strip().startswith(("names=", "cases=", "rubric="))]
    assert len(lists) == 3
    for ln in lists:
        assert '"\\(.dept)/\\(.skill)"' in ln, ln
        assert "| .skill]" not in ln


# --- skills-publish.yml and skills-switch.yml (Task 7) ---

RELEASE_WORKFLOWS = ("skills-publish.yml", "skills-switch.yml")
GATE_IF = (
    "${{ !cancelled() && needs.plan.result == 'success' && needs.plan.outputs.has_changes == 'true' "
    "&& (needs.eval-merge.result == 'success' || needs.plan.outputs.publish == '[]') }}"
)


def run_text(job: dict) -> str:
    return "\n".join(s.get("run", "") for s in job.get("steps", []))


def release_step(prefix: str) -> dict:
    wf = load_workflow("skills-publish.yml")
    return next(s for s in wf["jobs"]["release"]["steps"] if s.get("name", "").startswith(prefix))


def test_publish_triggers_on_main_skills_pushes():
    wf = load_workflow("skills-publish.yml")
    assert wf["on"]["push"]["branches"] == ["main"]
    assert wf["on"]["push"]["paths"] == ["skills/**"]
    assert wf["permissions"] == {"contents": "read"}


def test_release_workflows_share_one_serialised_concurrency_group():
    groups = set()
    for name in RELEASE_WORKFLOWS:
        wf = load_workflow(name)
        assert wf["concurrency"]["cancel-in-progress"] is False
        groups.add(wf["concurrency"]["group"])
    assert len(groups) == 1


def test_needs_graph_and_exact_gate_condition():
    """The verdict gate is an earlier job; release needs both and its `if` is exact."""
    jobs = load_workflow("skills-publish.yml")["jobs"]
    assert jobs["eval-merge"]["needs"] == ["plan"]
    assert jobs["release"]["needs"] == ["plan", "eval-merge"]
    assert jobs["release"]["if"] == GATE_IF
    assert "always()" not in GATE_IF and "always()" not in jobs["release"]["if"]
    gate = next(s for s in jobs["eval-merge"]["steps"] if s.get("name") == "check-verdict")
    assert 'skillhub check-verdict --run-key "$RUN_LABEL" --skills "$SKILLS"' in gate["run"]
    assert gate["env"]["SKILLS"] == "${{ needs.plan.outputs.reeval }}"


def test_publish_requires_an_evaluated_folder_and_json_arrays():
    for prefix in ("Publish", "Switch"):
        run = release_step(prefix)["run"]
        assert '[ -n "$SKILLS_ROOT" ] ||' in run
        assert run.index('[ -n "$SKILLS_ROOT" ]') < run.index("while IFS=")
    for prefix in ("Publish", "Switch", "Retire"):
        run = release_step(prefix)["run"]
        assert """jq -e 'type == "array" and all(""" in run and '<<<"$ROWS"' in run
        assert run.index("type ==") < run.index("while IFS=")
        assert "</dev/null" in run  # run-now inside the loop must not eat the loop's stdin


def test_publish_and_switch_use_the_evaluated_folder_and_expected_hash():
    publish, switch, retire = (release_step(p) for p in ("Publish", "Switch", "Retire"))
    steps = load_workflow("skills-publish.yml")["jobs"]["release"]["steps"]
    assert steps.index(publish) < steps.index(switch) < steps.index(retire)
    for s in (publish, switch):
        assert s["env"]["SKILLS_ROOT"] == "${{ needs.eval-merge.outputs.skills_root }}"
        assert "skills_root: $skills_root" in s["run"]
        assert "eval_run_id: $eval" in s["run"] and "git_sha: $sha" in s["run"]
    assert 'action: "publish"' in publish["run"] and "expect_hash: $r.hash" in publish["run"]
    assert "expect_rubric_hash: $r.rubric_hash" in publish["run"]
    assert "expect_dataset_digest: $r.dataset_digest" in publish["run"]
    assert 'expect_cells: ($r.cells | join(","))' in publish["run"]
    assert (
        "run_key: $eval" in publish["run"]
        and publish["env"]["EVAL_RUN_ID"] == "${{ needs.plan.outputs.run_key }}"
    )
    assert 'action: "switch"' in switch["run"] and 'version: "latest"' in switch["run"]
    assert "expect_hash: $r.hash" in switch["run"]
    assert 'action: "retire"' in retire["run"] and "skills_root" not in retire["run"]
    assert publish["env"]["MERGE_SHA"] == "${{ github.sha }}"
    for s in (publish, switch, retire):
        assert "SUCCESS" in s["run"] and "run_page_url" in s["run"] and "--timeout 30m" in s["run"]
        assert "::error::run-now failed" in s["run"]


def test_plan_computes_the_eval_digests_from_the_merge_checkout():
    """Content hash, rubric_hash and dataset_digest of each skill at the merge commit."""
    run = step_named(load_workflow("skills-publish.yml"), "Plan re-evaluation")["run"]
    assert (
        'publish=$(uv run skillhub eval-digests --skills "$reeval" --skills-root skills --fixtures-root fixtures'
        in run
    )
    assert "content_hash(load_bundle(" not in run
    assert '"\\(.dept)/\\(.skill)"' in run
    assert "merge-${HEAD_SHA:0:7}-a${RUN_ATTEMPT}" in run


def test_eval_merge_uploads_once_and_exposes_one_candidate_path():
    jobs = load_workflow("skills-publish.yml")["jobs"]
    evalj = jobs["eval-merge"]
    assert evalj["environment"] == "ci-eval"
    assert evalj["outputs"]["skills_root"] == "${{ steps.upload.outputs.skills_root }}"
    upload = next(s for s in evalj["steps"] if s.get("id") == "upload")
    assert 'root="/Workspace$ws_root/$SKILLHUB_CATALOG/candidates/$RUN_KEY"' in upload["run"]
    assert upload["env"]["SKILLHUB_WORKSPACE_ROOT"] == "${{ vars.SKILLHUB_WORKSPACE_ROOT }}"
    assert upload["env"]["SKILLHUB_CATALOG"] == "${{ vars.SKILLHUB_CATALOG }}"
    assert 'echo "skills_root=$root/skills"' in upload["run"] and "import-dir skills" in upload["run"]
    assert 'databricks workspace import "$root/.uploaded_at"' in upload["run"]  # the upload marker
    assert "168" in (WORKFLOWS / "skills-publish.yml").read_text()  # documents the cleanup horizon
    text = (WORKFLOWS / "skills-publish.yml").read_text()
    assert text.count("/candidates/$") == 1 and "/Shared/skill-hub" not in text
    step = step_named(load_workflow("skills-publish.yml"), "Start skill-eval Job and wait")
    run = step["run"]
    assert 'trigger_source: "merge"' in run and "skills_root: $skills_root" in run
    assert "-t prod" not in run and "--timeout 300m" in run and "run_page_url" in run
    assert step["name"].endswith("(5 h)") and evalj["timeout-minutes"] == 330


def test_release_workflows_security_hygiene():
    for name in RELEASE_WORKFLOWS:
        wf = load_workflow(name)
        text = (WORKFLOWS / name).read_text()
        assert "permissions" in wf and "pull_request_target" not in wf["on"]
        for env in [wf.get("env") or {}] + [j.get("env") or {} for j in wf["jobs"].values()]:
            assert not {"GH_TOKEN", "GITHUB_TOKEN"} & set(env)
        for step in all_steps(wf):
            assert "${{" not in step.get("run", ""), (name, step.get("name"))
            assert not SECRET_REF.search(step.get("run", ""))
            if step.get("uses", "").startswith("actions/checkout"):
                assert step["with"]["fetch-depth"] == 0
                assert step["with"]["persist-credentials"] is False
            if "uses" in step:
                assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", step["uses"]), step["uses"]
        for line in text.splitlines():
            if "uses:" in line:
                assert re.search(r"@[0-9a-f]{40} # v\d+\.\d+\.\d+$", line), line
        assert "always()" not in text


def test_production_secrets_only_in_environment_jobs():
    for name in RELEASE_WORKFLOWS:
        for job_name, job in load_workflow(name)["jobs"].items():
            uses_secrets = any("secrets.DATABRICKS" in str(s.get("env", {})) for s in job.get("steps", []))
            if uses_secrets:
                assert job.get("environment") in {"production", "ci-eval"}, (name, job_name)


def test_switch_workflow_is_explicit_version_only():
    wf = load_workflow("skills-switch.yml")
    assert set(wf["on"]["workflow_dispatch"]["inputs"]) == {"skill", "version"}
    job = wf["jobs"]["switch"]
    assert job["environment"] == "production"
    assert job["steps"][0]["name"] == "Validate inputs"  # before checkout or any other use
    runs = run_text(job)
    assert "latest" not in runs and "retire" not in runs
    call = step_named(wf, "Run skill-switch Job")["run"]
    assert 'action: "switch"' in call and "expect_hash" not in call  # manual: no merge hash exists
    assert job["env"] == {"SKILL_REF": "${{ inputs.skill }}", "VERSION": "${{ inputs.version }}"}


@pytest.mark.parametrize(
    ("skill", "version", "ok"),
    [
        ("common/audit-xls", "3", True),
        ("equity_research/morning-note", "123456", True),
        ("common/audit-xls\n", "3", False),
        ("common/audit-xls", "3\n", False),
        ("common/audit-xls;id", "3", False),
        ("common/audit-xls", "3;id", False),
        ("../common/audit-xls", "3", False),
        ("common/../x", "3", False),
        ("a/b/c", "3", False),
        ("common", "3", False),
        ("common/audit-xls", "01", False),
        ("common/audit-xls", "0", False),
        ("common/audit-xls", "latest", False),
        ("common/audit-xls", "", False),
        ("Common/audit-xls", "3", False),
    ],
)
def test_switch_validation_step_behaviour(skill, version, ok):
    script = load_workflow("skills-switch.yml")["jobs"]["switch"]["steps"][0]["run"]
    env = {"PATH": "/usr/bin:/bin", "SKILL_REF": skill, "VERSION": version}
    proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
    assert (proc.returncode == 0) is ok
    if not ok:
        assert skill.strip() not in proc.stdout + proc.stderr or skill.strip() == ""  # no echo-back


def test_plan_diffs_from_the_released_tag_with_an_ancestry_guard():
    wf = load_workflow("skills-publish.yml")
    checkout = wf["jobs"]["plan"]["steps"][0]
    assert checkout["with"]["fetch-depth"] == 0 and checkout["with"]["fetch-tags"] is True
    run = step_named(wf, "Plan re-evaluation")["run"]
    assert 'git merge-base --is-ancestor refs/tags/skillhub/released "$HEAD_SHA"' in run
    assert "::error::" in run and "EVENT_BEFORE_SHA" not in run  # the previous push is never the base
    assert 'changed-skills "${base[@]}"' in run and "base=(--empty-base)" in run
    assert run.index("is-ancestor") < run.index("changed-skills")


def _uv_running_the_cli(tmp_path: Path) -> str:
    """A `uv` whose `uv run skillhub ...` and `uv run python ...` run this checkout's code. Returns its folder."""
    import sys

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    uv = bin_dir / "uv"
    uv.write_text(
        "#!/bin/bash\n"
        '[ "$1" = run ] || exit 9\n'
        "shift\n"
        f'if [ "$1" = skillhub ]; then shift; exec "{sys.executable}" -c '
        '"from skillhub.cli import app; app()" "$@"; fi\n'
        f'if [ "$1" = python ]; then shift; exec "{sys.executable}" "$@"; fi\n'
        "exit 9\n"
    )
    uv.chmod(0o755)
    return str(bin_dir)


def _plan(repo: Path, tmp_path: Path) -> dict[str, str]:
    """Run the plan step on `repo` at HEAD and return its outputs."""
    env = {"PATH": f"{_uv_running_the_cli(tmp_path)}:/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    env |= {"RUNNER_TEMP": str(tmp_path), "GITHUB_OUTPUT": str(tmp_path / "out"), "RUN_ATTEMPT": "1"}
    env |= {"HEAD_SHA": _git(repo, "rev-parse", "HEAD"), "HOME": str(tmp_path)}
    run = step_named(load_workflow("skills-publish.yml"), "Plan re-evaluation")["run"]
    proc = subprocess.run(["bash", "-e", "-c", run], cwd=repo, env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return dict(ln.split("=", 1) for ln in (tmp_path / "out").read_text().splitlines())


def _git(repo: Path, *args: str) -> str:
    who = ("-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false")
    proc = subprocess.run(["git", "-C", str(repo), *who, *args], check=True, capture_output=True, text=True)
    return proc.stdout.strip()


def _add_skill(repo: Path, name: str) -> str:
    """Commit skills/common/<name> with one trigger case. Returns the commit."""
    root = repo / "skills" / "common" / name
    (root / "evals").mkdir(parents=True)
    (root / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Does {name}.\n---\nUse it.\n")
    evals = {"skill_name": name, "evals": [{"id": 1, "prompt": "do it", "should_trigger": True}]}
    (root / "evals" / "evals.json").write_text(json.dumps(evals))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"add {name}")
    return _git(repo, "rev-parse", "HEAD")


def test_plan_publishes_every_unreleased_skill_even_before_the_first_release(tmp_path):
    """Without the skillhub/released tag nothing was released yet. A first release that failed, was rejected or was
    replaced must not lose its skills to the next push: the plan diffs from the released state, never from the
    previous push alone."""
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / "config" / "hub.yaml").write_text((ROOT / "config" / "hub.yaml").read_text())
    _git(repo, "init", "-q", "-b", "main")
    first = _add_skill(repo, "alpha")  # its release failed, so no tag exists
    _add_skill(repo, "beta")
    out = _plan(repo, tmp_path)
    assert out["reeval"] == "common/alpha,common/beta"
    assert [r["skill"] for r in json.loads(out["publish"])] == ["alpha", "beta"]
    assert out["retire"] == "[]" and out["has_changes"] == "true"
    _git(repo, "tag", "skillhub/released", first)  # alpha was released: only beta is left
    (tmp_path / "out").unlink()
    out = _plan(repo, tmp_path)
    assert out["reeval"] == "common/beta" and out["has_changes"] == "true"


def test_release_guard_is_first_and_tag_move_is_last_with_lease():
    wf = load_workflow("skills-publish.yml")
    release = wf["jobs"]["release"]
    assert release["permissions"] == {"contents": "write"}
    for name, job in wf["jobs"].items():
        if name != "release":
            assert "permissions" not in job
    steps = release["steps"]
    assert steps[0]["uses"].startswith("actions/checkout") and steps[0]["with"]["fetch-tags"] is True
    guard = steps[1]
    assert guard["id"] == "guard"
    assert 'git merge-base --is-ancestor refs/tags/skillhub/released "$HEAD_SHA"' in guard["run"]
    assert "::error::" in guard["run"] and "released_before=" in guard["run"]
    tag = steps[-1]
    assert tag["name"] == "Move the skillhub/released tag" and "if" not in tag
    assert tag["env"]["GH_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"
    assert tag["env"]["RELEASED_BEFORE"] == "${{ steps.guard.outputs.released_before }}"
    assert "credential.helper=!gh auth git-credential' push" in tag["run"]
    assert '--force-with-lease="refs/tags/skillhub/released:${RELEASED_BEFORE}"' in tag["run"]
    assert "+${MERGE_SHA}" not in tag["run"]


def test_tokens_only_in_the_tag_move_step():
    for name in RELEASE_WORKFLOWS:
        for step in all_steps(load_workflow(name)):
            if {"GH_TOKEN", "GITHUB_TOKEN"} & set(step.get("env") or {}):
                assert (name, step["name"]) == ("skills-publish.yml", "Move the skillhub/released tag")


def test_skill_switch_job_passes_skills_root_and_expect_hash():
    jobs = yaml.safe_load((ROOT / "resources" / "jobs.yml").read_text())["resources"]["jobs"]
    job = jobs["skill_switch"]
    assert {"name": "skills_root", "default": ""} in job["parameters"]
    assert {"name": "expect_hash", "default": ""} in job["parameters"]
    params = job["tasks"][0]["python_wheel_task"]["parameters"]
    assert params[params.index("--skills-root") + 1] == "{{job.parameters.skills_root}}"
    assert params[params.index("--expect-hash") + 1] == "{{job.parameters.expect_hash}}"
    assert {"name": "run_key", "default": ""} in job["parameters"]
    assert params[params.index("--run-key") + 1] == "{{job.parameters.run_key}}"
    for name in ("expect_rubric_hash", "expect_dataset_digest", "expect_cells"):
        assert {"name": name, "default": ""} in job["parameters"]
        flag = "--" + name.replace("_", "-")
        assert params[params.index(flag) + 1] == "{{job.parameters." + name + "}}"


HASH = "sha256:" + "a" * 64
INPUTS = (
    f'"rubric_hash":"{HASH}","dataset_digest":"{HASH}","cells":["gpt/trigger","claude-sonnet/with_skill"]'
)
NO_CELLS = f'"rubric_hash":"none","dataset_digest":"{HASH}","cells":[]'  # NO_CELLS[:-10] drops the cells


def _step_script(workflow: str, prefix: str) -> str:
    wf = load_workflow(workflow)
    return step_named(wf, prefix)["run"]


def _run_job_function(script: str) -> str:
    match = re.search(r"^ *run_job\(\) \{\n.*?\n *\}$", script, re.S | re.M)
    assert match, "run_job function not found"
    return match.group(0)


@pytest.mark.parametrize(
    ("workflow", "prefix"),
    [
        ("skills-publish.yml", "Start skill-eval Job"),
        ("skills-publish.yml", "Publish"),
        ("skills-publish.yml", "Switch"),
        ("skills-publish.yml", "Retire"),
        ("skills-switch.yml", "Run skill-switch Job"),
    ],
)
@pytest.mark.parametrize("body", ["", '{"run_page_url": "https://ws.example/jobs/1/runs/9"}'])
def test_failed_run_now_prints_a_non_empty_url(tmp_path, workflow, prefix, body):
    fn = _run_job_function(_step_script(workflow, prefix))
    script = f"databricks() {{ printf '%s' '{body}'; return 1; }}\n{fn}\nrun_job '{{}}'"
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "RUNNER_TEMP": str(tmp_path)}
    env |= {"DATABRICKS_HOST": "https://ws.example", "JOB_ID": "42"}
    proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
    assert proc.returncode == 1
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("::error::run-now failed."))
    expected = "https://ws.example/jobs/1/runs/9" if body else "Job: https://ws.example/jobs/42"
    assert line.endswith(expected)


# Behaves like `databricks jobs run-now` in CLI v1.20: a positional JOB_ID next to --json is an error.
# Saves the request body to $SAVED.
_RUN_NOW_CLI = r"""
databricks() {
  [ "$1 $2" = "jobs run-now" ] || return 0
  shift 2
  local body="" pos=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --json | --timeout | -o) [ "$1" = --json ] && body=$2; shift 2 ;;
      -*) shift ;;
      *) pos=$1; shift ;;
    esac
  done
  if [ -n "$body" ] && [ -n "$pos" ]; then
    echo "Error: when --json flag is specified, no positional arguments are allowed." >&2
    return 1
  fi
  printf '%s' "$body" > "$SAVED"
  echo '{"state": {"result_state": "SUCCESS"}}'
}
uv() { echo pr12_ab12cd3; }
"""


@pytest.mark.parametrize(
    ("workflow", "prefix"),
    [
        ("skills-publish.yml", "Start skill-eval Job"),
        ("skills-publish.yml", "Publish"),
        ("skills-publish.yml", "Switch"),
        ("skills-publish.yml", "Retire"),
        ("skills-switch.yml", "Run skill-switch Job"),
    ],
)
def test_run_job_sends_the_job_id_in_the_request_body(tmp_path, workflow, prefix):
    fn = _run_job_function(_step_script(workflow, prefix))
    script = f"""{_RUN_NOW_CLI}\n{fn}\nrun_job '{{"job_parameters": {{"k": "v"}}}}'"""
    saved = tmp_path / "body.json"
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "RUNNER_TEMP": str(tmp_path)}
    env |= {"DATABRICKS_HOST": "https://ws.example", "JOB_ID": "42", "SAVED": str(saved)}
    proc = subprocess.run(["bash", "-e", "-c", script], env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(saved.read_text()) == {"job_id": 42, "job_parameters": {"k": "v"}}


def test_pr_eval_sends_the_job_id_in_the_request_body(tmp_path):
    (tmp_path / "skills").mkdir()
    saved = tmp_path / "body.json"
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "RUNNER_TEMP": str(tmp_path)}
    env |= {"SAVED": str(saved), "GITHUB_OUTPUT": str(tmp_path / "out")}
    env |= {
        "SKILLHUB_JOB_ID_EVAL": "42",
        "RUN_LABEL": "pr12-ab12cd3",
        "SKILLS": "common/a",
        "HEAD_SHA": "f" * 40,
    }
    env |= {"SKILLHUB_WORKSPACE_ROOT": "/Users/ci-app/skill-hub", "SKILLHUB_CATALOG": "skill_hub"}
    script = _RUN_NOW_CLI + _step_script("skills-pr.yml", "Start skill-eval Job")
    proc = subprocess.run(["bash", "-e", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    body = json.loads(saved.read_text())
    assert body["job_id"] == 42 and body["job_parameters"]["run_key"] == "pr12-ab12cd3"


def _row_check(prefix: str) -> str:
    run = _step_script("skills-publish.yml", prefix)
    return next(ln.strip() for ln in run.splitlines() if ln.strip().startswith("jq -e 'type"))


@pytest.mark.parametrize(
    ("rows", "ok"),
    [
        (f'[{{"dept":"common","skill":"a-b","hash":"{HASH}",{INPUTS}}}]', True),
        (f'[{{"dept":"common","skill":"a-b","hash":"{HASH}",{NO_CELLS}}}]', True),
        (
            f'[{{"dept":"common","skill":"a-b","hash":"{HASH}","rubric_hash":"none","dataset_digest":"{HASH}"}}]',
            False,
        ),
        (
            f'[{{"dept":"common","skill":"a-b","hash":"{HASH}",{NO_CELLS[:-10]}"cells":["gpt/without_skill"]}}]',
            False,
        ),
        (f'[{{"dept":"common","skill":"a-b","hash":"{HASH}",{NO_CELLS[:-10]}"cells":"gpt/trigger"}}]', False),
        ("[]", True),
        (f'[{{"dept":"common","skill":"a-b","hash":"",{INPUTS}}}]', False),
        (f'[{{"dept":"common","skill":"a-b",{INPUTS}}}]', False),
        (f'[{{"dept":"common","skill":"a-b","hash":"sha256:ABC",{INPUTS}}}]', False),
        (f'[{{"dept":"common","skill":"a-b","hash":"{HASH}0",{INPUTS}}}]', False),
        (f'[{{"dept":1,"skill":"a-b","hash":"{HASH}",{INPUTS}}}]', False),
        (f'[{{"dept":"common","skill":"a-b","hash":"{HASH}"}}]', False),  # The eval inputs are required
        (
            f'[{{"dept":"common","skill":"a-b","hash":"{HASH}","rubric_hash":"","dataset_digest":"{HASH}"}}]',
            False,
        ),
        (
            f'[{{"dept":"common","skill":"a-b","hash":"{HASH}","rubric_hash":"none","dataset_digest":"none"}}]',
            False,
        ),
        ('{"dept":"common"}', False),
    ],
)
@pytest.mark.parametrize("prefix", ["Publish", "Switch"])
def test_publish_rows_require_a_well_formed_hash(rows, ok, prefix):
    proc = subprocess.run(
        ["bash", "-c", _row_check(prefix)], env={"PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin", "ROWS": rows},
        capture_output=True, text=True,
    )  # fmt: skip
    assert (proc.returncode == 0) is ok


@pytest.mark.parametrize(
    ("rows", "ok"),
    [
        ('[{"dept":"common","skill":"a-b"}]', True),
        ("[]", True),
        ('[{"dept":"common"}]', False),
        ('[{"dept":null,"skill":"x"}]', False),
        ("{}", False),
    ],
)
def test_retire_rows_need_only_dept_and_skill(rows, ok):
    proc = subprocess.run(
        ["bash", "-c", _row_check("Retire")], env={"PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin", "ROWS": rows},
        capture_output=True, text=True,
    )  # fmt: skip
    assert (proc.returncode == 0) is ok


def test_hash_format_matches_content_hash():
    from skillhub.bundle import content_hash

    assert re.fullmatch(r"sha256:[0-9a-f]{64}", content_hash({"SKILL.md": b"x"}))


def test_concurrency_comment_mentions_replaced_pending_run():
    text = (WORKFLOWS / "skills-publish.yml").read_text()
    assert (
        "replaced by a manual skills-switch.yml dispatch publishes its changes on the next push"
        in text.replace("\n# ", " ")
    )


# --- platform.yml (Task 8) and the ci-eval environment ---

PLATFORM = "platform.yml"
ENVIRONMENTS = {"skills-pr.yml": {"skills-pr": "ci-eval"}, "skills-publish.yml": {"eval-merge": "ci-eval"}}
SAME_REPO = "github.event.pull_request.head.repo.full_name == github.repository"


def test_platform_runs_on_every_pull_request_and_main_push():
    wf = load_workflow(PLATFORM)
    assert "paths" not in (wf["on"]["pull_request"] or {})  # a required check must always start
    assert "paths" not in wf["on"]["push"]
    assert wf["on"]["push"]["branches"] == ["main"]
    assert "pull_request_target" not in wf["on"]
    assert wf["permissions"] == {"contents": "read"}


def test_platform_check_job_is_named_platform_and_holds_no_databricks_secrets():
    jobs = load_workflow(PLATFORM)["jobs"]
    assert jobs["platform"]["name"] == "platform"
    assert "environment" not in jobs["platform"]
    runs = run_text(jobs["platform"])
    assert "uv run pytest -q" in runs and "ruff check" in runs and "scripts/build_app.sh" in runs
    assert "secrets." not in str(jobs["platform"])


def test_platform_cancels_superseded_pull_request_runs_on_the_check_job_only():
    wf = load_workflow(PLATFORM)
    assert "workflow_dispatch" in wf["on"]
    assert "concurrency" not in wf
    group = wf["jobs"]["platform"]["concurrency"]
    assert group["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    for name in ("deploy-dev", "deploy-prod"):
        assert wf["jobs"][name]["concurrency"]["cancel-in-progress"] is False


def test_platform_dev_deploy_is_manual_and_approved_in_production():
    job = load_workflow(PLATFORM)["jobs"]["deploy-dev"]
    assert job["needs"] == ["platform"]
    assert job["environment"] == "production"
    assert job["if"] == "${{ github.event_name == 'workflow_dispatch' }}"
    assert job["concurrency"] == {"group": "skillhub-deploy-dev", "cancel-in-progress": False}
    runs = run_text(job)
    assert "bundle validate -t dev" in runs and "bundle deploy -t dev" in runs
    assert "-t prod" not in runs


def test_platform_prod_deploy_is_main_pushes_in_the_release_group():
    job = load_workflow(PLATFORM)["jobs"]["deploy-prod"]
    assert job["needs"] == ["platform"]
    assert job["environment"] == "production"
    assert job["if"] == "${{ github.event_name == 'push' }}"
    assert job["concurrency"] == {"group": "skillhub-release", "cancel-in-progress": False}
    runs = run_text(job)
    assert "bundle validate -t prod" in runs
    assert "bundle deploy -t prod --auto-approve" in runs
    assert "--fail-on-active-runs" not in runs  # Running evals must not block a deploy
    assert "-t dev" not in runs


def test_no_ci_eval_job_runs_databricks_bundle():
    """The ci_eval principal cannot resolve the prod bundle, so its jobs read Job IDs from variables."""
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for job_name, job in load_workflow(path.name)["jobs"].items():
            if job.get("environment") == "ci-eval":
                assert "databricks bundle" not in run_text(job), (path.name, job_name)
                assert "BUNDLE_VAR" not in str(job), (path.name, job_name)


def test_bundle_deploy_only_in_platform_deploy_jobs():
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for job_name, job in load_workflow(path.name)["jobs"].items():
            has_deploy = "bundle deploy" in run_text(job)
            assert has_deploy == (path.name == PLATFORM and job_name.startswith("deploy-")), (
                path.name,
                job_name,
            )


def test_no_workflow_runs_bundle_summary_and_job_ids_come_from_validated_variables():
    for path in sorted(WORKFLOWS.glob("*.yml")):
        assert "bundle summary" not in path.read_text(), path.name
    pr = step_named(load_workflow("skills-pr.yml"), "Start skill-eval Job")
    assert pr["env"]["SKILLHUB_JOB_ID_EVAL"] == "${{ vars.SKILLHUB_JOB_ID_EVAL }}"
    assert '[[ "$SKILLHUB_JOB_ID_EVAL" =~ ^[0-9]+$ ]]' in pr["run"] and "::error::" in pr["run"]
    expected = {
        ("skills-publish.yml", "eval-merge"): "EVAL",
        ("skills-publish.yml", "release"): "SWITCH",
        ("skills-switch.yml", "switch"): "SWITCH",
    }
    for (name, job_name), kind in expected.items():
        steps = [s for s in load_workflow(name)["jobs"][job_name]["steps"] if "JOB_ID" in s.get("env", {})]
        assert steps, (name, job_name)
        for s in steps:
            assert s["env"]["JOB_ID"] == "${{ vars.SKILLHUB_JOB_ID_" + kind + " }}"
            assert '[[ "$JOB_ID" =~ ^[0-9]+$ ]]' in s["run"] and "::error::" in s["run"]
            assert s["run"].index("=~ ^[0-9]+$") < s["run"].index("run_job ")


def test_platform_secrets_only_in_environment_deploy_steps():
    wf = load_workflow(PLATFORM)
    for job_name, job in wf["jobs"].items():
        for step in job["steps"]:
            if "secrets." in str(step.get("env", {})):
                assert job.get("environment") == "production", (job_name, step.get("name"))
                assert "databricks bundle" in step["run"]
            assert not SECRET_REF.search(step.get("run", ""))
            assert "${{" not in step.get("run", "")
        assert job["permissions"] == {"contents": "read"}
        for env in [job.get("env") or {}]:
            assert not {"GH_TOKEN", "GITHUB_TOKEN"} & set(env)


def test_platform_actions_are_pinned_and_checkout_is_hardened():
    path = WORKFLOWS / PLATFORM
    for line in path.read_text().splitlines():
        if "uses:" in line:
            assert re.search(r"@[0-9a-f]{40} # v\d+\.\d+\.\d+$", line), line
    for step in all_steps(load_workflow(PLATFORM)):
        if step.get("uses", "").startswith("actions/checkout"):
            assert step["with"]["persist-credentials"] is False and step["with"]["fetch-depth"] == 0
    assert "always()" not in path.read_text()


def test_every_bundle_step_passes_all_three_principals():
    """The bundle declares three principal variables without defaults, so every bundle command needs them."""
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for step in all_steps(load_workflow(path.name)):
            if "databricks bundle" in step.get("run", ""):
                env = step["env"]
                assert env["BUNDLE_VAR_ci_principal"] == "${{ vars.CI_PRINCIPAL }}", (path.name, step["name"])
                assert env["BUNDLE_VAR_ci_eval_principal"] == "${{ vars.CI_EVAL_PRINCIPAL }}"
                app = (
                    "APP_PRINCIPAL_DEV" if "-t dev" in step["run"] else "APP_PRINCIPAL"
                )  # separate app per target
                assert env["BUNDLE_VAR_app_principal"] == "${{ vars." + app + " }}"


def bundle_steps():
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for step in all_steps(load_workflow(path.name)):
            if "databricks bundle" in step.get("run", ""):
                yield path.name, step


def test_every_bundle_step_passes_the_catalog_ci_reads():
    """The bundle default catalog must never win over the SKILLHUB_CATALOG that CI reads."""
    steps = list(bundle_steps())
    assert len(steps) == 4
    for name, step in steps:
        assert step["env"]["BUNDLE_VAR_catalog"] == "${{ vars.SKILLHUB_CATALOG }}", (name, step["name"])
        run = step["run"]
        assert run.index('[ -n "$BUNDLE_VAR_catalog" ]') < run.index("databricks bundle"), step["name"]


@pytest.mark.parametrize(("catalog", "ok"), [("", False), ("acme_ai", True)])
def test_bundle_steps_stop_without_a_catalog(tmp_path, catalog, ok):
    for _, step in bundle_steps():
        script = "databricks() { echo CALLED; }\njq() { echo 0; }\n" + step["run"]
        env = {
            "PATH": "/usr/bin:/bin",
            "BUNDLE_VAR_catalog": catalog,
            "BUNDLE_VAR_workspace_root": "/Users/c/h",
        }
        proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
        assert (proc.returncode == 0) is ok, (step["name"], proc.stdout, proc.stderr)
        assert ("CALLED" in proc.stdout) is ok


def test_every_bundle_step_passes_the_workspace_root_the_workflows_upload_to():
    """The Jobs' SKILLHUB_WORKSPACE_ROOT (cleanup, experiments) equals the folder CI uploads candidates to."""
    for name, step in bundle_steps():
        assert step["env"]["BUNDLE_VAR_workspace_root"] == "${{ vars.SKILLHUB_WORKSPACE_ROOT }}", (
            name,
            step["name"],
        )


@pytest.mark.parametrize(("root", "ok"), [("", False), ("/Users/ci-app/skill-hub", True)])
def test_bundle_steps_stop_without_a_workspace_root(root, ok):
    for _, step in bundle_steps():
        script = "databricks() { echo CALLED; }\njq() { echo 0; }\n" + step["run"]
        env = {"PATH": "/usr/bin:/bin", "BUNDLE_VAR_catalog": "acme", "BUNDLE_VAR_workspace_root": root}
        proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
        assert (proc.returncode == 0) is ok, (step["name"], proc.stdout, proc.stderr)
        assert ("CALLED" in proc.stdout) is ok


def test_eval_workflows_use_the_ci_eval_environment_and_release_stays_on_production():
    """The ci_eval principal's secrets live in `ci-eval`; ci's live only in `production`."""
    for name, expected in ENVIRONMENTS.items():
        jobs = load_workflow(name)["jobs"]
        for job_name, environment in expected.items():
            assert jobs[job_name]["environment"] == environment, (name, job_name)
    assert load_workflow("skills-publish.yml")["jobs"]["release"]["environment"] == "production"
    assert load_workflow("skills-switch.yml")["jobs"]["switch"]["environment"] == "production"
    assert "environment" not in load_workflow("skills-publish.yml")["jobs"]["plan"]


def test_no_workflow_uses_always():
    for path in sorted(WORKFLOWS.glob("*.yml")):
        assert "always()" not in path.read_text(), path.name


# ---- Cross-workflow invariants ----------------------------------------------------------
# Already covered above, so not repeated here: no pull_request_target (skills-pr, platform and release
# triggers), skills-pr concurrency and the required check names, the Isaac trailer, SHA pins, no `${{ }}`
# or `always()` in run blocks, `bash -n` on run blocks, no bundle in ci-eval jobs, bundle deploy only in the
# platform deploy jobs, and validated Job ID variables per job.

DATABRICKS_SECRET = re.compile(r"secrets\.DATABRICKS_")


def all_jobs():
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for job_name, job in load_workflow(path.name)["jobs"].items():
            yield path.name, job_name, job


def test_every_workflow_parses_and_has_a_name_and_jobs():
    for path in sorted(WORKFLOWS.glob("*.yml")):
        wf = load_workflow(path.name)
        assert wf["name"] and wf["jobs"] and wf["on"], path.name
        assert "pull_request_target" not in wf["on"], path.name


def test_every_job_holding_databricks_credentials_declares_an_environment():
    """Credentials come from environment secrets only; repository-level secrets would bypass approval."""
    seen = 0
    for name, job_name, job in all_jobs():
        if DATABRICKS_SECRET.search(str(job)):
            seen += 1
            assert job.get("environment") in {"ci-eval", "production"}, (name, job_name)
    assert seen >= 5


def test_every_production_job_is_unreachable_from_pull_request():
    seen = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        wf = load_workflow(path.name)
        triggers = wf["on"] if isinstance(wf["on"], dict) else dict.fromkeys(wf["on"])
        for job_name, job in wf["jobs"].items():
            if job.get("environment") != "production":
                continue
            seen.append((path.name, job_name))
            if "pull_request" in triggers:
                condition = str(job.get("if", ""))
                assert "github.event_name == 'push'" in condition or "workflow_dispatch" in condition, (
                    path.name,
                    job_name,
                )
                assert "pull_request" not in condition.replace("!= 'pull_request'", "")
    assert ("platform.yml", "deploy-prod") in seen and ("skills-publish.yml", "release") in seen


def test_ci_eval_jobs_are_never_reachable_from_a_fork():
    """Forks get no secrets from `pull_request`; the fork guard keeps steps that need them from running."""
    exercised = 0
    for name, job_name, job in all_jobs():
        if job.get("environment") == "ci-eval" and "pull_request" in load_workflow(name)["on"]:
            exercised += 1
            assert name == "skills-pr.yml", (name, job_name)
            for step in job["steps"]:
                if DATABRICKS_SECRET.search(str({"env": step.get("env"), "with": step.get("with")})):
                    guard = str(step.get("if", "")) + str(job.get("if", ""))
                    if "steps.eval.outcome != 'skipped'" in guard:
                        # Reporting steps run only when the guarded eval step ran.
                        eval_step = next(x for x in job["steps"] if x.get("id") == "eval")
                        guard += str(eval_step.get("if", ""))
                    assert "is_fork == 'false'" in guard, (name, job_name, step.get("name"))
    assert exercised >= 1


def test_every_run_now_result_is_checked_for_success_and_uses_a_validated_job_id():
    found = 0
    for name, job_name, job in all_jobs():
        for step in job.get("steps", []):
            run = step.get("run", "")
            if "jobs run-now" not in run:
                continue
            found += 1
            where = (name, job_name, step.get("name"))
            assert "--json" in run and "-o json" in run, where
            assert '.state.result_state == "SUCCESS"' in run, where
            # CLI v1.20 refuses a positional job ID next to --json: the ID goes in the request body.
            assert not re.search(r"jobs run-now +[^-\s]", run), where
            job_id_var = re.search(r'--argjson job_id "\$(\w+)"', run)
            assert job_id_var, where
            var = job_id_var.group(1)
            assert re.fullmatch(r"\$\{\{ vars\.SKILLHUB_JOB_ID_(EVAL|SWITCH) \}\}", step["env"][var]), where
            validation = re.search(r'\[\[ "\$' + var + r'" =~ \^\[0-9\]\+\$ \]\]', run)
            assert validation, where
            # The check precedes the call site: the body built for run-now, or the run_job helper invocation.
            helper = run.find("run_job() {")
            call = run.index("run_job ", helper) if helper >= 0 else job_id_var.start()
            assert validation.start() < call, where
    assert found >= 5  # skills-pr eval, eval-merge, publish, switch, retire (+ skills-switch)


def test_run_now_is_started_only_from_the_expected_jobs():
    """Eval Jobs start from ci-eval jobs; switch Jobs only from production jobs."""
    seen = {"EVAL": 0, "SWITCH": 0}
    for name, job_name, job in all_jobs():
        for step in job.get("steps", []):
            env = str(step.get("env", {}))
            if "SKILLHUB_JOB_ID_EVAL" in env:
                seen["EVAL"] += 1
                assert job["environment"] == "ci-eval", (name, job_name)
            if "SKILLHUB_JOB_ID_SWITCH" in env:
                seen["SWITCH"] += 1
                assert job["environment"] == "production", (name, job_name)
    assert (
        seen["EVAL"] >= 2 and seen["SWITCH"] >= 4
    )  # skills-pr, eval-merge; publish, switch, retire, skills-switch


def test_the_release_group_is_shared_and_never_cancels_a_running_release():
    expected = {
        ("platform.yml", "deploy-prod"),
        ("skills-publish.yml", "release"),
        ("skills-switch.yml", "switch"),
    }
    members = set()
    for name, job_name, job in all_jobs():
        wf = load_workflow(name)
        concurrency = job.get("concurrency") or wf.get("concurrency") or {}
        if concurrency.get("group") == "skillhub-release":
            members.add((name, job_name))
            assert concurrency["cancel-in-progress"] is False, (name, job_name)
    assert members >= expected  # skills-publish sets the group on the whole workflow
    for path in sorted(WORKFLOWS.glob("*.yml")):
        assert "cancel-in-progress: true" not in path.read_text() or path.name == "skills-pr.yml"


def test_least_privilege_permissions_on_every_workflow():
    for path in sorted(WORKFLOWS.glob("*.yml")):
        wf = load_workflow(path.name)
        assert "id-token" not in str(wf), path.name
        assert "write-all" not in path.read_text(), path.name
        if "permissions" in wf:
            assert wf["permissions"] == {"contents": "read"}, path.name
        else:
            for job_name, job in wf["jobs"].items():
                assert "permissions" in job, (path.name, job_name)


def test_every_job_has_a_timeout():
    for name, job_name, job in all_jobs():
        assert "timeout-minutes" in job, (name, job_name)


# --- review fixes ---


def _fake_uv(tmp_path: Path) -> dict[str, str]:
    """A `uv` that records the skillhub argv, one per line, each call followed by `--`. `eval-digests` prints
    DIGESTS, so a test can see where its output goes."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    out = tmp_path / "argv"
    uv = bin_dir / "uv"
    uv.write_text(
        "#!/bin/bash\n"
        f'printf "%s\\n" "$@" -- >> "{out}"\n'
        'if [ "$3" = "eval-digests" ]; then echo DIGESTS; fi\n'
    )
    uv.chmod(0o755)
    return {"PATH": f"{bin_dir}:/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin", "OUT": str(out)}


def _argv(env: dict[str, str]) -> list[str]:
    """The argv of the last call."""
    return Path(env["OUT"]).read_text().split("--\n")[-2].splitlines()


def test_pr_check_verdict_requires_every_changed_skill_and_binds_their_digests(tmp_path):
    """A changed skill without a result fails the PR. The rows must be for this checkout's
    content, rubric, cases and fixtures."""
    step = step_named(load_workflow("skills-pr.yml"), "check-verdict")
    assert step["env"]["SKILLS"] == "${{ steps.changed.outputs.non_deleted }}"
    assert step["env"]["SKILLHUB_CI_EVAL_PRINCIPAL"] == "${{ vars.CI_EVAL_PRINCIPAL }}"
    env = _fake_uv(tmp_path) | {
        "SKILLS": "common/a-b,sales/c",
        "RUN_LABEL": "pr1-abc-a1",
        "RUNNER_TEMP": str(tmp_path),
    }
    proc = subprocess.run(["bash", "-e", "-c", step["run"]], env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    calls = Path(env["OUT"]).read_text().split("--\n")
    assert calls[0].splitlines() == [
        "run", "skillhub", "eval-digests", "--skills", "common/a-b,sales/c", "--skills-root", "skills",
        "--fixtures-root", "fixtures",
    ]  # fmt: skip
    assert calls[1].splitlines() == [
        "run", "skillhub", "check-verdict", "--run-key", "pr1-abc-a1", "--skills", "common/a-b,sales/c",
        "--expect-file", f"{tmp_path}/expect.json",
    ]  # fmt: skip
    assert (tmp_path / "expect.json").read_text().strip() == "DIGESTS"


def test_merge_check_verdict_binds_plan_digests(tmp_path):
    jobs = load_workflow("skills-publish.yml")["jobs"]
    gate = next(s for s in jobs["eval-merge"]["steps"] if s.get("name") == "check-verdict")
    assert gate["env"]["EXPECT"] == "${{ needs.plan.outputs.publish }}"
    assert gate["env"]["SKILLHUB_CI_EVAL_PRINCIPAL"] == "${{ vars.CI_EVAL_PRINCIPAL }}"
    rows = f'[{{"dept":"common","skill":"a-b","hash":"{HASH}",{INPUTS}}},{{"dept":"sales","skill":"c","hash":"{HASH}",{INPUTS}}}]'
    env = _fake_uv(tmp_path) | {"SKILLS": "common/a-b,sales/c", "RUN_LABEL": "merge-abc-a1", "EXPECT": rows}
    env["RUNNER_TEMP"] = str(tmp_path)
    proc = subprocess.run(["bash", "-c", gate["run"]], env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert _argv(env)[-2:] == ["--expect-file", f"{tmp_path}/expect.json"]
    assert (tmp_path / "expect.json").read_text() == rows
    env["EXPECT"] = f'[{{"dept":"common","skill":"a-b","hash":"{HASH}"}}]'  # no eval inputs: refused
    Path(env["OUT"]).unlink()
    proc = subprocess.run(["bash", "-c", gate["run"]], env=env, capture_output=True, text=True)
    assert proc.returncode == 1 and "::error::" in proc.stdout and not Path(env["OUT"]).exists()


def test_pr_comment_links_are_opt_in():
    """The comment gets host-bearing links only through the SKILLHUB_PR_LINKS variable."""
    comment = next(s for s in all_steps(load_workflow("skills-pr.yml")) if s.get("id") == "pr-comment")
    assert comment["env"]["SKILLHUB_PR_LINKS"] == "${{ vars.SKILLHUB_PR_LINKS }}"
    assert comment["env"]["SKILLHUB_CI_EVAL_PRINCIPAL"] == "${{ vars.CI_EVAL_PRINCIPAL }}"


QUARANTINE_ERROR = "::error::Moving a skill out of a quarantined department needs an operator: see docs."


@pytest.mark.parametrize(
    ("workflow", "prefix"), [("skills-pr.yml", "Find changed"), ("skills-publish.yml", "Plan re-evaluation")]
)
@pytest.mark.parametrize(("moved", "ok"), [(True, False), (False, True)])
def test_a_move_out_of_quarantine_stops_the_pr_and_the_merge_flow(tmp_path, workflow, prefix, moved, ok):
    """Changed-skills marks the move; skills-pr and skills-publish fail on it before any eval or publish."""
    script = step_named(load_workflow(workflow), prefix)["run"]
    end = script.index("fi\n", script.index("moved_from_quarantine")) + 3
    rows = [{"dept": "common", "skill": "vendor", "status": "renamed", "moved_from_quarantine": moved}]
    (tmp_path / "changed.json").write_text(json.dumps(rows))
    check = script[script.index("# Never promote content") : end]
    env = {"PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin", "RUNNER_TEMP": str(tmp_path)}
    proc = subprocess.run(["bash", "-c", check], env=env, capture_output=True, text=True)
    assert (proc.returncode == 0) is ok
    assert (QUARANTINE_ERROR in proc.stdout) is moved
    assert "vendor" not in proc.stdout  # names come from the PR; they are never echoed into a command
    assert script.index("changed-skills") < script.index("# Never promote content")


def test_pr_status_reports_a_failed_change_scan():
    status = step_named(load_workflow("skills-pr.yml"), "Status")
    assert status["env"]["CHANGED_OUTCOME"] == "${{ steps.changed.outcome }}"
    assert status["run"].index("CHANGED_OUTCOME") < status["run"].index("Skill evaluation passed")


def _deploy_prod_script() -> str:
    return step_named(load_workflow(PLATFORM), "Deploy bundle to prod")["run"]


@pytest.mark.parametrize(
    ("job_id", "listing", "ok", "deployed"),
    [
        ("", "[]", True, True),  # first deploy: the Job does not exist yet
        ("42", "[]", True, True),
        ("42", '{"runs": []}', True, True),
        ("42", "{}", True, True),
        ("42", '[{"run_id": 7}]', False, False),
        ("42", '{"runs": [{"run_id": 7}]}', False, False),
        ("4x2", "[]", False, False),
    ],
)
def test_deploy_prod_waits_only_for_the_skill_switch_job(tmp_path, job_id, listing, ok, deployed):
    """An active skill-switch run blocks the deploy; nothing else is checked."""
    step = step_named(load_workflow(PLATFORM), "Deploy bundle to prod")
    assert step["env"]["SWITCH_JOB_ID"] == "${{ vars.SKILLHUB_JOB_ID_SWITCH }}"
    log = tmp_path / "calls"
    stub = (
        f'databricks() {{ echo "$*" >> {log}; '
        f"if [ \"$1 $2\" = \"jobs list-runs\" ]; then printf '%s' '{listing}'; fi; }}\n"
    )
    env = {
        "PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin",
        "BUNDLE_VAR_catalog": "c",
        "BUNDLE_VAR_workspace_root": "/Users/ci-app/skill-hub",
        "SWITCH_JOB_ID": job_id,
    }
    proc = subprocess.run(
        ["bash", "-c", stub + _deploy_prod_script()], env=env, capture_output=True, text=True
    )
    assert (proc.returncode == 0) is ok, proc.stdout + proc.stderr
    calls = log.read_text().splitlines() if log.exists() else []
    assert ("bundle deploy -t prod --auto-approve" in calls) is deployed
    if job_id == "42":
        assert calls[0] == "jobs list-runs --job-id 42 --active-only -o json"


def test_every_bundle_step_passes_the_alert_destination():
    """Bootstrap creates the SQL alerts with the destination CI holds."""
    for name, step in bundle_steps():
        assert (
            step["env"]["BUNDLE_VAR_alert_destination_id"] == "${{ vars.SKILLHUB_ALERT_DESTINATION_ID }}"
        ), (
            name,
            step["name"],
        )


DEST = "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"


@pytest.mark.parametrize(
    ("dest", "ok", "written"), [("", True, False), (DEST, True, True), ("x; rm", False, False)]
)
def test_prod_deploy_writes_job_failure_notifications_only_for_a_destination(tmp_path, dest, ok, written):
    job = load_workflow(PLATFORM)["jobs"]["deploy-prod"]
    steps = job["steps"]
    step = step_named(load_workflow(PLATFORM), "Configure Job failure notifications")
    assert steps.index(step) < steps.index(step_named(load_workflow(PLATFORM), "Validate bundle (prod)"))
    assert step["env"] == {"DEST": "${{ vars.SKILLHUB_ALERT_DESTINATION_ID }}"}  # no secrets in this step
    env = {"PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin", "DEST": dest}
    proc = subprocess.run(["bash", "-c", step["run"]], env=env, capture_output=True, text=True, cwd=tmp_path)
    assert (proc.returncode == 0) is ok, proc.stdout + proc.stderr
    overrides = tmp_path / ".databricks" / "bundle" / "prod" / "variable-overrides.json"
    assert overrides.exists() is written
    if written:
        assert json.loads(overrides.read_text()) == {
            "job_failure_notifications": {"on_failure": [{"id": DEST}]}
        }
    if not dest:
        assert "::warning::" in proc.stdout


# Records every databricks call. `workspace import --file F` also records F's content; run-now succeeds.
_DATABRICKS_STUB = r"""
databricks() {
  printf 'call %s\n' "$*" >> "$LOG"
  if [ "$1" = workspace ] && [ "$2" = import ]; then
    while [ $# -gt 0 ]; do
      if [ "$1" = --file ]; then printf 'content %s\n' "$(cat "$2")" >> "$LOG"; fi
      shift
    done
  fi
  if [ "$1" = jobs ]; then echo '{"state": {"result_state": "SUCCESS"}}'; fi
}
uv() { echo pr12_ab12cd3; }
"""


@pytest.mark.parametrize(
    ("workflow", "prefix"),
    [("skills-pr.yml", "Start skill-eval Job"), ("skills-publish.yml", "Upload candidate skills")],
)
@pytest.mark.parametrize("with_fixtures", [False, True])
def test_candidate_uploads_write_the_marker_that_cleanup_reads(tmp_path, workflow, prefix, with_fixtures):
    """After the uploads, each candidate folder gets `.uploaded_at` (Unix seconds), which cleanup reads
    to date the folder. It is written after every import-dir and before the eval Job starts."""
    import io
    import time
    from types import SimpleNamespace

    from skillhub.registry import stage

    work = tmp_path / "work"
    (work / "skills").mkdir(parents=True)
    if with_fixtures:
        (work / "fixtures").mkdir()
    log = tmp_path / "log"
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "RUNNER_TEMP": str(tmp_path)}
    env |= {"LOG": str(log), "GITHUB_OUTPUT": str(tmp_path / "out"), "RUN_KEY": "pr12_ab12cd3"}
    env |= {
        "SKILLHUB_JOB_ID_EVAL": "42",
        "RUN_LABEL": "pr12-ab12cd3",
        "SKILLS": "common/a",
        "HEAD_SHA": "f" * 40,
        "SKILLHUB_WORKSPACE_ROOT": "/Users/ci-app/skill-hub",
        "SKILLHUB_CATALOG": "skill_hub",
    }
    script = _DATABRICKS_STUB + _step_script(workflow, prefix)
    proc = subprocess.run(["bash", "-e", "-c", script], cwd=work, env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    lines = log.read_text().splitlines()
    with pytest.MonkeyPatch.context() as mp:  # the folder cleanup reads for the same settings
        mp.setenv("SKILLHUB_WORKSPACE_ROOT", "/Users/ci-app/skill-hub")
        folder = f"{candidates_root(make_cfg())}/pr12_ab12cd3"
    assert folder == "/Workspace/Users/ci-app/skill-hub/skill_hub/candidates/pr12_ab12cd3"
    marker = next(i for i, ln in enumerate(lines) if ln.startswith(f"call workspace import {folder}/"))
    assert lines[marker].startswith(f"call workspace import {folder}/{stage.CANDIDATE_MARKER} --file ")
    assert lines[marker].endswith(" --format RAW --overwrite")
    uploads = [i for i, ln in enumerate(lines) if ln.startswith("call workspace import-dir ")]
    assert len(uploads) == (2 if with_fixtures else 1) and max(uploads) < marker
    assert all(not ln.startswith("call jobs") for ln in lines[:marker])  # the Job starts after the marker
    content = lines[marker + 1].removeprefix("content ")
    assert content.isdecimal() and abs(int(content) - time.time()) < 300
    ws = SimpleNamespace(download=lambda path: io.BytesIO(f"{content}\n".encode()))
    assert stage._marker_ms(SimpleNamespace(workspace=ws), folder, time.time() * 1000) == int(content) * 1000


@pytest.mark.parametrize(
    ("workflow", "prefix"),
    [("skills-pr.yml", "Start skill-eval Job"), ("skills-publish.yml", "Upload candidate skills")],
)
@pytest.mark.parametrize(
    ("ws_root", "catalog", "folder"),
    [
        ("/Users/ci-app/skill-hub", "skill_hub", "/Workspace/Users/ci-app/skill-hub/skill_hub/candidates/k1"),
        ("/Workspace/Users/ci-app/hub/", "c", "/Workspace/Users/ci-app/hub/c/candidates/k1"),
        ("", "skill_hub", None),
        ("Users/x", "skill_hub", None),
        ("/Users/../Shared", "skill_hub", None),
        ("/Users/x; rm -rf /", "skill_hub", None),
        ("/Users/x", "", None),
    ],
)
def test_candidate_uploads_go_to_the_workspace_root_or_stop(
    tmp_path, workflow, prefix, ws_root, catalog, folder
):
    """The upload folder is $SKILLHUB_WORKSPACE_ROOT/$SKILLHUB_CATALOG/candidates/<key>; a missing or
    malformed root or catalog stops the step before anything is uploaded."""
    (tmp_path / "skills").mkdir()
    log = tmp_path / "log"
    log.write_text("")
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "RUNNER_TEMP": str(tmp_path)}
    env |= {"LOG": str(log), "GITHUB_OUTPUT": str(tmp_path / "out"), "RUN_KEY": "k1"}
    env |= {"SKILLHUB_JOB_ID_EVAL": "42", "RUN_LABEL": "k1", "SKILLS": "common/a", "HEAD_SHA": "f" * 40}
    env |= {"SKILLHUB_WORKSPACE_ROOT": ws_root, "SKILLHUB_CATALOG": catalog}
    script = _DATABRICKS_STUB.replace("echo pr12_ab12cd3", "echo k1") + _step_script(workflow, prefix)
    proc = subprocess.run(["bash", "-e", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True)
    calls = log.read_text().splitlines()
    if folder is None:
        assert proc.returncode != 0 and "::error::" in proc.stdout
        assert not [ln for ln in calls if ln.startswith("call ")]
    else:
        assert proc.returncode == 0, proc.stderr
        assert f"call workspace import-dir skills {folder}/skills --overwrite" in calls


def test_publish_passes_the_planned_cells_as_one_comma_separated_job_parameter(tmp_path):
    """The Job gets the merge plan's cells; job-switch refuses a publish with a missing cell."""
    run = release_step("Publish")["run"]
    rows = f'[{{"dept":"common","skill":"a-b","hash":"{HASH}",{INPUTS}}}]'
    saved = tmp_path / "params.json"
    stub = (
        'databricks() { while [ $# -gt 0 ]; do if [ "$1" = --json ]; then printf "%s" "$2" > "$SAVED"; fi; '
        """shift; done; echo '{"state": {"result_state": "SUCCESS"}}'; }\n"""
    )
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "RUNNER_TEMP": str(tmp_path),
        "ROWS": rows,
    }
    env |= {"JOB_ID": "42", "SKILLS_ROOT": "/Workspace/x/skills", "MERGE_SHA": "f" * 40, "EVAL_RUN_ID": "k"}
    env |= {"ACTOR": "a", "DATABRICKS_HOST": "https://ws.example", "SAVED": str(saved)}
    proc = subprocess.run(["bash", "-c", stub + run], env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    params = json.loads(saved.read_text())["job_parameters"]
    assert params["expect_cells"] == "gpt/trigger,claude-sonnet/with_skill"
    assert params["expect_rubric_hash"] == HASH and params["expect_dataset_digest"] == HASH


def _switch_task_argv(params: dict[str, str], job_run_id: str) -> list[str]:
    """The argv the skill-switch Job's wheel task gets for these job parameters, as resources/jobs.yml maps them."""
    job = yaml.safe_load((ROOT / "resources" / "jobs.yml").read_text())["resources"]["jobs"]["skill_switch"]
    declared = {p["name"]: p["default"] for p in job["parameters"]}
    assert set(params) <= set(declared), sorted(set(params) - set(declared))  # only declared parameters
    values = declared | params
    argv = []
    for arg in job["tasks"][0]["python_wheel_task"]["parameters"]:
        arg = re.sub(r"\{\{job\.parameters\.(\w+)\}\}", lambda m: values[m.group(1)], arg)
        argv.append(arg.replace("{{job.run_id}}", job_run_id))
    return argv


def test_release_steps_publish_and_switch_what_the_merge_eval_recorded(reg, cfg, monkeypatch, tmp_path):
    """The whole hand-off, which no unit test of one piece covers: the plan's eval digests, the merge eval's rows
    under the plan's run key, the release steps' run-now bodies, the Job's task argv from jobs.yml, and job-switch
    with its publish gate. Then the live skill is the evaluated content."""
    import sys

    from typer.testing import CliRunner

    from skillhub import cli
    from skillhub.naming import make_run_key
    from tests.unit.eval_rows import add_rows, trust_ci_eval

    skill = tmp_path / "candidate" / "skills" / "common" / "alpha"  # the folder eval-merge uploaded
    (skill / "evals").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: alpha\ndescription: Does alpha.\n---\nUse it.\n")
    cases = [{"id": 1, "prompt": "do it", "should_trigger": True}]
    (skill / "evals" / "evals.json").write_text(json.dumps({"skill_name": "alpha", "evals": cases}))
    digests = CliRunner().invoke(
        cli.app,
        ["--config", str(ROOT / "config" / "hub.yaml"), "eval-digests", "--skills", "common/alpha"]
        + ["--skills-root", str(skill.parents[1]), "--fixtures-root", str(ROOT / "fixtures")],
    )
    assert digests.exit_code == 0, digests.output
    [row] = json.loads(digests.stdout)
    key = make_run_key("merge-abc1234-a1")  # the plan's run_key; the eval got the label
    for cell in row["cells"]:  # what the merge eval records: one trusted, passing row per planned cell
        model, mode = cell.split("/")
        add_rows(reg.sql, cfg, "common", "alpha", key, row["hash"], ((mode, "pass"),), model=model,
                 rubric=row["rubric_hash"], dataset=row["dataset_digest"])  # fmt: skip
    trust_ci_eval(monkeypatch)
    monkeypatch.setenv("SKILLHUB_CI_PRINCIPAL", "ci-app")
    monkeypatch.setenv("SKILLHUB_CONFIG", str(ROOT / "config" / "hub.yaml"))
    monkeypatch.setattr(cli, "_job_run_creator", lambda w, run_id: "ci-app")
    monkeypatch.setattr(cli, "_services", lambda c: (None, reg.uc, reg.sql, reg.journal))
    saved = tmp_path / "bodies"
    stub = (
        'databricks() { while [ $# -gt 0 ]; do if [ "$1" = --json ]; then printf "%s\\n" "$2" >> "$SAVED"; fi; '
        """shift; done; echo '{"state": {"result_state": "SUCCESS"}}'; }\n"""
    )
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "RUNNER_TEMP": str(tmp_path)}
    env |= {"ROWS": digests.stdout.strip(), "JOB_ID": "42", "SKILLS_ROOT": str(skill.parents[1])}
    env |= {"MERGE_SHA": "f" * 40, "EVAL_RUN_ID": key, "ACTOR": "a", "SAVED": str(saved)}
    env["DATABRICKS_HOST"] = "https://ws.example"
    for prefix in ("Publish", "Switch"):
        saved.unlink(missing_ok=True)
        proc = subprocess.run(["bash", "-e", "-c", stub + release_step(prefix)["run"]], env=env, text=True)
        assert proc.returncode == 0
        [body] = saved.read_text().splitlines()
        argv = _switch_task_argv(json.loads(body)["job_parameters"], job_run_id="77")
        monkeypatch.setattr(sys, "argv", ["skillhub-job", *argv])
        assert cli.job_main() is None, prefix  # a refused publish or switch raises SystemExit
    state = reg.journal.live_state("common", "alpha")
    assert (state.action, state.to_version) == ("switched", 1)
    published = [r for r in reg.journal.history("common", "alpha") if r.action == "published"]
    assert [(r.content_hash, r.eval_run_id) for r in published] == [(row["hash"], key)]


# --- The runner is configurable: a workspace with an IP access list needs a runner on an allowed network ------

RUNNER_DEFAULT = "${{ vars.SKILLHUB_RUNNER || 'ubuntu-latest' }}"
FORK_SAFE_RUNNER = "${{ (github.event.pull_request.head.repo.fork && 'ubuntu-latest') || vars.SKILLHUB_RUNNER || 'ubuntu-latest' }}"
DATABRICKS_JOBS = {
    "platform.yml": ["deploy-dev", "deploy-prod"],
    "skills-pr.yml": ["skills-pr"],
    "skills-publish.yml": ["eval-merge", "release"],
    "skills-switch.yml": ["switch"],
}


def test_jobs_that_call_databricks_run_on_the_configurable_runner():
    for name, jobs in DATABRICKS_JOBS.items():
        wf = load_workflow(name)
        for job in jobs:
            expected = FORK_SAFE_RUNNER if name == "skills-pr.yml" else RUNNER_DEFAULT
            assert wf["jobs"][job]["runs-on"] == expected, (name, job)


def test_a_fork_pull_request_never_reaches_the_configurable_runner():
    """A self-hosted runner must never run code from a fork: forks always get a GitHub-hosted runner."""
    runs_on = load_workflow("skills-pr.yml")["jobs"]["skills-pr"]["runs-on"]
    assert runs_on.startswith("${{ (github.event.pull_request.head.repo.fork && 'ubuntu-latest') ||")


def test_jobs_without_databricks_stay_on_github_hosted_runners():
    for name, job in (("platform.yml", "platform"), ("skills-publish.yml", "plan")):
        assert load_workflow(name)["jobs"][job]["runs-on"] == "ubuntu-latest", (name, job)


def test_deploys_start_the_app_and_deploy_its_source():
    """A bundle deploy creates the Databricks App but leaves it stopped with no deployment."""
    wf = load_workflow("platform.yml")
    for job, target in (("deploy-prod", "prod"), ("deploy-dev", "dev")):
        runs = "\n".join(str(s.get("run", "")) for s in wf["jobs"][job]["steps"])
        assert runs.index(f"databricks bundle deploy -t {target}") < runs.index(
            f"databricks bundle run skill_hub -t {target}"
        )
