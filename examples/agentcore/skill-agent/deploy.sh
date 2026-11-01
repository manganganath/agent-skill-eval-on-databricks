#!/usr/bin/env bash
# Deploys the sample agent to Amazon Bedrock AgentCore with the AgentCore starter toolkit and prints the runtime ARN
# to register on the Skill Hub's Models page. See README.md for what to set up first.
#
# Run it from this folder, in a Python environment with bedrock-agentcore-starter-toolkit installed, signed in to AWS
# with permission to create the runtime, its execution role and a secret. It reads the OAuth secret of the agent's
# Databricks service principal from the terminal without showing it, and stores it in AWS Secrets Manager.
set -euo pipefail

# No apostrophe inside "${...}": the bash 3.2 that macOS ships reads one as an opening quote.
: "${DATABRICKS_HOST:?set DATABRICKS_HOST to the workspace URL, such as https://<workspace host>}"
: "${DATABRICKS_CLIENT_ID:?set DATABRICKS_CLIENT_ID to the application ID of the service principal of the agent}"
REGION="${AWS_REGION:-us-east-1}"
NAME="${AGENT_NAME:-skillhub_skill_agent}"
SECRET_NAME="${SECRET_NAME:-skillhub/agents/${NAME}}"
case "$NAME" in
  skillhub*) ;;  # the eval key's IAM policy may invoke runtime/skillhub* only
  *) echo "AGENT_NAME must start with skillhub" >&2; exit 2 ;;
esac

read -rs -p "OAuth secret of ${DATABRICKS_CLIENT_ID}: " CLIENT_SECRET
echo
# The secret goes to the AWS CLI through a private file, not the command line, where other users could read it.
SECRET_FILE=$(umask 077; mktemp)
trap 'rm -f "$SECRET_FILE"' EXIT
printf '%s' "$CLIENT_SECRET" > "$SECRET_FILE"
unset CLIENT_SECRET
if aws secretsmanager describe-secret --region "$REGION" --secret-id "$SECRET_NAME" >/dev/null 2>&1; then
  aws secretsmanager put-secret-value --region "$REGION" --secret-id "$SECRET_NAME" \
    --secret-string "file://$SECRET_FILE" >/dev/null
else
  aws secretsmanager create-secret --region "$REGION" --name "$SECRET_NAME" --secret-string "file://$SECRET_FILE" \
    --tags Key=project,Value=skillhub >/dev/null
fi
rm -f "$SECRET_FILE"
SECRET_ARN=$(aws secretsmanager describe-secret --region "$REGION" --secret-id "$SECRET_NAME" --query ARN --output text)

agentcore configure --entrypoint agent.py --name "$NAME" --requirements-file requirements.txt --region "$REGION" \
  --deployment-type direct_code_deploy --runtime PYTHON_3_11 --disable-otel --disable-memory --non-interactive
agentcore deploy --agent "$NAME" --auto-update-on-conflict \
  --env "DATABRICKS_HOST=${DATABRICKS_HOST}" \
  --env "DATABRICKS_CLIENT_ID=${DATABRICKS_CLIENT_ID}" \
  --env "SKILLHUB_AGENT_SECRET_ARN=${SECRET_ARN}"

# The runtime's execution role may read the secret, and nothing else is added to it.
read -r ROLE_ARN RUNTIME_ARN < <(python3 - "$NAME" <<'PY'
import sys, yaml
agent = yaml.safe_load(open(".bedrock_agentcore.yaml"))["agents"][sys.argv[1]]
print(agent["aws"]["execution_role"], agent["bedrock_agentcore"]["agent_arn"])
PY
)
aws iam put-role-policy --role-name "${ROLE_ARN##*/}" --policy-name skillhub-agent-secret --policy-document "$(
  printf '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"secretsmanager:GetSecretValue","Resource":"%s"}]}' "$SECRET_ARN"
)"
echo "Runtime ARN: ${RUNTIME_ARN}"
echo "Register it on the Models page as an external agent (Amazon Bedrock AgentCore), then run Test."
