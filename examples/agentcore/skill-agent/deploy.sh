#!/usr/bin/env bash
# Deploys the sample agent to Amazon Bedrock AgentCore with the AgentCore CLI (@aws/agentcore) and prints the runtime
# ARN to register on the Skill Hub's Models page. See README.md for what to set up first.
#
# Run it signed in to AWS with permission to deploy a CDK stack, in an account and region that `cdk bootstrap` has
# prepared. It reads the OAuth secret of the agent's Databricks service principal from the terminal without showing it,
# and stores it in AWS Secrets Manager. The stack in agentcore/cdk/ makes the runtime, lets it read that one secret, and
# puts it in VPC mode and tags it as the settings below say.
set -euo pipefail
cd "$(dirname "$0")"

# No apostrophe inside "${...}": the bash 3.2 that macOS ships reads one as an opening quote.
: "${DATABRICKS_HOST:?set DATABRICKS_HOST to the workspace URL, such as https://<workspace host>}"
: "${DATABRICKS_CLIENT_ID:?set DATABRICKS_CLIENT_ID to the application ID of the service principal of the agent}"
REGION="${AWS_REGION:-us-east-1}"
SECRET_NAME="${SECRET_NAME:-skillhub/agents/skill_agent}"
# VPC mode, for a workspace with IP access lists: subnets whose outbound traffic leaves from an address the workspace
# allows, such as through a NAT gateway with an Elastic IP (network.yaml makes one), and the security groups to use.
AGENT_SUBNETS="${AGENT_SUBNETS:-}"
AGENT_SECURITY_GROUPS="${AGENT_SECURITY_GROUPS:-}"
if [ -n "${AGENT_SUBNETS}${AGENT_SECURITY_GROUPS}" ] && { [ -z "$AGENT_SUBNETS" ] || [ -z "$AGENT_SECURITY_GROUPS" ]; }; then
  echo "set both AGENT_SUBNETS and AGENT_SECURITY_GROUPS, or neither" >&2
  exit 2
fi
# Optional tags, for accounts whose policies require them, such as AWS_TAGS="Owner=me@example.com,cost-center=42". The
# secret and every resource of the stack get them, and project=skillhub.
checked() {  # checked tags|secret-tags|subnets|groups: the setting, checked, in the form that its user takes
  python3 - "$1" "${AWS_TAGS:-}" "$AGENT_SUBNETS" "$AGENT_SECURITY_GROUPS" <<'PY'
import json, re, sys
form, tags, subnets, groups = sys.argv[1:]
if form in ("subnets", "groups"):
    name, prefix = ("AGENT_SUBNETS", "subnet") if form == "subnets" else ("AGENT_SECURITY_GROUPS", "sg")
    ids = [i.strip() for i in (subnets if form == "subnets" else groups).split(",") if i.strip()]
    for i in ids:
        if not re.fullmatch(prefix + r"-(?:[0-9a-f]{8}|[0-9a-f]{17})", i):
            sys.exit(f"{name}: {i!r} is not a {prefix} ID")
    print(",".join(ids))
    sys.exit()
pairs = {}
for item in [i for i in tags.split(",") if i.strip()]:
    key, sep, value = (part.strip() for part in item.partition("="))
    if not sep or not re.fullmatch(r"[\w .:/=+@-]{1,128}", key) or not re.fullmatch(r"[\w .:/=+@-]{0,256}", value):
        sys.exit(f"AWS_TAGS: {item.strip()!r} is not key=value")
    pairs[key] = value
if form == "tags":
    print(",".join(f"{k}={v}" for k, v in pairs.items()))
else:
    print(json.dumps([{"Key": k, "Value": v} for k, v in {"project": "skillhub", **pairs}.items()]))
PY
}
TAGS=$(checked tags) || exit 2
SECRET_TAGS=$(checked secret-tags)
AGENT_SUBNETS=$(checked subnets) || exit 2
AGENT_SECURITY_GROUPS=$(checked groups) || exit 2
case "$(agentcore --version 2>/dev/null || true)" in
  [1-9]*) ;;
  *)
    echo "deploy.sh needs the AgentCore CLI 1.0 or later: npm install -g @aws/agentcore. Uninstall the starter" \
      "toolkit first (pip uninstall bedrock-agentcore-starter-toolkit): it installs an agentcore command too." >&2
    exit 2
    ;;
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
  aws secretsmanager tag-resource --region "$REGION" --secret-id "$SECRET_NAME" --tags "$SECRET_TAGS" >/dev/null
else
  aws secretsmanager create-secret --region "$REGION" --name "$SECRET_NAME" --secret-string "file://$SECRET_FILE" \
    --tags "$SECRET_TAGS" >/dev/null
fi
rm -f "$SECRET_FILE"
SECRET_ARN=$(aws secretsmanager describe-secret --region "$REGION" --secret-id "$SECRET_NAME" --query ARN --output text)

# The CLI adds the default target, this AWS account and REGION, on the first deploy.
[ -f agentcore/aws-targets.json ] || echo '[]' > agentcore/aws-targets.json
SKILLHUB_AGENT_SECRET_ARN="$SECRET_ARN" AGENT_SUBNETS="$AGENT_SUBNETS" AGENT_SECURITY_GROUPS="$AGENT_SECURITY_GROUPS" \
  AWS_TAGS="$TAGS" agentcore deploy --target default --region "$REGION"

# The stack exports the runtime's ARN as AgentCore-<project>-<target>-<runtime>-RuntimeArn.
RUNTIME_ARN=$(aws cloudformation list-exports --region "$REGION" \
  --query "Exports[?Name=='AgentCore-skillhub-default-skill-agent-RuntimeArn'].Value" --output text)
echo "Runtime ARN: ${RUNTIME_ARN}"
echo "Register it on the Models page as an external agent (Amazon Bedrock AgentCore), then run Test."
