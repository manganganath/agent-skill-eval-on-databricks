import { ApiError } from "../api/client";

const MESSAGES: Record<string, string> = {
  no_user_token: "Sign in again. Skill Hub did not receive your session.",
  skill_not_readable: "You do not have access to this skill. Ask the skill's owner for access.",
  skill_not_found: "This skill was not found, or you do not have access to it.",
  not_reviewer: "Only skill reviewers can do this.",
  evals_not_readable: "You do not have access to the evaluation results for this department.",
  authz_check_failed: "Skill Hub could not confirm your access, so it refused this request. Try again later.",
  job_forbidden: "Skill Hub is not allowed to start this job. Ask an administrator.",
  job_not_found: "The job for this action is not set up in this workspace. Ask an administrator to deploy Skill Hub's jobs.",
  job_failed: "Skill Hub could not start or check the job. Try again later, or ask an administrator.",
  job_conflict: "Another run is already in progress. Try again shortly.",
  run_not_found: "That run was not found, or you cannot view it.",
  switch_in_progress: "A version switch is already running for this skill.",
  finish_or_revert_switch:
    "The last switch of this skill did not finish. Run that switch again, or switch back to the version it started from.",
  already_live: "This version is already live.",
  version_not_found: "That version does not exist.",
  version_not_requested: "A pinned version belongs to a skill that is not selected. Select the skill, or clear its version.",
  department_not_found: "That department is not set up in Skill Hub.",
  no_access: "You do not have access to these results.",
  results_unavailable: "These results do not exist yet.",
  invalid_run_id: "That run ID is not valid.",
  sql_failed: "Skill Hub could not read the results from the SQL warehouse. Try again later.",
  uc_failed: "Skill Hub could not read this skill from Unity Catalog. Try again later.",
  try_unavailable: "Trying skills is not available right now.",
  try_failed: "The test run failed. Try again later.",
  try_busy: "Another test run is in progress. Try again in a moment.",
  try_timeout: "The test run took too long and was stopped. Try a shorter input.",
  try_setup_required:
    "Trying skills under review needs a one-time setup: an administrator grants the Skill Hub app CREATE SCHEMA on the catalog. See the operations guide.",
  prompt_too_long: "The test input is too long. Keep it under 4000 characters.",
  too_many_models: "Pick at most 3 models.",
  unknown_model: "One of the selected models is not configured.",
  not_author: "Only skill authors and reviewers can do this.",
  candidate_not_found:
    "This candidate was not found, or you do not have access to it. Candidate uploads are removed 7 days after upload.",
  candidate_unreadable: "Skill Hub could not read the candidate skills. Try again later.",
  draft_invalid: "The draft was not uploaded. Fix the problems listed and upload it again.",
  draft_too_large: "The draft is too large. A draft may hold up to 200 files and 4 MB once unpacked.",
  draft_store_failed: "Skill Hub could not save the draft. Try again later, or ask an administrator to check the drafts folder.",
  department_not_allowed: "You can upload drafts only to departments whose live skills you can read.",
  rubric_required: "This skill has no rubric, so it can run in trigger mode only. Clear effectiveness and try again.",
  not_draft_owner: "Only the person who uploaded this draft, or a reviewer, can discard it or submit it for review.",
  github_not_configured:
    "Pull requests are not set up for the Skill Hub yet. An administrator sets the GitHub repository and token secret. See the operations guide.",
  github_failed: "Skill Hub could not reach GitHub, or GitHub refused the request. Try again later.",
  already_submitted: "This draft was already submitted for review. Its pull request is listed on this page.",
  no_changes:
    "main already has this skill exactly as the draft has it, so a pull request would change nothing. If it is waiting for release, follow its pull request.",
  evals_missing: "This draft has no test cases (evals/evals.json), so it cannot be submitted. Upload it again with its evals.",
  pull_not_found: "That pull request was not found in the repository.",
  not_mergeable:
    "This pull request cannot be merged from the Skill Hub yet. It must be open, change only skills, pass its checks, and be up to date with main. If main has moved, update the branch in GitHub; the gate then runs again.",
  mixed_sources:
    "Evaluate live skills and candidates in separate runs, with candidates from one upload only. A candidate run cannot pin versions.",
};

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const reason = error instanceof ApiError ? error.reason : undefined;
  const message =
    (reason && MESSAGES[reason]) ?? "Something went wrong. Try again.";
  return (
    <div role="alert" className="error">
      <p>{message}</p>
      {reason && <p className="muted">Reason code: <code>{reason}</code></p>}
      {onRetry && <button type="button" onClick={onRetry}>Try again</button>}
    </div>
  );
}
