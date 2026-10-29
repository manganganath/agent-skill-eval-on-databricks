import { ApiError } from "../api/client";

const MESSAGES: Record<string, string> = {
  no_user_token: "Sign in again. Skill Hub did not receive your session.",
  skill_not_readable: "You do not have access to this skill. Ask the skill's owner for access.",
  skill_not_found: "This skill was not found, or you do not have access to it.",
  not_reviewer: "Only skill reviewers can do this.",
  evals_not_readable: "You do not have access to the evaluation results for this department.",
  authz_check_failed: "Skill Hub could not confirm your access, so it refused this request. Try again later.",
  job_forbidden: "Skill Hub is not allowed to start this job. Ask an administrator.",
  job_conflict: "Another run is already in progress. Try again shortly.",
  switch_in_progress: "A version switch is already running for this skill.",
  already_live: "This version is already live.",
  version_not_found: "That version does not exist.",
  try_unavailable: "Trying skills is not available right now.",
  try_failed: "The test run failed. Try again later.",
  try_busy: "Another test run is in progress. Try again in a moment.",
  try_timeout: "The test run took too long and was stopped. Try a shorter input.",
  prompt_too_long: "The test input is too long. Keep it under 4000 characters.",
  too_many_models: "Pick at most 3 models.",
  unknown_model: "One of the selected models is not configured.",
};

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const reason = error instanceof ApiError ? error.reason : undefined;
  const message =
    (reason && MESSAGES[reason]) ?? "Something went wrong while loading this page. Try again.";
  return (
    <div role="alert" className="error">
      <p>{message}</p>
      {reason && <p className="muted">Reason code: {reason}</p>}
      {onRetry && <button type="button" onClick={onRetry}>Try again</button>}
    </div>
  );
}
