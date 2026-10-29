// Mirrors the API: the Pydantic models in app/backend/schemas.py and the columns in app/backend/queries.py.
// Keep them in step.
export type Verdict = "pass" | "warn" | "fail" | "inconclusive" | "advisory";
// Request modes (what a caller asks to run) versus the mode recorded on a result row.
export type Mode = "trigger" | "effectiveness";
export type RunMode = "trigger" | "with_skill" | "without_skill";

export interface Me { user: string; groups: string[]; is_reviewer: boolean; is_author: boolean }

export type EvalAccess = "ok" | "denied" | "unavailable";

export interface SkillSummary {
  fqn: string; dept: string; name: string; description: string | null;
  live_version: number | null; verdicts: Record<string, Verdict>;
  eval_access: EvalAccess;
}

export interface Criterion {
  id: string; text: string; type: "task_quality" | "skill_format"; weight: number; source: string;
}
export interface Rubric {
  skill_name: string; source_hash: string; generated_by: string; pass_score: number;
  reviewed: boolean; criteria: Criterion[];
}
export interface EvalCase {
  id: number; kind: "trigger" | "effectiveness"; prompt: string; should_trigger: boolean;
  expected_skill: string | null; assertions: string[]; fixtures: string[];
  origin: "generated" | "human"; reviewed: boolean;
}
export interface EvalFile { skill_name: string; evals: EvalCase[] }

export interface SkillDetail {
  fqn: string; dept: string; name: string; description: string | null;
  live_version: number | null; verdicts: Record<string, Verdict>;
  files: Record<string, string>; evals: EvalFile | null; rubric: Rubric | null;
  eval_access: EvalAccess;
}

// A skill that is not released: under review in a pull request ("pr"), or a Skill Hub draft ("draft").
// CandidateSummary in app/backend/schemas.py. A draft has no pull request or commit.
export type CandidateKind = "pr" | "draft";
export interface CandidateSummary {
  kind: CandidateKind; run_key: string; pr_number: number | null; git_sha: string | null; uploaded_at: string | null;
  uploaded_by: string | null; has_rubric: boolean | null;
  dept: string; name: string; description: string | null; content_hash: string | null;
  verdicts: Record<string, Verdict>; eval_access: EvalAccess;
}

// One eval run of a candidate: its pull request's eval, or a Skill Hub run started for it.
export interface CandidateResult {
  run_key: string; trigger_source: string | null; skill: string; model_profile: string; mode: RunMode;
  verdict: Verdict | null; run_id: string | null; experiment_id: string | null; n_cases: number | null;
  pass_rate: number | null; error_rate: number | null; accuracy: number | null; content_hash: string | null;
  started_at: string | null;
}

export interface CandidateDetail {
  dept: string; name: string; description: string | null; content_hash: string;
  verdicts: Record<string, Verdict>; files: Record<string, string>; evals: EvalFile | null; rubric: Rubric | null;
  eval_access: EvalAccess; results: CandidateResult[];
  candidate: {
    kind: CandidateKind; pr_number: number | null; run_key: string; git_sha: string | null;
    uploaded_at: string | null; uploaded_by: string | null;
  };
}

// A draft upload: a zip of the skill folder as it is (base64), or the folder's files with their paths.
export type DraftIn =
  | { dept: string; archive: string }
  | { dept: string; files: { path: string; content: string }[] };
export interface DraftOut { run_key: string; dept: string; name: string; has_rubric: boolean }

export interface JournalRow {
  dept: string; skill: string; action: string; version: number | null; from_version: number | null;
  to_version: number | null; versioned_fqn: string | null; content_hash: string | null;
  git_sha: string | null; eval_run_id: string | null; actor: string; job_run_id: string | null; ts: string;
}

export interface MatrixRow {
  dept: string; skill: string; skill_version: number; content_hash: string; model_profile: string;
  harness?: string; endpoint: string; model_class: string; mode: RunMode; verdict: Verdict; run_id: string; experiment_id: string;
  n_cases: number; n_reviewed: number; pass_rate: number | null; spread: number | null; error_rate: number | null;
  accuracy: number | null; precision: number | null; recall: number | null; pass_rate_without: number | null;
  uplift_task_quality: number | null; uplift_skill_format: number | null; started_at: string;
}

export interface CaseRow {
  run_id: string; trace_id: string; assessment_name: string; case_id: number; repeat: number; mode: RunMode;
  user_input: string; expected_skill: string | null; invoked_skill: string | null; trigger_outcome: string | null;
  value: string | null; rationale: string | null; judge_model: string | null; source_type: string | null;
  reviewed: boolean; dept: string;
}

// A candidate run names one candidate upload and its skills as dept/name; live skills and versions run separately.
export interface CandidateRunIn { run_key: string; skills: string[] }
export interface EvalRunIn {
  skills?: string[]; models: string[]; modes: Mode[]; versions?: Record<string, number>; candidate?: CandidateRunIn;
}
export interface EvalRunOut { run_key: string; job_run_id: number }
export interface EvalRunStatus { state: string; result_state: string | null; run_key: string }
export interface SwitchOut { job_run_id: number }

export interface TryIn { fqn: string; prompt: string; models: string[] }
export interface TryCandidateIn { prompt: string; models: string[] }
export interface TryModelResult {
  model: string;
  with_skill: { text: string; invoked: boolean };
  without_skill: { text: string };
  trace_url: string | null;
}

export type TryOut = TryModelResult[];

export interface AppConfig {
  workspace_url: string;
  models: Record<string, "frontier" | "fast">;
  departments: string[];
}

// The two evaluation modes, as the API accepts them. The UI lists them from here.
export const MODES: Mode[] = ["trigger", "effectiveness"];
