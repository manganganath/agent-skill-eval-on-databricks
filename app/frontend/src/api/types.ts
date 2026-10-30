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
  uploaded_by: string | null; has_rubric: boolean | null; submitted_pr?: number | null;
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
    uploaded_at: string | null; uploaded_by: string | null; submitted_pr?: number | null; submitted_url?: string | null;
  };
}

// A draft upload: a zip of the skill folder as it is (base64), or the folder's files with their paths.
export type DraftIn =
  | { dept: string; archive: string }
  | { dept: string; files: { path: string; content: string }[] };
export interface DraftOut { run_key: string; dept: string; name: string; has_rubric: boolean }

// A draft opened as a pull request (POST /api/drafts/{run_key}/submit), and the steps of its release (GET
// /api/release-status). Mirrors SubmitOut, ReleaseStatusOut and MergeOut in app/backend/schemas.py.
export interface SubmitOut { pr_number: number; url: string; branch: string; notes: string[] }
export type GateState = "pending" | "passed" | "failed" | "none";
export type ReleaseState = "not_started" | "running" | "waiting_approval" | "succeeded" | "failed";
export interface ReleaseStatus {
  pr_number: number; dept: string; skill: string;
  opened: { state: "open" | "closed"; url: string };
  gate: { state: GateState; url: string };
  merged: { merged: boolean; sha: string | null; at: string | null };
  release: { state: ReleaseState; url: string | null };
  live: { fqn: string; version: number | null; switched_after_merge: boolean; eval_access: EvalAccess };
}
export interface MergeOut { pr_number: number; merged: boolean; sha: string }

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

// The Dashboard tab's aggregates. Mirrors DashboardOut in app/backend/schemas.py. Rows carry `dept` where a
// department matters; `tokens` is null when the user cannot read the system.serving tables.
export interface DashboardSummary {
  skills: number; verdicts: Record<Verdict, number>; trigger_accuracy: number | null;
  uplift_task_quality: number | null; error_rate: number | null;
}
export interface DashboardVerdict { dept: string; model_profile: string; verdict: Verdict; n: number }
export interface DashboardUplift {
  dept: string; skill: string; skill_version: number; model_profile: string; pass_rate_without: number | null;
  pass_rate: number | null; uplift_task_quality: number | null; uplift_skill_format: number | null;
}
export interface DashboardRouting {
  dept: string; model_profile: string; expected_skill: string | null; invoked_skill: string | null;
  trigger_outcome: string | null; n: number;
}
export interface DashboardTriggerTrend {
  dept: string; skill: string; skill_version: number; model_profile: string; day: string; accuracy: number | null; runs: number;
}
export interface DashboardPassRateTrend {
  dept: string; skill: string; skill_version: number; model_profile: string; day: string; pass_rate: number | null; runs: number;
}
export interface DashboardHealth {
  dept: string; model_profile: string; cells: number; error_rate: number | null; inconclusive_rate: number | null;
}
// `eval`: evaluation runs and their judges. `try`: Try runs, which call with the signed-in user's token.
export type TokenSource = "eval" | "try";
export interface DashboardTokens {
  model_profile: string; endpoint_name: string; day: string; source: TokenSource;
  input_tokens: number | null; output_tokens: number | null;
}
export interface DashboardOut {
  summary: DashboardSummary; verdicts: DashboardVerdict[]; uplift: DashboardUplift[]; routing: DashboardRouting[];
  trigger_trend: DashboardTriggerTrend[]; pass_rate_trend: DashboardPassRateTrend[]; health: DashboardHealth[];
  tokens: DashboardTokens[] | null;
}
/** The dashboard and the departments left out of it (X-Eval-Denied-Departments). */
export interface Dashboard extends DashboardOut { deniedDepartments: string[] }

export interface AppConfig {
  workspace_url: string;
  models: Record<string, "frontier" | "fast">;
  departments: string[];
  // Always sent by the API; optional here so older mocks and callers stay valid.
  roles?: { authors: string; reviewers: string };
  department_access?: Record<string, { readers: string[]; quarantine: boolean }>;
  verdict_rules?: VerdictRules;
}
// config defaults: rates are 0..1; gating_modes are request modes ("trigger", "effectiveness").
export interface VerdictRules {
  fail_rate: number; warn_rate: number; max_error_rate: number; min_reviewed_cases: number; pass_score: number;
  gating_modes: string[];
}
export interface ReadableDepartments { departments: string[] }

// The two evaluation modes, as the API accepts them. The UI lists them from here.
export const MODES: Mode[] = ["trigger", "effectiveness"];
