# Retention and audit

An auditor may ask, years later, why a skill version went live. The evidence must still be there: which content, evaluated how, approved by whom, and who used it. This page covers:
- what the product keeps, and where;
- what it guarantees;
- what an operator adds for a retention period of several years.

## What is kept, and where

In the paths below, `<d>` is `common`, `external` or `dept_<key>`, so a department `equity_research` has `dept_equity_research_evals`.

| Evidence | Where | Written by | How long it stays |
|---|---|---|---|
| **The release journal:** every publish, switch, retire and refusal, with the content hash, Git commit, eval run, actor and Job run | `<catalog>.<d>_evals.skill_versions` | `skillhub-ci`, through the `skill-switch` Job | Until the table is dropped. Append-only. |
| **Eval results:** one row per run, and one per case and score | `eval_runs` and `eval_cases` in each `<d>_evals` schema | `skillhub-ci-eval`, through `skillhub sync` | The same. Append-only. |
| **Rubrics**, by hash | `rubrics` in each `<d>_evals` schema | The same | The same. Append-only. |
| **Skill versions** | Unity Catalog skills `<catalog>.<d>_versions.<skill>-v<N>` | `skillhub-ci` | The product never deletes them. Retire switches the live skill away, and the versions stay. |
| **Agent traces and judge assessments** | The `traces_*` tables in each `<d>_evals` schema | MLflow, for the eval Job and the Skill Hub; the workspace's OTLP endpoint, for external agents | Until they are deleted with SQL. MLflow manages these tables. |
| **Run details** (params, metrics, tags) | The MLflow experiments under `<workspace_root>/<catalog>` | The eval Job | While the experiment exists. `skillhub sync` copies what an audit needs into `eval_runs`. |
| **The model catalog** | `<catalog>.eval.model_profiles` | The Skill Hub | The current state only: rows are updated. Each run keeps its own copy of the profile in `eval_runs` (`model_profile`, `endpoint`, `route`, `model_id`, `region`, `catalog_version`). |
| **Who did what in the workspace,** including each skill load (`aiGatewaySkills`) | `system.access.audit` | Databricks | Limited: the audit system table keeps about a year. |
| **Pull requests, approvals and commits** | GitHub | GitHub | Git history stays as long as the repository does. Actions logs and artifacts stay 90 days by default. |
| **Candidates and drafts** | `<workspace_root>/<catalog>/candidates` and `<workspace_root>/<catalog>/drafts` | The eval Job and the Skill Hub | Temporary. The nightly `skillhub cleanup` deletes them after 7 days. |
| **Run schemas** | `run_<key>_<schema_base>` | The eval Job | Temporary. `skillhub cleanup` drops them after 24 hours. |

## What the product guarantees

- **The four results tables are append-only** (`delta.appendOnly = true`). Delta refuses any `UPDATE`, `DELETE` or `MERGE` that would change a row, whoever runs it. An integration test checks this, and checks that rows survive `VACUUM`.
- **Product code never updates or deletes a row** of those tables. `skillhub sync` only inserts rows that are missing.
- **`VACUUM` never removes a current row.** It removes only data files that no current version of the table uses.
- **Time travel is not the audit trail.** It lasts only as long as Delta keeps old data files: `delta.deletedFileRetentionDuration`, 7 days by default. The rows themselves are the evidence, and they stay.

## What to add for a multi-year retention period

### 1. Keep each table's write history

`DESCRIBE HISTORY` lists every write to a table: who made it, when, and what operation it was. Delta keeps this history for `delta.logRetentionDuration`, which is 30 days by default. Set the retention period in `config/hub.yaml`. Bootstrap then sets it on the four results tables of every department, including departments added later:

```yaml
retention:
  history_days: 2555   # 7 years; leave the block out to keep Delta's default
```

The value must be between 30 and 36500. If the bootstrap principal does not own a table, bootstrap fails and prints the `ALTER TABLE` statement for the table's owner to run. Removing the block later does not reset the property.

Check it with:

```sql
SHOW TBLPROPERTIES <catalog>.common_evals.skill_versions ('delta.logRetentionDuration');
DESCRIBE HISTORY <catalog>.common_evals.skill_versions LIMIT 5;   -- userName is the writer, a principal's ID for CI
```

### 2. Copy the audit log

The audit system table keeps a limited history. Copy the events that name the hub's catalog into your own append-only table, every day. Use a scheduled SQL query or a Job:
- **The events copied** include skill loads, trace writes, judge assessments and Unity Catalog operations.
- **The two-day overlap** catches events that the system table receives late.
- **`event_id`** prevents duplicates.

```sql
-- Once:
CREATE TABLE IF NOT EXISTS <catalog>.eval.audit_log
  TBLPROPERTIES ('delta.appendOnly' = 'true')
  AS SELECT * FROM system.access.audit WHERE false;

-- Every day:
INSERT INTO <catalog>.eval.audit_log
SELECT a.* FROM system.access.audit a
WHERE a.workspace_id = <workspace id>
  AND a.event_time > (SELECT coalesce(max(event_time), TIMESTAMP '1970-01-01') FROM <catalog>.eval.audit_log)
                     - INTERVAL 2 DAYS
  AND exists(map_values(a.request_params), v -> startswith(v, '<catalog>.') OR v = '<catalog>')
  AND NOT EXISTS (SELECT 1 FROM <catalog>.eval.audit_log c WHERE c.event_id = a.event_id);
```

Reviewers hold `SELECT` on the whole `<catalog>.eval` schema, so they can read `audit_log` there. It holds workspace-wide events. To restrict it, create the table in a separate schema, for example `<catalog>.archive` as in step 3, and replace `<catalog>.eval.audit_log` with that name. The principal that runs the copy needs `CREATE TABLE` on the schema, and `MODIFY` on the table; no one else needs `MODIFY`. Grant `SELECT` on `audit_log` to your auditors. If Databricks adds a column to `system.access.audit`, the daily insert fails until you add the column to the copy with `ALTER TABLE <catalog>.eval.audit_log ADD COLUMNS (...)`.

### 3. Back up the trace tables

Don't change the properties of the `traces_*` tables: MLflow manages them. On your backup schedule, deep clone each department's trace tables into an archive schema that only the backup principal can write:

```sql
CREATE OR REPLACE TABLE <catalog>.archive.common_traces_otel_spans
  DEEP CLONE <catalog>.common_evals.traces_otel_spans;
```

A deep clone is a full, independent copy. Running it again copies only what changed since the last clone.

### 4. Keep GitHub's evidence

The journal row of each release carries the Git commit and the eval run, so the evidence chain doesn't depend on GitHub Actions logs. For a longer record of who approved each release in the `production` environment:
- raise the repository's Actions retention (Settings, Actions, General); or
- export the deployment reviews with the GitHub API.

### 5. Don't drop schemas

A dropped schema takes its tables, and their history, with it. The bootstrap principal and `skillhub-ci` own the schemas. Give `MANAGE` and ownership to no one else.
