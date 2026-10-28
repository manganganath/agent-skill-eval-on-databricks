from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.unit.test_bundle_config import ROOT

SCRIPT = ROOT / "scripts" / "configure_github.sh"

STUB = """#!/usr/bin/env bash
echo "ARGS: $*" >> "$STUB_LOG"
if [[ "$1 $2" == "secret set" ]]; then
  env=""; prev=""
  for a in "$@"; do if [[ "$prev" == "--env" ]]; then env="$a"; fi; prev="$a"; done
  cat > "$STUB_SECRET_DIR/${env}__$3"
  exit 0
fi
if [[ "$1 $2" == "secret list" ]]; then
  if [[ " $* " == *" --org "* ]]; then
    if [[ "${STUB_ORG_FAIL:-}" == 1 ]]; then exit 1; fi
    printf '%s\\n' ${STUB_ORG_SECRETS:-}
  else
    printf '%s\\n' ${STUB_REPO_SECRETS:-}
  fi
elif [[ "$1" == "api" && "$2" == users/* ]]; then
  if [[ " $* " == *" .type "* ]]; then echo "${STUB_OWNER_TYPE:-User}"; else echo 4242; fi
elif [[ "$1" == "api" && "$*" == *deployment-branch-policies* && "$*" != *POST* ]]; then
  printf '%s\\n' ${STUB_POLICIES:-}
fi
if [[ " $* " == *" --input - "* ]]; then
  { echo "STDIN-BEGIN"; cat; echo "STDIN-END"; } >> "$STUB_LOG"
fi
exit 0
"""

SECRET_VALUES = {
    "host": "https://secret-host.example",
    "wh": "wh-secret-123",
    "ci_id": "ci-client-id-AAA",
    "ci_secret": "ci-client-secret-BBB",
    "eval_id": "eval-client-id-CCC",
    "eval_secret": "eval-client-secret-DDD",
}


@pytest.fixture
def env(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(STUB)
    gh.chmod(0o755)
    files = {}
    for key, value in SECRET_VALUES.items():
        path = tmp_path / f"{key}.txt"
        path.write_text(value + "\n")
        files[key] = path
    secrets = tmp_path / "secrets-out"
    secrets.mkdir()
    return {"tmp": tmp_path, "bin": bin_dir, "files": files, "log": tmp_path / "log", "secrets": secrets}


def base_args(e, **over) -> list[str]:
    f = e["files"]
    args = {
        "--repo": "o/r",
        "--host-file": f["host"],
        "--warehouse-id-file": f["wh"],
        "--ci-client-id-file": f["ci_id"],
        "--ci-client-secret-file": f["ci_secret"],
        "--ci-eval-client-id-file": f["eval_id"],
        "--ci-eval-client-secret-file": f["eval_secret"],
        "--ci-principal": "ci-principal-1",
        "--ci-eval-principal": "eval-principal-2",
        "--app-principal": "app-1",
        "--app-principal-dev": "app-dev-1",
        "--catalog": "hubcat",
    }
    args.update(over)
    out: list[str] = []
    for k, v in args.items():
        if v is not None:
            out += [k, str(v)]
    return out


def run(e, *extra: str, args: list[str] | None = None, **envvars):
    environment = {
        **os.environ,
        "PATH": f"{e['bin']}:{os.environ['PATH']}",
        "STUB_LOG": str(e["log"]),
        "STUB_SECRET_DIR": str(e["secrets"]),
        **envvars,
    }
    return subprocess.run(
        ["bash", str(SCRIPT), *(args if args is not None else base_args(e)), *extra],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        cwd=e["tmp"],
    )


def stdin_bodies(e) -> list[dict]:
    parts = log_text(e).split("STDIN-BEGIN\n")[1:]
    return [json.loads(part.split("STDIN-END")[0]) for part in parts]


def secret_mapping(e) -> dict[tuple[str, str], str]:
    """(environment, name) -> exact bytes the script piped into `gh secret set`."""
    out = {}
    for f in e["secrets"].iterdir():
        env_name, name = f.name.split("__", 1)
        out[(env_name, name)] = f.read_text()
    return out


def expected_mapping() -> dict[tuple[str, str], str]:
    v = SECRET_VALUES
    return {
        ("production", "DATABRICKS_HOST"): v["host"],
        ("production", "SQL_WAREHOUSE_ID"): v["wh"],
        ("production", "DATABRICKS_CLIENT_ID"): v["ci_id"],
        ("production", "DATABRICKS_CLIENT_SECRET"): v["ci_secret"],
        ("ci-eval", "DATABRICKS_HOST"): v["host"],
        ("ci-eval", "SQL_WAREHOUSE_ID"): v["wh"],
        ("ci-eval", "DATABRICKS_CLIENT_ID"): v["eval_id"],
        ("ci-eval", "DATABRICKS_CLIENT_SECRET"): v["eval_secret"],
    }


def log_text(e) -> str:
    return e["log"].read_text() if e["log"].exists() else ""


def test_script_is_executable_and_hygienic():
    text = SCRIPT.read_text()
    assert os.access(SCRIPT, os.X_OK)
    assert "set -euo pipefail" in text
    assert subprocess.run(["bash", "-n", str(SCRIPT)], check=False).returncode == 0
    assert "auth switch" not in text.replace("`gh auth switch`", "")
    # Every variable expansion on a gh command line is double-quoted.
    for line in text.splitlines():
        if line.lstrip().startswith("#") or "gh " not in line:
            continue
        assert not re.search(r'(?<!["{])\$[A-Za-z_]', line.replace('"$', "").replace("$(", "")), line


def test_requires_repo(env):
    result = run(env, args=[])
    assert result.returncode == 2
    assert "usage" in result.stderr


def test_dry_run_prints_commands_and_makes_no_mutating_call(env):
    result = run(env, "--dry-run")
    assert result.returncode == 0, result.stderr
    out = result.stdout
    for name in ("DATABRICKS_HOST", "DATABRICKS_CLIENT_ID", "DATABRICKS_CLIENT_SECRET", "SQL_WAREHOUSE_ID"):
        assert f"gh secret set {name} --env production --repo o/r" in out
        assert f"gh secret set {name} --env ci-eval --repo o/r" in out
    for name in (
        "CI_PRINCIPAL",
        "CI_EVAL_PRINCIPAL",
        "APP_PRINCIPAL",
        "APP_PRINCIPAL_DEV",
        "SKILLHUB_CATALOG",
    ):
        assert f"variable set {name} --env production --repo o/r" in out
    assert "variable set SKILLHUB_CATALOG --env ci-eval" in out
    assert "environments/production" in out and "environments/ci-eval" in out
    assert "deployment-branch-policies" in out and '"name": "main"' in out
    assert '"custom_branch_policies": true' in out
    assert '"prevent_self_review": false' in out
    assert "branches/main/protection" in out
    assert (
        '"checks": [{"context": "skills-pr", "app_id": 15368}, {"context": "platform", "app_id": 15368}]'
        in out
    )
    assert '"contexts"' not in out  # a name-only check accepts any commit status with that name
    assert '"required_approving_review_count": 0' in out
    assert '"require_code_owner_reviews": false' in out
    assert '"enforce_admins": false' in out
    # Only read-only calls reached gh.
    log = log_text(env)
    assert "secret set" not in log and "variable set" not in log and "-X" not in log
    assert "auth switch" not in log
    # Checklist.
    assert "Service Principal User" in out
    assert "databricks bundle summary -t prod -o json" in out
    # Job-ID variables are skipped when not given.
    assert "SKILLHUB_JOB_ID" not in out.split("Remaining manual steps")[0]


def test_dry_run_leaks_no_secret_value(env):
    result = run(env, "--dry-run", "--job-id-eval", "123", "--job-id-switch", "456")
    assert result.returncode == 0, result.stderr
    combined = result.stdout + result.stderr + log_text(env)
    for value in SECRET_VALUES.values():
        assert value not in combined
    assert "variable set SKILLHUB_JOB_ID_EVAL --env production --repo o/r --body 123" in result.stdout
    assert "variable set SKILLHUB_JOB_ID_EVAL --env ci-eval --repo o/r --body 123" in result.stdout
    assert "variable set SKILLHUB_JOB_ID_SWITCH --env production --repo o/r --body 456" in result.stdout
    assert "SKILLHUB_JOB_ID_SWITCH --env ci-eval" not in result.stdout


def test_real_run_passes_secrets_on_stdin_only(env):
    result = run(env)
    assert result.returncode == 0, result.stderr
    log = log_text(env)
    assert "ARGS: secret set DATABRICKS_CLIENT_SECRET --env production --repo o/r" in log
    for value in SECRET_VALUES.values():
        assert value not in log  # stub logs arguments only; none carries a value
        assert value not in result.stdout + result.stderr
    env_prod, env_eval, protection = stdin_bodies(env)[0], stdin_bodies(env)[2], stdin_bodies(env)[3]
    assert stdin_bodies(env)[1] == {"name": "main", "type": "branch"}
    assert env_prod == {
        "reviewers": [{"type": "User", "id": 4242}],
        "prevent_self_review": False,
        "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
    }
    assert env_eval == {"reviewers": []}
    assert protection == {
        "required_status_checks": {
            "strict": True,
            "checks": [{"context": "skills-pr", "app_id": 15368}, {"context": "platform", "app_id": 15368}],
        },
        "enforce_admins": False,
        "required_pull_request_reviews": {
            "required_approving_review_count": 0,
            "require_code_owner_reviews": False,
            "dismiss_stale_reviews": True,
        },
        "restrictions": None,
        "allow_force_pushes": False,
        "allow_deletions": False,
    }
    assert "ARGS: api -X POST repos/o/r/environments/production/deployment-branch-policies" in log


def test_secrets_reach_the_right_environment(env):
    result = run(env)
    assert result.returncode == 0, result.stderr
    assert secret_mapping(env) == expected_mapping()  # trailing newline stripped too
    combined = result.stdout + result.stderr + log_text(env)
    for value in SECRET_VALUES.values():
        assert value not in combined


def test_swapped_client_files_are_detected_by_the_mapping(env):
    f = env["files"]
    swapped = base_args(
        env,
        **{
            "--ci-client-id-file": f["eval_id"],
            "--ci-client-secret-file": f["eval_secret"],
            "--ci-eval-client-id-file": f["ci_id"],
            "--ci-eval-client-secret-file": f["ci_secret"],
        },
    )
    result = run(env, args=swapped)
    assert result.returncode == 0, result.stderr
    assert secret_mapping(env) != expected_mapping()
    assert secret_mapping(env)[("production", "DATABRICKS_CLIENT_ID")] == SECRET_VALUES["eval_id"]


def test_refuses_identical_client_id_files(env):
    f = env["files"]
    result = run(env, "--dry-run", args=base_args(env, **{"--ci-eval-client-id-file": f["ci_id"]}))
    assert result.returncode == 1
    assert "same client ID" in result.stderr


def test_real_mode_guard_fires_before_any_change(env):
    result = run(env, STUB_REPO_SECRETS="DATABRICKS_HOST")
    assert result.returncode == 1
    log = log_text(env)
    assert "-X" not in log and "secret set" not in log and "variable set" not in log
    assert list(env["secrets"].iterdir()) == []


def test_org_owner_requires_reviewer(env):
    result = run(env, "--dry-run", STUB_OWNER_TYPE="Organization")
    assert result.returncode == 1
    assert "--reviewer" in result.stderr
    ok = run(env, "--dry-run", "--reviewer", "alice", STUB_OWNER_TYPE="Organization")
    assert ok.returncode == 0, ok.stderr


def test_org_secret_list_failure_fails_closed_for_organizations(env):
    args = [*base_args(env), "--reviewer", "alice"]
    result = run(env, "--dry-run", args=args, STUB_OWNER_TYPE="Organization", STUB_ORG_FAIL="1")
    assert result.returncode == 1
    assert "cannot read the organization secrets" in result.stderr
    override = run(
        env, "--dry-run", "--skip-org-check", args=args, STUB_OWNER_TYPE="Organization", STUB_ORG_FAIL="1"
    )
    assert override.returncode == 0, override.stderr


def test_org_secret_message_says_delete(env):
    args = [*base_args(env), "--reviewer", "alice"]
    result = run(
        env, "--dry-run", args=args, STUB_OWNER_TYPE="Organization", STUB_ORG_SECRETS="SQL_WAREHOUSE_ID"
    )
    assert result.returncode == 1
    assert "delete that org secret" in result.stderr


def test_foreign_branch_policy_is_refused(env):
    for policies in ("branch:main branch:dev", "tag:main"):
        result = run(env, STUB_POLICIES=policies)
        assert result.returncode == 1, policies
        assert "other than the branch 'main'" in result.stderr
        assert "-X POST" not in log_text(env)


def test_existing_branch_policy_is_a_noop(env):
    result = run(env, STUB_POLICIES="branch:main")
    assert result.returncode == 0, result.stderr
    assert "-X POST" not in log_text(env)
    assert "already present" in result.stdout


def test_idempotent_rerun_issues_same_calls(env):
    run(env, STUB_POLICIES="branch:main")
    first = log_text(env)
    run(env, STUB_POLICIES="branch:main")
    assert log_text(env) == first + first


@pytest.mark.parametrize("secret", ["DATABRICKS_HOST", "DATABRICKS_CLIENT_SECRET", "SQL_WAREHOUSE_ID"])
def test_refuses_repo_level_secret(env, secret):
    result = run(env, "--dry-run", STUB_REPO_SECRETS=f"OTHER {secret}")
    assert result.returncode == 1
    assert "repository-level" in result.stderr


def test_refuses_org_level_secret(env):
    args = [*base_args(env), "--reviewer", "alice"]
    result = run(
        env, "--dry-run", args=args, STUB_OWNER_TYPE="Organization", STUB_ORG_SECRETS="DATABRICKS_CLIENT_ID"
    )
    assert result.returncode == 1
    assert "organization-level" in result.stderr


def test_unreadable_org_is_tolerated_for_user_owner(env):
    result = run(env, "--dry-run", STUB_ORG_FAIL="1")
    assert result.returncode == 0, result.stderr


def test_refuses_when_ci_eval_client_id_equals_ci_principal(env):
    result = run(env, "--dry-run", args=base_args(env, **{"--ci-principal": SECRET_VALUES["eval_id"]}))
    assert result.returncode == 1
    assert "equals CI_PRINCIPAL" in result.stderr
    assert SECRET_VALUES["eval_id"] not in result.stderr


@pytest.mark.parametrize("flag", ["--job-id-eval", "--job-id-switch"])
@pytest.mark.parametrize("bad", ["abc", "12x", "-1", "1.5"])
def test_refuses_non_numeric_job_id(env, flag, bad):
    result = run(env, "--dry-run", flag, bad)
    assert result.returncode == 1
    assert "numeric" in result.stderr


def test_missing_secret_file_is_rejected_before_any_call(env):
    env["files"]["host"].unlink()
    result = run(env, "--dry-run")
    assert result.returncode == 1
    assert log_text(env) == ""


def test_codeowners_names_owner_without_requiring_code_owner_review():
    text = (ROOT / ".github" / "CODEOWNERS").read_text()
    assert "@manganganath" in text
    assert "skills/**/evals/**" in text


def test_ci_eval_gets_the_principal_the_gates_trust(env):
    result = run(env, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "variable set CI_EVAL_PRINCIPAL --env ci-eval --repo o/r --body eval-principal-2" in result.stdout
    assert "SKILLHUB_PR_LINKS" not in result.stdout  # links stay off unless asked for


@pytest.mark.parametrize("value", ["true", "false"])
def test_pr_links_is_a_repository_variable(env, value):
    result = run(env, "--dry-run", "--pr-links", value)
    assert result.returncode == 0, result.stderr
    assert f"variable set SKILLHUB_PR_LINKS --repo o/r --body {value}" in result.stdout


def test_pr_links_rejects_other_values_before_any_change(env):
    result = run(env, "--pr-links", "yes")
    assert result.returncode == 1 and "--pr-links must be true or false" in result.stderr
    assert "variable set" not in log_text(env) and "secret set" not in log_text(env)


def test_alert_destination_goes_to_production(env):
    dest = "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"
    result = run(env, "--dry-run", "--alert-destination-id", dest)
    assert result.returncode == 0, result.stderr
    assert (
        f"variable set SKILLHUB_ALERT_DESTINATION_ID --env production --repo o/r --body {dest}"
        in result.stdout
    )
    assert "SKILLHUB_ALERT_DESTINATION_ID --env ci-eval" not in result.stdout
    bad = run(env, "--alert-destination-id", "x; rm -rf /")
    assert bad.returncode == 1 and "notification destination ID" in bad.stderr


def test_workspace_root_defaults_to_the_ci_principals_home_in_both_environments(env):
    """Experiments and candidates never live under /Shared, which every user can edit."""
    result = run(env, "--dry-run")
    assert result.returncode == 0, result.stderr
    ci = re.search(
        r"variable set CI_PRINCIPAL --env production --repo o/r --body (\S+)", result.stdout
    ).group(1)
    for environment in ("production", "ci-eval"):
        assert (
            f"variable set SKILLHUB_WORKSPACE_ROOT --env {environment} --repo o/r --body /Users/{ci}/skill-hub"
            in result.stdout
        )


def test_workspace_root_can_be_set_and_is_normalised(env):
    result = run(env, "--dry-run", "--workspace-root", "/Workspace/Users/ops@example.com/hub/")
    assert result.returncode == 0, result.stderr
    assert (
        "SKILLHUB_WORKSPACE_ROOT --env ci-eval --repo o/r --body /Users/ops@example.com/hub" in result.stdout
    )


@pytest.mark.parametrize(
    "bad", ["/Shared/skill-hub", "/Workspace/Shared", "relative/x", "/Users/../x", "/a b"]
)
def test_workspace_root_refuses_shared_and_malformed_paths_before_any_change(env, bad):
    result = run(env, "--workspace-root", bad)
    assert result.returncode == 1 and "--workspace-root" in result.stderr
    assert "variable set" not in log_text(env) and "secret set" not in log_text(env)
