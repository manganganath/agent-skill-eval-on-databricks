"""The sample agent's deploy script (examples/agentcore/skill-agent/deploy.sh), run against stand-ins for the AWS CLI
and the AgentCore CLI that log their arguments, and the settings that `agentcore deploy` receives. It runs under each
bash on the machine, including the bash 3.2 that macOS ships as /bin/bash."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SAMPLE = ROOT / "examples/agentcore/skill-agent"
SECRET = "dosefake0123456789abcdef"
SECRET_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:skillhub/agents/skill_agent-AbCdEf"
RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/skillhub_default_skill_agent-XyZ123"
SUBNETS = "subnet-0fb028b4c75a1ab34,subnet-0912fc6894ce5abd5"
GROUP = "sg-05eb020d57cab08ee"

AWS = """#!/bin/sh
printf 'aws %s\\n' "$*" >> "$CALLS"
for a in "$@"; do case "$a" in file://*) cat "${a#file://}" > "$CALLS.secret" ;; esac; done
case "$*" in
  *"describe-secret"*"--query ARN"*) echo "$STUB_SECRET_ARN" ;;
  *"describe-secret"*) exit "$STUB_SECRET_EXISTS_EXIT" ;;
  *"list-exports"*) echo "$STUB_RUNTIME_ARN" ;;
esac
"""
RECEIVED = ("DATABRICKS_HOST", "DATABRICKS_CLIENT_ID", "SKILLHUB_AGENT_SECRET_ARN", "AGENT_SUBNETS", "AGENT_SECURITY_GROUPS",
            "AWS_TAGS")  # fmt: skip
AGENTCORE = (
    "#!/bin/sh\n"
    'if [ "$1" = --version ]; then echo "$STUB_AGENTCORE_VERSION"; exit "$STUB_AGENTCORE_VERSION_EXIT"; fi\n'
    'printf \'agentcore %s\\n\' "$*" >> "$CALLS"\n'
    + "".join(f'printf \'  {n}=%s\\n\' "${{{n}:-}}" >> "$CALLS"\n' for n in RECEIVED)
    + 'printf \'  targets=%s\\n\' "$(cat agentcore/aws-targets.json)" >> "$CALLS"\n'
)


def _bashes():
    found = {os.path.realpath(b) for b in (shutil.which("bash"), "/bin/bash") if b and Path(b).exists()}
    return sorted(found)


def _deploy(tmp_path, bash, secret_exists=False, targets=None, **env):
    bin_, run, tmp = tmp_path / "bin", tmp_path / "run", tmp_path / "tmp"
    for d in (bin_, run / "agentcore", tmp):
        d.mkdir(parents=True)
    for name, body in (("aws", AWS), ("agentcore", AGENTCORE)):
        (bin_ / name).write_text(body)
        (bin_ / name).chmod(0o755)
    shutil.copy(SAMPLE / "deploy.sh", run / "deploy.sh")
    if targets is not None:
        (run / "agentcore/aws-targets.json").write_text(targets)
    calls = tmp_path / "calls"
    settings = {
        "PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}", "TMPDIR": str(tmp), "CALLS": str(calls),
        "STUB_SECRET_ARN": SECRET_ARN, "STUB_RUNTIME_ARN": RUNTIME_ARN,
        "STUB_SECRET_EXISTS_EXIT": "0" if secret_exists else "254",
        "STUB_AGENTCORE_VERSION": "1.0.0", "STUB_AGENTCORE_VERSION_EXIT": "0",
        "DATABRICKS_HOST": "https://example.invalid", "DATABRICKS_CLIENT_ID": "0000-client",
    }  # fmt: skip
    settings.update(env)
    settings = {k: v for k, v in settings.items() if v is not None}
    # Run from another folder: the script finds its project next to itself.
    result = subprocess.run([bash, str(run / "deploy.sh")], cwd=tmp_path, input=SECRET + "\n", env=settings,
                            capture_output=True, text=True, timeout=60)  # fmt: skip
    logged = calls.read_text().splitlines() if calls.exists() else []
    stored = Path(f"{calls}.secret").read_text() if Path(f"{calls}.secret").exists() else None
    return result, logged, stored, list(tmp.iterdir())


def _deploy_settings(calls):
    """What `agentcore deploy` received: its arguments, and the settings in its environment."""
    at = next(i for i, c in enumerate(calls) if c.startswith("agentcore "))
    lines = calls[at + 1 : at + 8]
    return calls[at], dict(line.strip().split("=", 1) for line in lines)


@pytest.mark.parametrize("bash", _bashes())
def test_deploy_stores_the_secret_deploys_with_the_cli_and_prints_the_runtime_arn(tmp_path, bash):
    result, calls, stored, left = _deploy(tmp_path, bash)
    assert result.returncode == 0, result.stderr
    assert f"Runtime ARN: {RUNTIME_ARN}" in result.stdout
    assert stored == SECRET and left == []  # handed over in a private file, which is removed
    assert not any(SECRET in c for c in calls) and SECRET not in result.stdout + result.stderr
    assert [c.split()[:3] for c in calls if not c.startswith(" ")] == [
        ["aws", "secretsmanager", "describe-secret"],
        ["aws", "secretsmanager", "create-secret"],
        ["aws", "secretsmanager", "describe-secret"],
        ["agentcore", "deploy", "--target"],
        ["aws", "cloudformation", "list-exports"],
    ]
    command, received = _deploy_settings(calls)
    assert command == "agentcore deploy --target default --region us-east-1"
    assert received == {
        "DATABRICKS_HOST": "https://example.invalid",
        "DATABRICKS_CLIENT_ID": "0000-client",
        "SKILLHUB_AGENT_SECRET_ARN": SECRET_ARN,
        "AGENT_SUBNETS": "",
        "AGENT_SECURITY_GROUPS": "",
        "AWS_TAGS": "",
        "targets": "[]",  # made for the CLI, which adds the default target from the AWS sign-in
    }
    lookup = calls[-1]
    assert "AgentCore-skillhub-default-skill-agent-RuntimeArn" in lookup and "--region us-east-1" in lookup


@pytest.mark.parametrize("bash", _bashes())
def test_the_deployment_targets_already_there_are_kept(tmp_path, bash):
    mine = '[{"name": "default", "account": "111122223333", "region": "eu-west-1"}]'
    result, calls, _, _ = _deploy(tmp_path, bash, targets=mine, AWS_REGION="eu-west-1")
    assert result.returncode == 0, result.stderr
    command, received = _deploy_settings(calls)
    assert received["targets"] == mine and command.endswith("--region eu-west-1")


@pytest.mark.parametrize("bash", _bashes())
def test_an_existing_secret_gets_the_new_value_and_the_tags(tmp_path, bash):
    result, calls, stored, _ = _deploy(tmp_path, bash, secret_exists=True)
    assert result.returncode == 0, result.stderr
    assert stored == SECRET
    assert [c.split()[2] for c in calls if c.startswith("aws secretsmanager")][:3] == [
        "describe-secret",
        "put-secret-value",
        "tag-resource",
    ]


@pytest.mark.parametrize("bash", _bashes())
def test_vpc_mode_passes_the_subnets_and_security_groups_to_the_stack(tmp_path, bash):
    result, calls, _, _ = _deploy(tmp_path, bash, AGENT_SUBNETS=SUBNETS, AGENT_SECURITY_GROUPS=GROUP)
    assert result.returncode == 0, result.stderr
    _, received = _deploy_settings(calls)
    assert (received["AGENT_SUBNETS"], received["AGENT_SECURITY_GROUPS"]) == (SUBNETS, GROUP)


@pytest.mark.parametrize("bash", _bashes())
def test_aws_tags_tag_the_secret_and_go_to_the_stack(tmp_path, bash):
    result, calls, _, _ = _deploy(tmp_path, bash, AWS_TAGS="Owner=me@example.com, cost-center=42")
    assert result.returncode == 0, result.stderr
    create = next(c for c in calls if "create-secret" in c)
    want = [{"Key": "project", "Value": "skillhub"}, {"Key": "Owner", "Value": "me@example.com"},
            {"Key": "cost-center", "Value": "42"}]  # fmt: skip
    assert json.dumps(want) in create
    _, received = _deploy_settings(calls)
    assert received["AWS_TAGS"] == "Owner=me@example.com,cost-center=42"


@pytest.mark.parametrize("bash", _bashes())
@pytest.mark.parametrize(
    ("env", "says"),
    [
        ({"DATABRICKS_HOST": None}, "DATABRICKS_HOST"),
        ({"DATABRICKS_CLIENT_ID": None}, "DATABRICKS_CLIENT_ID"),
        ({"AWS_TAGS": "Owner"}, "AWS_TAGS: 'Owner' is not key=value"),
        ({"AGENT_SUBNETS": SUBNETS}, "set both AGENT_SUBNETS and AGENT_SECURITY_GROUPS, or neither"),
        ({"AGENT_SECURITY_GROUPS": GROUP}, "set both AGENT_SUBNETS and AGENT_SECURITY_GROUPS, or neither"),
        (
            {"AGENT_SUBNETS": "subnet-1,vpc-2", "AGENT_SECURITY_GROUPS": GROUP},
            "AGENT_SUBNETS: 'subnet-1' is not",
        ),
        (
            {"AGENT_SUBNETS": SUBNETS, "AGENT_SECURITY_GROUPS": "default"},
            "AGENT_SECURITY_GROUPS: 'default' is not",
        ),
        ({"STUB_AGENTCORE_VERSION": "0.3.14"}, "needs the AgentCore CLI 1.0 or later"),
        (
            {"STUB_AGENTCORE_VERSION": "", "STUB_AGENTCORE_VERSION_EXIT": "2"},
            "needs the AgentCore CLI 1.0 or later",
        ),
    ],
)
def test_a_missing_or_malformed_setting_or_the_old_toolkit_stops_before_any_call(tmp_path, bash, env, says):
    result, calls, _, _ = _deploy(tmp_path, bash, **env)
    assert result.returncode != 0 and says in result.stderr
    assert calls == []
