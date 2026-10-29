# Skill Hub API

The Skill Hub REST API serves the app's UI and any custom UI. The OpenAPI schema is served at `/docs` on the app.

## REST API

**Authentication.** The signed-in user's token arrives in the `x-forwarded-access-token` header. The backend builds a per-request client with that token, so reads use the user's permissions. Jobs are started with the app service principal. A check that cannot be made returns 403. An error body has the form `{"detail": {"reason": "<code>"}}`, for example `skill_not_found`, `skill_not_readable`, `not_reviewer`, `job_failed` or `try_timeout`.

| Method and path | Request | Response | Notes |
|---|---|---|---|
| `GET /api/me` | | `{user, groups, is_reviewer, is_author}` | `is_author` and `is_reviewer` tell the UI whether to show candidates |
| `GET /api/config` | | `{workspace_url, models, departments}` | `models` maps each model profile to its class |
| `GET /api/skills?q=` | | List of `{fqn, dept, name, description, live_version, verdicts, eval_access}` | `eval_access` is `ok`, `denied` or `unavailable` |
| `GET /api/skills/{fqn}` | | `{fqn, dept, name, description, live_version, verdicts, files, evals, rubric, eval_access}` | `live_version` and `verdicts` are the same as in the catalog list. `files` maps path to text. If the warehouse fails, the answer is still 200, with `eval_access` set to `unavailable`. |
| `GET /api/skills/{fqn}/versions` | | JournalRow objects, oldest first | `[]` before any version is published |
| `GET /api/candidates` | | List of `{kind, run_key, pr_number, git_sha, uploaded_at, uploaded_by, has_rubric, dept, name, description, content_hash, verdicts, eval_access}` | Skills under review in a pull request (`kind` `pr`) and Skill Hub drafts (`kind` `draft`). A draft has no `pr_number` or `git_sha`; `uploaded_by` and `has_rubric` are set for drafts only. `[]` for a user who is neither an author nor a reviewer. See [Candidates](#candidates). |
| `GET /api/candidates/{run_key}/{dept}/{skill}` | | `{dept, name, description, content_hash, verdicts, files, evals, rubric, eval_access, results, candidate}` | The same shape as a skill detail. `evals` and `rubric` come from the candidate's `evals/` folder, `results` lists its eval runs newest first, and `candidate` is `{kind, pr_number, run_key, git_sha, uploaded_at, uploaded_by}`. 403 `not_author` for a user who is neither an author nor a reviewer; 404 `candidate_not_found` for a candidate that is missing or that the user may not see. |
| `GET /api/results/matrix?skill=&dept=&fqn=` | | `eval_matrix` rows, each with `dept` and `harness` | `fqn=<catalog>.<schema>.<skill>` sets both `dept` and `skill`. Departments the user cannot read are left out and named in the `X-Eval-Denied-Departments` header. If the result hit the row cap, `X-Result-Truncated: true` is set. |
| `GET /api/results/cases?run_id=` | | `eval_cases` rows | `run_id` is a 32-character MLflow run ID. The same two headers apply. |
| `GET /api/results/dashboard` | | `{summary, verdicts, uplift, routing, trigger_trend, pass_rate_trend, health, tokens}` | Aggregates over the departments the user can read, each row with `dept`. `summary` has the tiles, `verdicts` the latest gating rows by model, `uplift` the with-skill rows of each skill's live version, `routing` the trigger outcomes by expected and invoked skill, and the two trends the last 30 days by day. `tokens` is the last 30 days of usage per configured endpoint, or `null` when the user cannot read `system.serving`. The same denial rules as the matrix apply: a 403 `evals_not_readable` when no matrix row can be read. |
| `POST /api/eval-runs` | `{skills, models, modes}` and optional `versions`, or `{candidate: {run_key, skills}, models, modes}` | `{run_key, job_run_id}` | `skills` and `modes` are lists; each skill is a live FQN `<catalog>.<schema>.<skill>`, and a bare name answers 404 `skill_not_found`. `versions` is keyed by the same FQNs listed in `skills` and maps a skill to a registered version number, and that version is evaluated instead of the live copy. 403 if the user cannot read a requested skill. For a candidate, `skills` lists `dept/name` entries of that upload; see [Candidates](#candidates). A body with both live skills or `versions` and a `candidate` answers 422 `mixed_sources`. |
| `POST /api/drafts` | `{dept, archive}` or `{dept, files: [{path, content}]}` | 201 `{run_key, dept, name, has_rubric}` | Upload a draft skill: `archive` is a zip of the skill folder, base64; `files` are the folder's files, each base64 with its path. See [Drafts](#drafts). |
| `GET /api/eval-runs/{job_run_id}` | | `{state, result_state, run_key}` | |
| `POST /api/skills/{fqn}/switch` | `{to_version}` | `{job_run_id}` | 403 unless the user is a reviewer. An invalid Job parameter answers 502 `job_failed`. |
| `POST /api/try` | `{fqn, prompt, models}` | One entry per model, with `with_skill` and `without_skill` answers, whether the skill was invoked, and a trace link | Beta. Answers 504 `try_timeout` after 180 seconds. The next Try can start at once. |
| `POST /api/candidates/{run_key}/{dept}/{skill}/try` | `{prompt, models}` | The same as `/api/try` | Beta. Tries a skill under review: a pull request candidate or a draft. The upload is staged as the app into a private run schema that only the caller can read, and the live skill of the same name is left out of the run. The schema is dropped when the run ends. 403 `not_author` and 404 `candidate_not_found` as for [candidate detail](#candidates). 503 `try_setup_required` until the catalog grant is in place (see [Try on skills under review](operations.md#try-on-skills-under-review)). The other answers are as for `/api/try`. |

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

### Candidates

A candidate is a skill that a pull request adds or changes, before it is released. CI uploads the pull request's checkout to `<workspace_root>/<catalog>/candidates/<run key>/`, with the run key `pr<N>_<sha7>_a<attempt>`, and runs the pull request's eval with that key. The listing shows the skills that eval was started for, and the skills that have eval rows for the key, from the newest upload of each pull request.

Visibility is the same for every candidate route:

- Only members of the authors and reviewers groups (`roles` in `config/hub.yaml`) see candidates.
- Reviewers see every department.
- An author sees a department only when Unity Catalog's effective permissions show `USE CATALOG`, `USE SCHEMA` and `READ VOLUME` on its live schema for the user or one of the user's groups. If that check fails, the department is hidden.
- The uploader of a draft always sees it.

After these checks the app reads the candidate folder as its service principal. Eval rows are read with the user's token, from `<d>_evals.eval_runs`, for the candidate's run key and the keys of the Skill Hub runs started for it.

`POST /api/eval-runs` with a `candidate` starts `skill-eval` with `skills_root` set to the upload's `skills` folder, `fixtures_root` set to its `fixtures` folder (or empty for the Job's default when the upload has none), `source` `git:<sha7>` (`draft:<id>` for a draft) and `trigger_source` `manual`. A skill without `evals/rubric.json` runs trigger mode only: asking for effectiveness answers 422 `rubric_required`. Each request gets a fresh run key, `<candidate run key>_h<8 hex digits>`, so it never collides with the pull request's gate. Authors and reviewers only (403 `not_author`); a candidate the user may not see answers 404 `candidate_not_found`.

```bash
curl -X POST "<app-url>/api/eval-runs" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"candidate": {"run_key": "pr12_ab12cd3_a1", "skills": ["equity_research/earnings-preview"]}, "models": ["claude-sonnet"], "modes": ["trigger"]}'
```

### Drafts

A draft is a skill uploaded in the Skill Hub, to evaluate it before any pull request. It is never published: publish accepts only rows of the merge eval (`trigger_source` `merge`). Authors and reviewers may upload; an author only to a department whose live skills they can read (else 403 `department_not_allowed`).

- The upload needs `SKILL.md` with `name` and `description` in its frontmatter, and `evals/evals.json` with at least one case. `evals/rubric.json` is optional. A top-level `fixtures/` folder is stored beside the skill and replaces the default fixtures for the draft's evals. `SKILL.md` may be at the top of the upload or inside one folder, whose name must equal the frontmatter `name`.
- The skill is checked as `skillhub lint` checks a bundle, including thresholds that would loosen the gate. Every problem found is listed in a 422 answer: `{"detail": {"reason": "draft_invalid", "errors": ["..."]}}`.
- Paths must be relative with `/` between names: absolute paths, `..`, backslashes and drive letters are refused, and so are symbolic links and other special archive entries. A request over 8 MB, an archive over 4 MB, more than 200 files or more than 4 MB once unpacked answers 413 `draft_too_large`.
- The app stores the draft as its service principal in `<workspace_root>/<catalog>/drafts/draft_<id>/`: `skills/<dept>/<skill>/`, `fixtures/`, the `.uploaded_at` marker and `draft.json` (who uploaded it). `<id>` is 8 hex digits of the upload time and 16 random hex digits. The nightly cleanup removes a draft 168 hours after upload. A failed write answers 502 `draft_store_failed` and leaves nothing behind.

## Direct Databricks API equivalents

For a custom UI that does not call the REST API:

| Need | Databricks API | Permission |
|---|---|---|
| List and read skills | Unity Catalog Skills API (Beta); see the Skills API reference on docs.databricks.com | Read access on the schema |
| Read skill files | Files API, paths `Skills/<catalog>/<schema>/<skill>/<path>` | `READ VOLUME` on the schema |
| Results | SQL Statement Execution API, `POST /api/2.0/sql/statements`, over `<d>_evals.eval_matrix` and `eval_cases` | `SELECT` on the table and `CAN USE` on the warehouse |
| Token usage | SQL Statement Execution API over `system.serving.endpoint_usage` and `system.serving.served_entities` | `SELECT` on both tables and `CAN USE` on the warehouse. Without it the Dashboard shows no token usage |
| Candidate and draft files | Workspace API `GET /api/2.0/workspace/list` and `/export` under `<workspace_root>/<catalog>/candidates/<run key>` or `.../drafts/draft_<id>` | `CAN_READ` on the folder; reviewers have it through the experiment folder's access list |
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
