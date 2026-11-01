"""The sample agent's deploy script (examples/agentcore/skill-agent/deploy.sh), run against stand-ins for the AWS CLI
and the AgentCore toolkit that log their arguments. It runs under each bash on the machine, including the bash 3.2
that macOS ships as /bin/bash."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "examples/agentcore/skill-agent/deploy.sh"
SECRET = "dosefake0123456789abcdef"
SECRET_ARN = (
    "arn:aws:secretsmanager:us-east-1:111122223333:secret:skillhub/agents/skillhub_skill_agent-AbCdEf"
)
ROLE_ARN = "arn:aws:iam::111122223333:role/AmazonBedrockAgentCoreSDKRuntime-us-east-1-abc123"
RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/skillhub_skill_agent-XyZ123"

AWS = """#!/bin/sh
printf 'aws %s\\n' "$*" >> "$CALLS"
for a in "$@"; do case "$a" in file://*) cat "${a#file://}" > "$CALLS.secret" ;; esac; done
case "$*" in
  *"describe-secret"*"--query ARN"*) echo "$STUB_SECRET_ARN" ;;
  *"describe-secret"*) exit "$STUB_SECRET_EXISTS_EXIT" ;;
esac
"""
AGENTCORE = """#!/bin/sh
printf 'agentcore %s\\n' "$*" >> "$CALLS"
if [ "$1" = configure ]; then
  printf 'agents:\\n  skillhub_skill_agent:\\n    aws: {execution_role: "%s"}\\n' "$STUB_ROLE_ARN" > .bedrock_agentcore.yaml
  printf '    bedrock_agentcore: {agent_arn: "%s"}\\n' "$STUB_RUNTIME_ARN" >> .bedrock_agentcore.yaml
fi
"""


def _bashes():
    found = {os.path.realpath(b) for b in (shutil.which("bash"), "/bin/bash") if b and Path(b).exists()}
    return sorted(found)


def _deploy(tmp_path, bash, secret_exists=False, **env):
    bin_, run, tmp = tmp_path / "bin", tmp_path / "run", tmp_path / "tmp"
    for d in (bin_, run, tmp):
        d.mkdir()
    # The script reads the toolkit's YAML with python3, which needs PyYAML: this interpreter has it.
    python3 = f'#!/bin/sh\nexec "{sys.executable}" "$@"\n'
    for name, body in (("aws", AWS), ("agentcore", AGENTCORE), ("python3", python3)):
        (bin_ / name).write_text(body)
        (bin_ / name).chmod(0o755)
    shutil.copy(SCRIPT, run / "deploy.sh")
    calls = tmp_path / "calls"
    settings = {
        "PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}", "TMPDIR": str(tmp), "CALLS": str(calls),
        "STUB_SECRET_ARN": SECRET_ARN, "STUB_ROLE_ARN": ROLE_ARN, "STUB_RUNTIME_ARN": RUNTIME_ARN,
        "STUB_SECRET_EXISTS_EXIT": "0" if secret_exists else "254",
        "DATABRICKS_HOST": "https://example.invalid", "DATABRICKS_CLIENT_ID": "0000-client",
    }  # fmt: skip
    settings.update(env)
    settings = {k: v for k, v in settings.items() if v is not None}
    result = subprocess.run([bash, "deploy.sh"], cwd=run, input=SECRET + "\n", env=settings, capture_output=True,
                            text=True, timeout=60)  # fmt: skip
    logged = calls.read_text().splitlines() if calls.exists() else []
    stored = Path(f"{calls}.secret").read_text() if Path(f"{calls}.secret").exists() else None
    return result, logged, stored, list(tmp.iterdir())


@pytest.mark.parametrize("bash", _bashes())
def test_deploy_stores_the_secret_deploys_and_prints_the_runtime_arn(tmp_path, bash):
    result, calls, stored, left = _deploy(tmp_path, bash)
    assert result.returncode == 0, result.stderr
    assert f"Runtime ARN: {RUNTIME_ARN}" in result.stdout
    assert stored == SECRET and left == []  # handed over in a private file, which is removed
    assert not any(SECRET in c for c in calls) and SECRET not in result.stdout + result.stderr
    assert [c.split()[:3] for c in calls] == [
        ["aws", "secretsmanager", "describe-secret"],
        ["aws", "secretsmanager", "create-secret"],
        ["aws", "secretsmanager", "describe-secret"],
        ["agentcore", "configure", "--entrypoint"],
        ["agentcore", "deploy", "--agent"],
        ["aws", "iam", "put-role-policy"],
    ]
    deploy, policy = calls[4], calls[5]
    assert f"--env SKILLHUB_AGENT_SECRET_ARN={SECRET_ARN}" in deploy
    assert (
        "--env DATABRICKS_HOST=https://example.invalid" in deploy
        and "DATABRICKS_CLIENT_ID=0000-client" in deploy
    )
    assert "--role-name AmazonBedrockAgentCoreSDKRuntime-us-east-1-abc123" in policy
    assert '"Action":"secretsmanager:GetSecretValue","Resource":"' + SECRET_ARN + '"' in policy


@pytest.mark.parametrize("bash", _bashes())
def test_an_existing_secret_gets_the_new_value(tmp_path, bash):
    result, calls, stored, _ = _deploy(tmp_path, bash, secret_exists=True)
    assert result.returncode == 0, result.stderr
    assert stored == SECRET
    assert [c.split()[2] for c in calls[:2]] == ["describe-secret", "put-secret-value"]


@pytest.mark.parametrize("bash", _bashes())
@pytest.mark.parametrize(
    ("env", "says"),
    [
        ({"DATABRICKS_HOST": None}, "DATABRICKS_HOST"),
        ({"DATABRICKS_CLIENT_ID": None}, "DATABRICKS_CLIENT_ID"),
        ({"AGENT_NAME": "other_agent"}, "AGENT_NAME must start with skillhub"),
    ],
)
def test_a_missing_setting_or_a_wrong_name_stops_before_any_call(tmp_path, bash, env, says):
    result, calls, _, _ = _deploy(tmp_path, bash, **env)
    assert result.returncode != 0 and says in result.stderr
    assert calls == []
