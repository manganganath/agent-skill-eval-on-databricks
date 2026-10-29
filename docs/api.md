# Skill Hub API

The Skill Hub REST API serves the app's UI and any custom UI. The OpenAPI schema is served at `/docs` on the app.

## REST API

**Authentication.** The signed-in user's token arrives in the `x-forwarded-access-token` header. The backend builds a per-request client with that token, so reads use the user's permissions. Jobs are started with the app service principal. A check that cannot be made returns 403. An error body has the form `{"detail": {"reason": "<code>"}}`, for example `skill_not_found`, `skill_not_readable`, `not_reviewer`, `job_failed` or `try_timeout`.

| Method and path | Request | Response | Notes |
|---|---|---|---|
| `GET /api/me` | | `{user, groups, is_reviewer}` | |
| `GET /api/config` | | `{workspace_url, models, departments}` | `models` maps each model profile to its class |
| `GET /api/skills?q=` | | List of `{fqn, dept, name, description, live_version, verdicts, eval_access}` | `eval_access` is `ok`, `denied` or `unavailable` |
| `GET /api/skills/{fqn}` | | `{fqn, dept, name, description, live_version, verdicts, files, evals, rubric, eval_access}` | `live_version` and `verdicts` are the same as in the catalog list. `files` maps path to text. If the warehouse fails, the answer is still 200, with `eval_access` set to `unavailable`. |
| `GET /api/skills/{fqn}/versions` | | JournalRow objects, oldest first | `[]` before any version is published |
| `GET /api/results/matrix?skill=&dept=&fqn=` | | `eval_matrix` rows, each with `dept` and `harness` | `fqn=<catalog>.<schema>.<skill>` sets both `dept` and `skill`. Departments the user cannot read are left out and named in the `X-Eval-Denied-Departments` header. If the result hit the row cap, `X-Result-Truncated: true` is set. |
| `GET /api/results/cases?run_id=` | | `eval_cases` rows | `run_id` is a 32-character MLflow run ID. The same two headers apply. |
| `POST /api/eval-runs` | `{skills, models, modes}` and optional `versions` | `{run_key, job_run_id}` | `skills` and `modes` are lists; each skill is a live FQN `<catalog>.<schema>.<skill>`, and a bare name answers 404 `skill_not_found`. `versions` is keyed by the same FQNs listed in `skills` and maps a skill to a registered version number, and that version is evaluated instead of the live copy. 403 if the user cannot read a requested skill. |
| `GET /api/eval-runs/{job_run_id}` | | `{state, result_state, run_key}` | |
| `POST /api/skills/{fqn}/switch` | `{to_version}` | `{job_run_id}` | 403 unless the user is a reviewer. An invalid Job parameter answers 502 `job_failed`. |
| `POST /api/try` | `{fqn, prompt, models}` | One entry per model, with `with_skill` and `without_skill` answers, whether the skill was invoked, and a trace link | Beta. Answers 504 `try_timeout` after 180 seconds. The next Try can start at once. |

Example:

```bash
curl -X POST "<app-url>/api/eval-runs" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"skills": ["<catalog>.<schema>.earnings-preview"], "models": ["claude-sonnet"], "modes": ["trigger"]}'
```

The response has this shape:

```json
{"run_key": "hub_<8hex>", "job_run_id": 123456}
```

## Direct Databricks API equivalents

For a custom UI that does not call the REST API:

| Need | Databricks API | Permission |
|---|---|---|
| List and read skills | Unity Catalog Skills API (Beta); see the Skills API reference on docs.databricks.com | Read access on the schema |
| Read skill files | Files API, paths `Skills/<catalog>/<schema>/<skill>/<path>` | `READ VOLUME` on the schema |
| Results | SQL Statement Execution API, `POST /api/2.0/sql/statements`, over `<d>_evals.eval_matrix` and `eval_cases` | `SELECT` on the table and `CAN USE` on the warehouse |
| Start an eval | Jobs API `run-now` for the `skill-eval` Job | Only the CI and app service principals have run permission, so a custom UI calls the Skill Hub API instead |
| Switch a version | Jobs API `run-now` for `skill-switch` | The same restriction applies, so use the Skill Hub API |
| Traces | MLflow tracing: `mlflow.search_traces` with the experiment ID | `CAN_READ` on the experiment |
| Model calls for a Try panel | Model Serving endpoints, OpenAI-compatible `/serving-endpoints/<endpoint>/invocations` | `CAN QUERY` |

The skills MCP endpoint that agents use is in Unity Gateway (Beta). This product's client for it is in one module of the package, and its address is not part of this document. Use that module as a reference implementation rather than building against an undocumented address.

**Example.** List a skill's matrix rows through the SQL Statement Execution API:

```python
import os

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementParameterListItem

w = WorkspaceClient()
result = w.statement_execution.execute_statement(
    warehouse_id=os.environ["SQL_WAREHOUSE_ID"],
    statement=(
        "SELECT skill, skill_version, model_profile, harness, mode, verdict, pass_rate, n_reviewed, n_cases "
        "FROM <catalog>.<d>_evals.eval_matrix WHERE skill = :skill"
    ),
    parameters=[StatementParameterListItem(name="skill", value="<skill>")],
    wait_timeout="30s",
)
for row in result.result.data_array or []:
    print(row)
```
