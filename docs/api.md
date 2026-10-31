# Skill Hub API

The Skill Hub REST API serves the app's UI and any custom UI. The OpenAPI schema is served at `/docs` on the app.

## REST API

**Authentication.** The signed-in user's token arrives in the `x-forwarded-access-token` header. The backend builds a per-request client with that token, so reads use the user's permissions. Jobs are started with the app service principal. A check that cannot be made returns 403. An error body has the form `{"detail": {"reason": "<code>"}}`, for example `skill_not_found`, `skill_not_readable`, `not_reviewer`, `job_failed` or `try_timeout`. The browser's session is a cookie of the Databricks Apps proxy, so a page on another site could post a form to the app as the signed-in user. The app refuses such a request before any route runs: a `POST`, `PUT`, `PATCH` or `DELETE` to `/api/` answers 403 `cross_site_request` when `Sec-Fetch-Site` is `cross-site` or `same-site`. Browsers send that header, and when it is present it alone decides: the app's own pages send `same-origin` and pass. Without it, a request whose `Origin` header names a host other than the app's is refused. The app's host is the one in `X-Forwarded-Host`, which the proxy sets, or in `Host`, for a local run. A request with neither header passes, so curl, scripts and bearer-token clients work as before. Reads (`GET`) are not refused.

| Method and path | Request | Response | Notes |
|---|---|---|---|
| `GET /api/health` | | `{ok}` | Needs no token. |
| `GET /api/me` | | `{user, groups, is_reviewer, is_author}` | `is_author` and `is_reviewer` tell the UI whether to show candidates |
| `GET /api/me/departments` | | `{departments}` | The departments whose live skills the user can read: Unity Catalog's effective permissions show `USE CATALOG`, `USE SCHEMA` and `READ VOLUME` on the live schema for the user or one of the user's groups. The check is the one the catalog falls back on, made as the app. A department that cannot be checked is left out. |
| `GET /api/config` | | `{workspace_url, models, departments, roles, department_access, verdict_rules}` | `models` maps each model profile to its class. `roles` names the authors and reviewers groups, `department_access` gives each department's `readers` groups and `quarantine` flag, and `verdict_rules` the verdict thresholds in force (`fail_rate`, `warn_rate`, `max_error_rate`, `min_reviewed_cases`, `pass_score`, `gating_modes`). Group names only, never membership. |
| `GET /api/skills?q=` | | List of `{fqn, dept, name, description, live_version, verdicts, eval_access}` | `eval_access` is `ok`, `denied` or `unavailable`. A run counts for the live version when its `skill_version` is that version or its content hash is the live version's, so a run recorded before a release (version 0, the merged content) counts once the version is published; the newest run of each model and mode decides. Only models from `hub.yaml` count: a model added in the catalog never changes `verdicts`. |
| `GET /api/skills/{fqn}` | | `{fqn, dept, name, description, live_version, verdicts, files, evals, rubric, eval_access}` | `live_version` and `verdicts` are the same as in the catalog list. `files` maps path to text, without binary files and without `evals/`. `evals` are the cases of the skill's eval dataset table and `rubric` its newest stored rubric. If the warehouse fails, the answer is still 200, with `eval_access` set to `unavailable`. 404 `skill_not_found` for a skill that is missing or that the user cannot read; 502 `uc_failed` when its files cannot be read. |
| `GET /api/skills/{fqn}/versions` | | JournalRow objects, oldest first | `[]` before any version is published. 404 `skill_not_found` as for the skill; 403 `evals_not_readable` when the user cannot read the journal. |
| `GET /api/candidates` | | List of `{kind, run_key, pr_number, git_sha, uploaded_at, uploaded_by, has_rubric, rubric_optional, submitted_pr, dept, name, description, content_hash, verdicts, eval_access}` | Skills under review in a pull request (`kind` `pr`) and Skill Hub drafts (`kind` `draft`). A draft has no `pr_number` or `git_sha`; `uploaded_by`, `has_rubric` and `rubric_optional` are set for drafts only (`rubric_optional`: effectiveness can run without a rubric, see [Candidates](#candidates)), and `submitted_pr` is set for a draft submitted for review, which is left out once its pull request is listed. `[]` for a user who is neither an author nor a reviewer. `verdicts` count `hub.yaml` models only. A pull request skill whose content hash is its department's live version hash is left out, since the release lists it. See [Candidates](#candidates). |
| `GET /api/candidates/{run_key}/{dept}/{skill}` | | `{dept, name, description, content_hash, verdicts, files, evals, rubric, rubric_optional, eval_access, results, candidate}` | The same shape as a skill detail. `evals` and `rubric` come from the candidate's `evals/` folder, `rubric_optional` says whether effectiveness can run without a rubric, `results` lists its eval runs newest first, and `candidate` is `{kind, pr_number, run_key, git_sha, uploaded_at, uploaded_by, submitted_pr, submitted_url}`; the last three are set for drafts only. 403 `not_author` for a user who is neither an author nor a reviewer; 404 `candidate_not_found` for a candidate that is missing or that the user may not see. |
| `GET /api/results/matrix?skill=&dept=&fqn=` | | `eval_matrix` rows, each with `dept` and `harness` | `fqn=<catalog>.<schema>.<skill>` sets both `dept` and `skill`. Departments the user cannot read are left out and named in the `X-Eval-Denied-Departments` header. If the result hit the row cap, `X-Result-Truncated: true` is set. 403 `evals_not_readable` when a department was refused and no row could be read; 404 `department_not_found` for an unknown `dept`, and `skill_not_found` for an unknown `fqn`. |
| `GET /api/results/cases?run_id=` | | `eval_cases` rows | `run_id` is a 32-character MLflow run ID, else 422 `invalid_run_id`. The same two headers and the same 403 apply. |
| `GET /api/results/dashboard` | | `{summary, verdicts, uplift, routing, trigger_trend, pass_rate_trend, health, tokens}` | Aggregates over the departments the user can read, each row with `dept`. `summary` has the tiles, `verdicts` the gating rows of `hub.yaml` models for each skill's live version (else its newest tested version), counted as in the catalog, by model (models added in the catalog are not counted), `health` the error and inconclusive rates of the same rows, `uplift` the with-skill rows of each skill's live version, `routing` the trigger outcomes by expected and invoked skill, and the two trends the last 30 days by day. `tokens` is the last 30 days of usage per day, configured endpoint and `source` (`eval` for evaluation runs and their judges, `try` for Try runs), or `null` when the user cannot read `system.serving`. Other use of the endpoints is not counted. The same denial rules as the matrix apply: a 403 `evals_not_readable` when no matrix row can be read. |
| `POST /api/eval-runs` | `{skills, models, modes}` and optional `versions`, or `{candidate: {run_key, skills}, models, modes}` | `{run_key, job_run_id}` | `skills` and `modes` are lists; each skill is a live FQN `<catalog>.<schema>.<skill>`, and a bare name answers 404 `skill_not_found`. `versions` is keyed by the same FQNs listed in `skills` and maps a skill to a registered version number, and that version is evaluated instead of the live copy: a key not in `skills` answers 422 `version_not_requested`, and a version that is not published 404 `version_not_found`. 403 `skill_not_readable` if the user cannot read a requested skill or version. A model that is neither a `config/hub.yaml` profile nor an enabled model of the model catalog answers 404 `model_not_found`, and 503 `catalog_unavailable` when the catalog cannot be read. When the Job cannot be started: 404 `job_not_found`, 403 `job_forbidden`, 409 `job_conflict` or 502 `job_failed`. For a candidate, `skills` lists `dept/name` entries of that upload; see [Candidates](#candidates). A body with both live skills or `versions` and a `candidate` answers 422 `mixed_sources`. |
| `GET /api/models` | | `{models, catalog_available, bedrock_region}` | Every eval target, for everyone signed in: the `config/hub.yaml` profiles first (`kind` `hub_yaml`, `release_gate` true), then the model catalog by kind (`ug_model`, `external_model`) and name. Each has `name, kind, route, endpoint, model_id, region, model_class, harness, enabled, release_gate, price_in, price_out, tested_at, test, updated_by, updated_at`; `test` is the last connection test, `{status, message, job_run_id}`. `catalog_available` is false when the catalog cannot be read, and then only the hub.yaml profiles are listed. |
| `POST /api/models` | `{name, kind, endpoint}` for a model on Unity Gateway, or `{name, kind, model_id, region}` for a Bedrock model, plus `model_class` and optional `price_in`, `price_out` (USD per million tokens) | 201, the new model | Reviewers only: 403 `not_reviewer`. A new model is disabled. `kind` is `ug_model` (route `serving`) or `external_model` (route `bedrock`; `region` defaults to `bedrock_region`). 422 `kind_not_supported` for `external_agent`; 422 `model_invalid` with `errors`, a list of plain-language problems; 409 `model_exists` for a name that is taken or that is a `config/hub.yaml` profile; 503 `catalog_unavailable`. |
| `PATCH /api/models/{name}` | Any of `endpoint, model_id, region, model_class, price_in, price_out, enabled` | The model | Reviewers only. Changing `endpoint`, `model_id` or `region` disables the model and clears its test. `enabled: true` answers 409 `test_not_passed` unless the last test passed. 404 `model_not_found`, 409 `release_gate_model` for a `config/hub.yaml` profile, 422 `model_invalid`. A model is never deleted. |
| `POST /api/models/{name}/test` | | `{job_run_id}` | Reviewers only. Starts the `skillhub-probe` Job, which runs as `skillhub-ci-eval` and makes one call of at most 16 tokens. 404 `model_not_found`; the Job errors as for eval runs. |
| `GET /api/models/{name}/test/{job_run_id}` | | `{state, message, tested_at}` | Reviewers only. `state` is `running`, `passed`, `failed`, or `stale` when the model changed while it was tested. A finished test is recorded on the model the first time it is read. 404 `run_not_found` for a run that is not this model's test. |
| `GET /api/serving-endpoints` | | List of `{name, task}` | Reviewers only. The chat serving endpoints the reviewer can see, read with their own token, for a new model on Unity Gateway. 502 `serving_unavailable`. |
| `POST /api/drafts` | `{dept, archive}` or `{dept, files: [{path, content}]}` | 201 `{run_key, dept, name, has_rubric}` | Upload a draft skill: `archive` is a zip of the skill folder, base64; `files` are the folder's files, each base64 with its path. Authors and reviewers only: 403 `not_author`. See [Drafts](#drafts). |
| `DELETE /api/drafts/{run_key}` | | `{discarded}` | Remove a draft's files. Its uploader or a reviewer only: 403 `not_draft_owner` for anyone else, 404 `candidate_not_found` when it is gone. Its eval results stay in the eval tables. |
| `POST /api/drafts/{run_key}/submit` | | `{pr_number, url, branch, notes}` | Open a pull request that puts the draft's skill on `main`. Its uploader or a reviewer, once: 403 `not_author` for a user who is neither an author nor a reviewer, 403 `not_draft_owner` for anyone else, 404 `candidate_not_found` for a draft that is gone, 409 `already_submitted` with `pr_number` for a draft already submitted, 409 `no_changes` when `main` already has the skill exactly as the draft has it, 422 `evals_missing`, 503 `github_not_configured`, 502 `github_failed`. See [Pull requests](#pull-requests). |
| `GET /api/release-status?pr=&dept=&skill=` | | `{pr_number, dept, skill, opened, gate, merged, release, live}` | The release steps of one skill's pull request. Authors and reviewers, with the visibility of candidates: 403 `not_author`, 404 `candidate_not_found` for a department the user may not see. 404 `pull_not_found` for a pull request that is missing or that changes no file under `skills/<dept>/<skill>/`. 503 `github_not_configured`, 502 `github_failed`. See [Pull requests](#pull-requests). |
| `POST /api/pulls/{number}/merge` | | `{pr_number, merged, sha}` | Merge with a merge commit. Reviewers only: 403 `not_reviewer`. 409 `not_mergeable` unless every rule holds, with `unmet` naming the rules that do not: `open`, `only_skills`, `from_hub_or_visible_skill`, `check:skills-pr`, `check:platform`, `clean` or `mergeable_unknown`, or `github` when GitHub refuses the merge. 404 `pull_not_found`, 503 `github_not_configured`, 502 `github_failed`. See [Pull requests](#pull-requests). |
| `GET /api/eval-runs/{job_run_id}` | | `{state, result_state, run_key}` | `state` is the Job run's life cycle state and `result_state` its result, `null` until it ends. 404 `run_not_found` for a run that is not of the Skill Hub's Jobs, or that another user started; reviewers see every run. |
| `POST /api/skills/{fqn}/switch` | `{to_version}` | `{job_run_id}` | 403 `not_reviewer` unless the user is a reviewer. 404 `version_not_found` for a version that is not published. 409 `switch_in_progress` while a switch of the skill runs, `already_live` for the live version, and `finish_or_revert_switch` after an unfinished switch, until it is run again or reverted. An invalid Job parameter answers 502 `job_failed`. |
| `POST /api/try` | `{fqn, prompt, models}` | One entry per model: `{model, with_skill: {text, invoked}, without_skill: {text}, trace_url}` | Beta. At most 3 models (else 422 `too_many_models`) and 4000 characters of prompt (else 422 `prompt_too_long`); 422 `unknown_model`, 404 `skill_not_found`. Answers 429 `try_busy` when another Try still runs after a 10-second wait, 504 `try_timeout` after 180 seconds, 502 `try_failed` when a model call fails, and 503 `try_unavailable` when the Try engine cannot load. The next Try can start at once. |
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

`POST /api/eval-runs` with a `candidate` starts `skill-eval` with `skills_root` set to the upload's `skills` folder, `fixtures_root` set to its `fixtures` folder (or empty for the Job's default when the upload has none), `source` `git:<sha7>` (`draft:<id>` for a draft) and `trigger_source` `manual`. A skill without `evals/rubric.json` runs trigger mode only: asking for effectiveness answers 422 `rubric_required`. The exception is a skill whose effectiveness cases are all trigger cases with assertions (`rubric_optional` is `true`): effectiveness then judges them on their assertions only. Each request gets a fresh run key, `<candidate run key>_h<8 hex digits>`, so it never collides with the pull request's gate. Authors and reviewers only (403 `not_author`); a candidate the user may not see answers 404 `candidate_not_found`.

```bash
curl -X POST "<app-url>/api/eval-runs" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"candidate": {"run_key": "pr12_ab12cd3_a1", "skills": ["equity_research/earnings-preview"]}, "models": ["claude-sonnet"], "modes": ["trigger"]}'
```

### Drafts

A draft is a skill uploaded in the Skill Hub, to evaluate it before any pull request. It is never published: publish accepts only rows of the merge eval or of a pull request gate run that the release reuses (`trigger_source` `merge` or `pr`). Authors and reviewers may upload; an author only to a department whose live skills they can read (else 403 `department_not_allowed`).

- The upload needs `SKILL.md` with `name` and `description` in its frontmatter, and `evals/evals.json` with at least one case. `evals/rubric.json` is optional. A top-level `fixtures/` folder is stored beside the skill and replaces the default fixtures for the draft's evals. `SKILL.md` may be at the top of the upload or inside one folder, whose name must equal the frontmatter `name`.
- The skill is checked as `skillhub lint` checks a bundle, including thresholds that would loosen the gate. Every problem found is listed in a 422 answer: `{"detail": {"reason": "draft_invalid", "errors": ["..."]}}`.
- Paths must be relative with `/` between names: absolute paths, `..`, backslashes and drive letters are refused, and so are symbolic links and other special archive entries. A request over 8 MB, an archive over 4 MB, more than 200 files or more than 4 MB once unpacked answers 413 `draft_too_large`.
- The app stores the draft as its service principal in `<workspace_root>/<catalog>/drafts/draft_<id>/`: `skills/<dept>/<skill>/`, `fixtures/`, the `.uploaded_at` marker and `draft.json` (who uploaded it, `has_rubric` and `rubric_optional`). `<id>` is 8 hex digits of the upload time and 16 random hex digits. The nightly cleanup removes a draft 168 hours after upload. A failed write answers 502 `draft_store_failed`. The app then removes what it wrote, as far as it can, and the nightly cleanup removes anything left over. A half-written draft is never listed, since `draft.json` is written last.

### Pull requests

A submitted draft becomes a pull request against `main`, and the Skill Hub follows it to a live version.

- **Submit.** The draft's skill folder goes into the commit, `evals/` included. A `fixtures/` folder is left out, and the answer's `notes` say so. The commit replaces the skill's folder: a file the skill has on `main` and the draft lacks is deleted. The branch is `skillhub/<skill>-<the last 8 hex digits of the draft id>`, and the commit's `Submitted-by` trailer names the submitter. The title says Add or Update, by whether `skills/<dept>/<skill>/SKILL.md` is on `main`. A draft without `evals/rubric.json` is accepted: CI generates the rubric in the eval gate, and the pull request says so. A draft whose cases need no rubric (`rubric_optional`) gets none, and the pull request says that its cases are judged on their assertions. The draft's `draft.json` records `submitted_pr`, `submitted_url`, `submitted_by` and `submitted_at`.
- **Release status.** The pull request must change a file under `skills/<dept>/<skill>/`. The app reads the repository with its own token, so a pull request that does not answers 404 `pull_not_found`, as a missing one does, and nothing of it is returned. `opened` is the pull request's state and link. `gate` is the `skills-pr` check of its head commit: `pending`, `passed`, `failed`, `cancelled`, or `none` when it has not run. A newer push or a person cancels a check: re-run it in GitHub, or push again. `merged` holds the merge commit. `release` is the `skills-publish` run of that commit: `not_started`, `running`, `waiting_approval` (its `url` is the run, where the production approval is given in GitHub), `succeeded`, `failed` or `cancelled`. A release run is cancelled when a newer run of the release concurrency group replaced it (a newer release, a switch or a production deploy), or when a person cancelled it. An operator starts the release again with `gh workflow run skills-publish.yml --ref main`. `live` is the skill's live version from the journal, its name in Unity Catalog as `fqn`, and whether a switch came after the merge. GitHub answers are reused for a few seconds.
- **Merge.** A reviewer merges with a merge commit, and only when the pull request is open, changes only `skills/<dept>/<skill>/` paths, was opened from the Skill Hub or names only skills the reviewer may see, has `skills-pr` and `platform` passed, and is `clean` in GitHub. The app checks these itself, since the token's owner may be an administrator who could bypass branch protection. GitHub works out whether a pull request can be merged in the background, and answers `unknown` meanwhile, for example right after a push. The app then reads the pull request again, up to 3 times, a second apart. If the state is still unknown, `unmet` has `mergeable_unknown`, and the reviewer tries again in a few seconds. The merge names the head commit, so a push made after the check is not merged unseen. The app never approves the `production` environment.
- **Configuration.** `SKILLHUB_GITHUB_REPO` and `SKILLHUB_GITHUB_TOKEN_SECRET`, set by the bundle (see [GitHub token](operations.md#github-token-for-the-skill-hub)). Without them the endpoints answer 503 `github_not_configured`. A GitHub failure answers 502 `github_failed` with the error type only, never GitHub's message.

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

Each connection offers at most 20 per-skill tools and leaves out the rest without an error. An agent outside the Skill Hub therefore adds one MCP server per schema, not one for several schemas, and keeps each schema at 20 skills or fewer. The generic tools (`load_skill`, `get_skill_files`, `list_skills`) are offered on each connection.

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
