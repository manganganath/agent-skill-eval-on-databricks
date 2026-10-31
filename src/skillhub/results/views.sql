CREATE OR REPLACE VIEW {evals}.eval_matrix AS
WITH ranked AS (
  SELECT r.*,
         ROW_NUMBER() OVER (PARTITION BY skill, skill_version, model_profile, COALESCE(harness, 'reference'), mode
                            ORDER BY started_at DESC, run_id DESC) AS rn
  FROM {evals}.eval_runs AS r
  WHERE r.parent_run_id IS NOT NULL
)
SELECT run_id, dept, skill, skill_fqn, skill_version, content_hash, model_profile,
       COALESCE(harness, 'reference') AS harness, endpoint,
       model_class, mode, verdict, pass_rate, spread, error_rate, n_cases, n_reviewed,
       n_cases - n_reviewed AS unreviewed_cases,
       accuracy, `precision`, recall, pass_rate_without, uplift_task_quality,
       uplift_skill_format, planted_bugs_recall, started_at, run_key, trigger_source, experiment_id,
       COALESCE(profile_kind, 'hub_yaml') AS profile_kind
FROM ranked
WHERE rn = 1;
