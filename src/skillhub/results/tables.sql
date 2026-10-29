CREATE TABLE IF NOT EXISTS {evals}.skill_versions (
  dept STRING, skill STRING, action STRING, version INT, from_version INT, to_version INT,
  versioned_fqn STRING, content_hash STRING, git_sha STRING, eval_run_id STRING,
  actor STRING, job_run_id STRING, ts TIMESTAMP)
TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS {evals}.eval_runs (
  run_id STRING, parent_run_id STRING, run_key STRING, experiment_id STRING,
  dept STRING, skill STRING, skill_fqn STRING, skill_version INT, content_hash STRING,
  rubric_hash STRING, dataset_digest STRING, model_profile STRING, harness STRING, endpoint STRING, model_class STRING,
  mode STRING, judges STRING, prompt_versions STRING, git_sha STRING, trigger_source STRING, requested_by STRING,
  verdict STRING, pass_rate DOUBLE, spread DOUBLE, error_rate DOUBLE, n_cases INT, n_reviewed INT,
  accuracy DOUBLE, `precision` DOUBLE, recall DOUBLE, pass_rate_without DOUBLE,
  uplift_task_quality DOUBLE, uplift_skill_format DOUBLE, planted_bugs_recall DOUBLE,
  started_at TIMESTAMP, ended_at TIMESTAMP, synced_at TIMESTAMP, run_author STRING)
TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS {evals}.eval_cases (
  run_id STRING, trace_id STRING, assessment_name STRING, case_id INT, repeat INT, mode STRING,
  user_input STRING, expected_skill STRING, invoked_skill STRING, trigger_outcome STRING,
  value STRING, rationale STRING, judge_model STRING, source_type STRING, reviewed BOOLEAN, created_at TIMESTAMP)
TBLPROPERTIES ('delta.appendOnly' = 'true');

CREATE TABLE IF NOT EXISTS {evals}.rubrics (
  skill STRING, rubric_hash STRING, rubric_json STRING, source_hash STRING, reviewed BOOLEAN, ts TIMESTAMP)
TBLPROPERTIES ('delta.appendOnly' = 'true');
