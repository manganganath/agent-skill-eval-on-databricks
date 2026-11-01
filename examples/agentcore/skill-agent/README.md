# Sample skill agent on Amazon Bedrock AgentCore

A [Strands](https://strandsagents.com) agent, served by the Amazon Bedrock AgentCore runtime SDK, that the Skill Hub can evaluate as an external agent. It loads skills from the Unity Gateway skills MCP, calls the Amazon Bedrock model that each eval case names, and sends its OpenTelemetry trace to the department's trace table. Use it as it is, or as the reference for an agent in another framework.

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

   Then give the group its grants. The `skillhub-bootstrap` Job runs as `skillhub-ci` and grants the group its schema and table access. The agent's principal gets `USE CATALOG` through its department reader group, which it must join (see above).
2. **The eval key may invoke the runtime.** The IAM user whose key is in the `skillhub-aws` secret scope needs `bedrock-agentcore:InvokeAgentRuntime` on `arn:aws:bedrock-agentcore:<region>:<account-id>:runtime/skillhub*`. The runtime name must therefore start with `skillhub`.
3. **Network.** The serverless eval Job must reach `bedrock-agentcore.<region>.amazonaws.com`, and the runtime must reach the workspace.

## Try it locally

With an AWS sign-in that may call Bedrock and read the secret:

```bash
python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
export DATABRICKS_HOST=https://<workspace host> DATABRICKS_CLIENT_ID=<application ID> \
       SKILLHUB_AGENT_SECRET_ARN=<secret ARN> AWS_REGION=us-east-1
python agent.py &                                        # serves http://localhost:8080
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
pip install bedrock-agentcore-starter-toolkit
export DATABRICKS_HOST=https://<workspace host> DATABRICKS_CLIENT_ID=<application ID> AWS_REGION=us-east-1
./deploy.sh
```

The script stores the OAuth secret in AWS Secrets Manager (`skillhub/agents/<name>`, read from the terminal without showing it, and passed to the AWS CLI through a private temporary file that is removed when the script ends), deploys the runtime `skillhub_skill_agent` with the starter toolkit, lets its execution role read that secret, and prints the runtime ARN. The runtime, the secret and the execution role's inline policy are named `skillhub*`, and the secret is tagged `project=skillhub`, with `AWS_TAGS` as well when set. The starter toolkit names the execution role and the code bucket itself. Run it again to deploy a change.

If your account's policies require tags on resources, or delete untagged ones, set `AWS_TAGS`, for example `AWS_TAGS="Owner=me@example.com,cost-center=42"`. The script then tags the secret, the runtime, its execution role and the toolkit's code bucket. The bucket is shared by the account's agents, so its own tags are kept. Without `AWS_TAGS`, only the secret is tagged, `project=skillhub`.

The starter toolkit installs the agent's dependencies with `uv`, for Linux ARM64. Behind a package proxy, set `UV_INDEX_URL` to the proxy's index before you run the script; otherwise the build fails to fetch from `pypi.org`.

## Register and test

On the Skill Hub's **Models** page, a reviewer adds an external agent: harness **Amazon Bedrock AgentCore**, the runtime ARN, the Bedrock model ID, for example `us.anthropic.claude-sonnet-5-5`, and a class. The region is the runtime's. Register one row per model. **Test** runs one case, which loads `common`'s skills, and waits up to two minutes for its trace in `common`'s trace table; then enable the row. Run eval lists it under **External agents**, for live skills.

If the test fails:
- `AccessDeniedException` on the invoke: the eval key's IAM policy (step 2).
- `the agent reported: ...`: the agent's own error, also in its CloudWatch logs.
- `the agent reported: the skills MCP refused the agent (HTTP <code>: ...)`: the workspace's own reason. For example, the agent's principal has no workspace entitlement, or may not read the skills' schema.
- `its trace did not reach ...`: the principal is not in `skillhub-agents`, bootstrap has not run since, or the runtime cannot reach the workspace.
