# Sample skill agent on Amazon Bedrock AgentCore

A [Strands](https://strandsagents.com) agent, served by the Amazon Bedrock AgentCore runtime SDK, that the Skill Hub can evaluate as an external agent. It loads skills from the Unity Gateway skills MCP, calls the Amazon Bedrock model that each eval case names, and sends its OpenTelemetry trace to the department's trace table. Use it as it is, or as the reference for an agent in another framework.

It is an [AgentCore CLI](https://github.com/aws/agentcore-cli) project (`@aws/agentcore` 1.0):

| Path | What it is |
|---|---|
| `app/skill_agent/` | The agent: `agent.py`, `contract.py` (the Skill Hub's eval contract, standard library only) and `pyproject.toml` (its dependencies) |
| `agentcore/agentcore.json` | The project spec: one runtime, `skill_agent`, built from `app/skill_agent/` as a code zip for Python 3.13 |
| `agentcore/cdk/` | The CDK app that `agentcore deploy` runs. `lib/cdk-stack.ts` adds what is particular to a deployment (see [Deploy](#deploy)) |
| `deploy.sh` | Stores the agent's OAuth secret in AWS Secrets Manager, runs `agentcore deploy`, and prints the runtime ARN |
| `network.yaml` | Optional: a VPC whose outbound traffic leaves from one fixed IP address, for a workspace with IP access lists |
| `payload.example.json` | One case, as the eval Job sends it |

## What the eval Job sends, and what the agent does

The `skill-eval` Job invokes the runtime once per case and repeat, each time in a new session, with this JSON payload (`payload.example.json` is one):

| Field | Meaning |
|---|---|
| `prompt` | The case's prompt |
| `mode` | `trigger`, `with_skill` or `without_skill` |
| `skills_mcp` | One skills MCP URL per schema, the department's first, then `common`'s. `null` in `without_skill` mode: the agent must not load skills |
| `model` | The Bedrock model ID or inference profile ID to call |
| `max_turns` | At most this many model turns: 3 in trigger mode, 8 otherwise |
| `trace` | Attributes to put on every span: `skillhub.run_key`, `skillhub.trace_key`, `skillhub.case_id`, `skillhub.repeat`, `skillhub.mode`, `skillhub.profile` |
| `otel` | Where the spans go: `endpoint` (`https://<workspace host>/api/2.0/otel/v1/traces`) and `uc_table` (`<catalog>.<dept>_evals.traces_otel_spans`), sent as the header `X-Databricks-UC-Table-Name` |
| `files`, `search_index` | The case's fixture files as text, and the keyword index over them. The agent offers them as the `web_search`, `read_file` and `read_workbook` tools, as the Skill Hub's own agent loop does |

The agent answers `{"answer": "<final text>"}`, or `{"error": "<why>"}`, which makes the case an error, not a fail. The Skill Hub finds the trace by its attributes, reads the first skill tool call from the `execute_tool` spans, the tokens from the `chat` spans, and runs its judges on the trace and the answer.

The agent:
- refuses a skills MCP URL on any other host than `DATABRICKS_HOST`, so its token goes nowhere else;
- never offers the skills MCP's authoring tools (`create_skill`, `update_skill`, `delete_skill`);
- names each skill tool as Bedrock accepts: dots become underscores (and hyphens too for Amazon Nova models), and a name over 64 characters is shortened with a hash. The Skill Hub maps these names back;
- records the request, the response, each tool call's arguments and result, and its tool definitions as span attributes, in the OpenTelemetry GenAI conventions (`gen_ai.input.messages`, `gen_ai.tool.call.arguments` and so on). The tool-call judges read them from the trace. By default Strands puts them in span events instead, where MLflow does not find the request or the tool calls' arguments, so the agent sets `OTEL_SEMCONV_STABILITY_OPT_IN` (`contract.SEMCONV_OPT_IN`), keeping any other values the runtime sets;
- answers with what the model wrote when it reaches its output limit, as the Skill Hub's own agent loop does: the judges score the answer as it stands;
- flushes its spans before it answers.

## Before you deploy

1. **A Databricks service principal for the agent.** A workspace admin creates it (for example `skillhub-skill-agent`), creates an OAuth secret for it, and adds it to:
   - the group `skillhub-agents` (hub.yaml `roles.agents`), which bootstrap lets write spans to each department's `traces_otel_spans` table;
   - the reader group of each department whose skills it may load, for example `skillhub-users` for `common`.

   ```bash
   databricks service-principals create --display-name skillhub-skill-agent --json '{"active": true}'
   # add the principal to skillhub-agents and to the department reader groups (Settings > Identity and access)
   ```

   Create `skillhub-agents` once as an **account** group and assign it to the workspace (account console, or the account-level groups API). Do not use `databricks groups create`: it makes a workspace-local group, and Unity Catalog cannot grant to those, so the grants would fail with `PRINCIPAL_DOES_NOT_EXIST`.

   Give the group the workspace entitlement `workspace-access`. The trace endpoint refuses a principal without it ("This API is disabled for users without the workspace-access entitlement"). `workspace-consume` is enough for the skills MCP, not for the traces:

   ```bash
   databricks api patch /api/2.0/preview/scim/v2/Groups/<group id> --json '{"schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"], "Operations": [{"op": "add", "path": "entitlements", "value": [{"value": "workspace-access"}]}]}'
   ```

   Then give the group its grants. The `skillhub-bootstrap` Job runs as `skillhub-ci` and grants the group its schema and table access. The agent's principal gets `USE CATALOG` through its department reader group, which it must join (see above).
2. **The eval key may invoke the runtime.** The IAM user whose key is in the `skillhub-aws` secret scope needs `bedrock-agentcore:InvokeAgentRuntime` on `arn:aws:bedrock-agentcore:<region>:<account-id>:runtime/skillhub*`. The runtime name must therefore start with `skillhub`.
3. **Network.** The serverless eval Job must reach `bedrock-agentcore.<region>.amazonaws.com`, and the runtime must reach the workspace. If the workspace has IP access lists, the runtime must call it from an address on a list. In AgentCore's public network mode it calls from AWS addresses that change, which the workspace refuses ("Current IP is not allowed" in its audit log). Run it in VPC mode instead, in subnets whose outbound traffic leaves through a NAT gateway with a fixed Elastic IP, and allow that address (see [A network with a fixed address](#a-network-with-a-fixed-address)).
4. **Tools and the AWS account.** Node.js 20.12 or later, [uv](https://docs.astral.sh/uv/), the AWS CLI, and the AgentCore CLI:

   ```bash
   npm install -g @aws/agentcore
   ```

   Uninstall the older starter toolkit first (`pip uninstall bedrock-agentcore-starter-toolkit`): it also installs an `agentcore` command. `agentcore deploy` deploys a CDK stack, so the account and region must be bootstrapped for CDK once. Bootstrapping creates IAM roles, so it is for someone allowed to create them. Add `--tags` if your account requires tags:

   ```bash
   npx -y aws-cdk@2 bootstrap aws://<account-id>/<region> --tags Owner=<you>
   ```

## Try it locally

With an AWS sign-in that may call Bedrock and read the secret:

```bash
cd app/skill_agent && uv venv && uv pip install -r pyproject.toml
export DATABRICKS_HOST=https://<workspace host> DATABRICKS_CLIENT_ID=<application ID> \
       SKILLHUB_AGENT_SECRET_ARN=<secret ARN> AWS_REGION=us-east-1
.venv/bin/python agent.py &                              # serves http://localhost:8080
cd ../..
curl -s localhost:8080/ping
curl -s -X POST localhost:8080/invocations -H 'Content-Type: application/json' -d @payload.example.json
```

Fill in the placeholders of `payload.example.json` first. The answer is `{"answer": ...}`, and a trace with `skillhub.run_key` `local_smoke` appears in the `common` trace table within a minute:

```sql
SELECT trace_id, name FROM <catalog>.common_evals.traces_otel_spans
WHERE variant_get(attributes, '$["skillhub.run_key"]', 'string') = 'local_smoke'
```

## Deploy

```bash
export DATABRICKS_HOST=https://<workspace host> DATABRICKS_CLIENT_ID=<application ID> AWS_REGION=us-east-1
./deploy.sh
```

The script:
1. checks its settings, and that `agentcore` is the AgentCore CLI 1.0 or later;
2. stores the OAuth secret in AWS Secrets Manager, tagged `project=skillhub`. It reads the secret from the terminal without showing it, and passes it to the AWS CLI through a private temporary file that is removed when the script ends;
3. runs `agentcore deploy`. That builds the code zip with uv, for Linux ARM64, and deploys the stack `AgentCore-skillhub-default`. The stack holds the runtime `skillhub_default_skill_agent` and its execution role, which may call Bedrock models, write its logs and read that one secret;
4. prints the runtime ARN.

Run it again to deploy a change. The settings, from the environment:

| Setting | Meaning |
|---|---|
| `DATABRICKS_HOST`, `DATABRICKS_CLIENT_ID` | The workspace URL, and the application ID of the agent's service principal. Required |
| `AWS_REGION` | Where to deploy. Default `us-east-1` |
| `SECRET_NAME` | The Secrets Manager secret for the OAuth secret. Default `skillhub/agents/skill_agent` |
| `AGENT_SUBNETS`, `AGENT_SECURITY_GROUPS` | Comma-separated subnet and security group IDs. When set, the runtime runs in VPC mode in those subnets. Set both or neither |
| `AWS_TAGS` | Tags for the secret and every resource of the stack, such as `Owner=me@example.com,cost-center=42`, for accounts whose policies require tags or delete untagged resources. Everything also gets `project=skillhub` |
| `UV_INDEX_URL` | Behind a package proxy, the proxy's index. Without it, the build fetches the dependencies from `pypi.org` |

The deployment's own state stays out of git: the account and region of the target (`agentcore/aws-targets.json`), and what the CLI deployed there (`agentcore/.cli/`).

## A network with a fixed address

Skip this when you already have private subnets with a NAT gateway: pass them as `AGENT_SUBNETS`, with a security group that allows outbound HTTPS, and allow the NAT gateway's Elastic IP in the workspace.

Otherwise, `network.yaml` makes one: a VPC with two private subnets in zones where AgentCore supports VPC mode, a NAT gateway with an Elastic IP, and a security group that allows outbound HTTPS only, with no inbound access. A NAT gateway is billed by the hour.

```bash
aws cloudformation deploy --stack-name skillhub-agent-network --template-file network.yaml \
  --parameter-overrides AvailabilityZoneIds=use1-az1,use1-az2 --tags project=skillhub
aws cloudformation describe-stacks --stack-name skillhub-agent-network --query 'Stacks[0].Outputs' --output table
```

Use zone IDs where AgentCore supports VPC mode in your region (`use1-az1,use1-az2` for us-east-1). Then a workspace admin allows the `EgressIp` output, in an IP access list of its own:

```bash
databricks api post /api/2.0/ip-access-lists --json '{"label": "skillhub-agentcore-egress", "list_type": "ALLOW", "ip_addresses": ["<EgressIp>/32"]}'
```

Deploy with the `SubnetIds` and `SecurityGroupId` outputs as `AGENT_SUBNETS` and `AGENT_SECURITY_GROUPS`. The Elastic IP is kept when the stack is deleted, so the allow-list entry never comes to mean another account's address. Release it on purpose when you no longer need it.

## Register and test

On the Skill Hub's **Models** page, a reviewer adds an external agent: harness **Amazon Bedrock AgentCore**, the runtime ARN, the Bedrock model ID, for example `us.anthropic.claude-sonnet-5-5`, and a class. The region is the runtime's. Register one row per model. **Test** runs one case, which loads `common`'s skills, and waits up to two minutes for its trace in `common`'s trace table; then enable the row. Run eval lists it under **External agents**, for live skills.

If the test fails:
- `AccessDeniedException` on the invoke: the eval key's IAM policy (step 2).
- `the agent reported: ...`: the agent's own error, also in its CloudWatch logs.
- `the agent reported: the skills MCP refused the agent (HTTP <code>: ...)`: the workspace's own reason. For example, the agent's principal has no workspace entitlement, or may not read the skills' schema, or the workspace's IP access lists do not allow the runtime's address (step 3).
- `its trace did not reach ...`: the principal is not in `skillhub-agents`, the group has no `workspace-access` entitlement (the agent's logs show `Failed to export spans batch code: 403`), bootstrap has not run since, or the runtime cannot reach the workspace.

## Change or remove the agent

Change the code in `app/skill_agent/` and its dependencies in `app/skill_agent/pyproject.toml`, then run `./deploy.sh` again. To give the agent more AWS resources, add them in `agentcore/cdk/lib/cdk-stack.ts`. To remove the agent, delete its stack with `aws cloudformation delete-stack --stack-name AgentCore-skillhub-default`, then its secret.
