#!/usr/bin/env bash
# Configure GitHub for this repository: branch protection on main and the `production` and `ci-eval`
# environments, with their secrets and variables.
#
# Usage:
#   GH_TOKEN=$(gh auth token --user OWNER_LOGIN) scripts/configure_github.sh --repo OWNER/NAME \
#     --host-file F --warehouse-id-file F \
#     --ci-client-id-file F --ci-client-secret-file F \
#     --ci-eval-client-id-file F --ci-eval-client-secret-file F \
#     --ci-principal ID --ci-eval-principal ID --app-principal ID --app-principal-dev ID \
#     --catalog NAME [--workspace-root PATH] [--skill-hub-url URL] [--reviewer LOGIN] [--pr-links true|false] \
#     [--alert-destination-id ID] \
#     [--job-id-eval N --job-id-switch N] [--skip-org-check] [--dry-run]
#
# PR comments: the sticky eval comment carries no workspace host, run or experiment ID and no Skill Hub
# URL, because the repository can be public. --pr-links true sets the repository variable
# SKILLHUB_PR_LINKS so the comment links each row to MLflow and the Skill Hub (private repositories only).
#
# Workspace root: experiments and CI candidates live under PATH (SKILLHUB_WORKSPACE_ROOT, set in both
# environments). The default is the ci principal's home folder, /Users/<ci principal>/skill-hub: every folder
# under /Workspace/Shared inherits `users` CAN_MANAGE, so the Skill Hub never uses /Shared.
#
# Account: the script uses whatever `gh` credentials are active, and honours GH_TOKEN. It never runs
# `gh auth switch`. Pass GH_TOKEN as shown above when the active account cannot administer the repo.
#
# Secrets are read from files given by path (use 0600 files, e.g. the bootstrap secret outputs) and are
# passed to `gh secret set` on stdin. They are never passed as arguments and never printed.
#
# Owner type: when OWNER is an Organization, --reviewer LOGIN is required and the organization secrets
# must be readable (otherwise the guard cannot be verified and the script fails). --skip-org-check
# overrides that when you have verified by hand that no org-level DATABRICKS_* or SQL_WAREHOUSE_ID
# secret exists. For a User owner the org check is skipped.
#
# Idempotent: branch protection is a PUT of the full state, secrets and variables are overwritten, an
# existing environment or branch policy is left as it is.
#
# Single maintainer: main requires the checks `skills-pr` and `platform`, 0 approving reviews, and
# enforce_admins is false. The `production` reviewer is the owner and self-review is allowed. To
# raise the review count once a second reviewer exists, change in the protection body below
#   "required_approving_review_count": 0  ->  1
#   "require_code_owner_reviews": false   ->  true      (.github/CODEOWNERS already names the owner)
# optionally set "enforce_admins": true and "prevent_self_review" to true, then re-run the script.
#
# --dry-run prints every mutating gh command with secrets redacted and makes none of them. Read-only
# lookups (secret list for the guards) still run.
set -euo pipefail

REPO=""
REVIEWER=""
DRY_RUN=0
SKIP_ORG_CHECK=0
HOST_FILE="" WAREHOUSE_FILE="" CI_ID_FILE="" CI_SECRET_FILE="" EVAL_ID_FILE="" EVAL_SECRET_FILE=""
CI_PRINCIPAL="" CI_EVAL_PRINCIPAL="" APP_PRINCIPAL="" APP_PRINCIPAL_DEV=""
CATALOG="" SKILL_HUB_URL="" JOB_ID_EVAL="" JOB_ID_SWITCH="" PR_LINKS="" ALERT_DESTINATION_ID="" WORKSPACE_ROOT=""

die() { echo "error: $*" >&2; exit 1; }
usage() { echo "usage: $0 --repo OWNER/NAME [--dry-run] (see the header of this script for all options)" >&2; exit 2; }

while [ "$#" -gt 0 ]; do
  [ "$#" -ge 2 ] || [ "$1" = "--dry-run" ] || [ "$1" = "--skip-org-check" ] || usage
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    --reviewer) REVIEWER="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --skip-org-check) SKIP_ORG_CHECK=1; shift ;;
    --host-file) HOST_FILE="$2"; shift 2 ;;
    --warehouse-id-file) WAREHOUSE_FILE="$2"; shift 2 ;;
    --ci-client-id-file) CI_ID_FILE="$2"; shift 2 ;;
    --ci-client-secret-file) CI_SECRET_FILE="$2"; shift 2 ;;
    --ci-eval-client-id-file) EVAL_ID_FILE="$2"; shift 2 ;;
    --ci-eval-client-secret-file) EVAL_SECRET_FILE="$2"; shift 2 ;;
    --ci-principal) CI_PRINCIPAL="$2"; shift 2 ;;
    --ci-eval-principal) CI_EVAL_PRINCIPAL="$2"; shift 2 ;;
    --app-principal) APP_PRINCIPAL="$2"; shift 2 ;;
    --app-principal-dev) APP_PRINCIPAL_DEV="$2"; shift 2 ;;
    --catalog) CATALOG="$2"; shift 2 ;;
    --workspace-root) WORKSPACE_ROOT="$2"; shift 2 ;;
    --skill-hub-url) SKILL_HUB_URL="$2"; shift 2 ;;
    --job-id-eval) JOB_ID_EVAL="$2"; shift 2 ;;
    --job-id-switch) JOB_ID_SWITCH="$2"; shift 2 ;;
    --pr-links) PR_LINKS="$2"; shift 2 ;;
    --alert-destination-id) ALERT_DESTINATION_ID="$2"; shift 2 ;;
    *) usage ;;
  esac
done

[[ "$REPO" =~ ^[^/[:space:]]+/[^/[:space:]]+$ ]] || usage
OWNER="${REPO%%/*}"
REVIEWER_GIVEN="$REVIEWER"
REPO_PATH="repos/${REPO}"

for pair in "--host-file:$HOST_FILE" "--warehouse-id-file:$WAREHOUSE_FILE" "--ci-client-id-file:$CI_ID_FILE" \
  "--ci-client-secret-file:$CI_SECRET_FILE" "--ci-eval-client-id-file:$EVAL_ID_FILE" \
  "--ci-eval-client-secret-file:$EVAL_SECRET_FILE"; do
  [ -n "${pair#*:}" ] || die "${pair%%:*} is required"
  [ -s "${pair#*:}" ] || die "${pair%%:*}: file '${pair#*:}' is missing or empty"
done
for pair in "--ci-principal:$CI_PRINCIPAL" "--ci-eval-principal:$CI_EVAL_PRINCIPAL" \
  "--app-principal:$APP_PRINCIPAL" "--app-principal-dev:$APP_PRINCIPAL_DEV" "--catalog:$CATALOG"; do
  [ -n "${pair#*:}" ] || die "${pair%%:*} is required"
done

OWNER_TYPE="$(gh api "users/${OWNER}" --jq .type)" || die "cannot look up the owner '${OWNER}'"
if [ "$OWNER_TYPE" = "Organization" ] && [ -z "$REVIEWER_GIVEN" ]; then
  die "the owner '${OWNER}' is an Organization: --reviewer LOGIN is required"
fi
REVIEWER="${REVIEWER_GIVEN:-$OWNER}"

# Two different credentials: the ci and ci-eval client IDs must differ.
[ "$(tr -d '[:space:]' < "$CI_ID_FILE")" != "$(tr -d '[:space:]' < "$EVAL_ID_FILE")" ] ||
  die "the ci and ci-eval client-ID files hold the same client ID"

# ---- Guards (all run before anything is changed) ----
case "$PR_LINKS" in
  ""|true|false) ;;
  *) die "--pr-links must be true or false" ;;
esac
if [ -n "$ALERT_DESTINATION_ID" ] && ! [[ "$ALERT_DESTINATION_ID" =~ ^[0-9a-fA-F-]{36}$ ]]; then
  die "--alert-destination-id must be a notification destination ID"
fi
WORKSPACE_ROOT="${WORKSPACE_ROOT:-/Users/${CI_PRINCIPAL}/skill-hub}"
WORKSPACE_ROOT="${WORKSPACE_ROOT%/}"
WORKSPACE_ROOT="${WORKSPACE_ROOT#/Workspace}"
if ! [[ "$WORKSPACE_ROOT" =~ ^(/[A-Za-z0-9@._+-]+)+$ ]] || [[ "$WORKSPACE_ROOT/" == *"/../"* || "$WORKSPACE_ROOT/" == *"/./"* ]]; then
  die "--workspace-root must be an absolute workspace folder such as /Users/<ci principal>/skill-hub"
fi
case "$WORKSPACE_ROOT" in
  /Shared|/Shared/*) die "--workspace-root must not be under /Shared: every folder there inherits users CAN_MANAGE" ;;
esac
for pair in "SKILLHUB_JOB_ID_EVAL:$JOB_ID_EVAL" "SKILLHUB_JOB_ID_SWITCH:$JOB_ID_SWITCH"; do
  if [ -n "${pair#*:}" ] && ! [[ "${pair#*:}" =~ ^[0-9]+$ ]]; then
    die "${pair%%:*} must be numeric (got '${pair#*:}')"
  fi
done

EVAL_CLIENT_ID="$(tr -d '[:space:]' < "$EVAL_ID_FILE")"
[ "$EVAL_CLIENT_ID" != "$(printf '%s' "$CI_PRINCIPAL" | tr -d '[:space:]')" ] ||
  die "the ci-eval client ID equals CI_PRINCIPAL: ci-eval must use the separate ci_eval principal"

forbidden='^(DATABRICKS_.*|SQL_WAREHOUSE_ID)$'
repo_secrets="$(gh secret list --repo "$REPO" --json name --jq '.[].name')" ||
  die "cannot list repository secrets for $REPO; cannot verify the guard"
if grep -Eq "$forbidden" <<<"$repo_secrets"; then
  die "repository-level DATABRICKS_* or SQL_WAREHOUSE_ID secret exists in $REPO; delete it (credentials live only in environments)"
fi
if [ "$OWNER_TYPE" = "Organization" ]; then
  if [ "$SKIP_ORG_CHECK" -eq 1 ]; then
    echo "note: organization secret check skipped by --skip-org-check" >&2
  else
    org_secrets="$(gh secret list --org "$OWNER" --json name --jq '.[].name')" ||
      die "cannot read the organization secrets of '${OWNER}'; cannot verify the guard (use --skip-org-check only after checking by hand)"
    if grep -Eq "$forbidden" <<<"$org_secrets"; then
      die "organization-level DATABRICKS_* or SQL_WAREHOUSE_ID secret exists in ${OWNER}; delete that org secret (excluding this repo from it is not enough)"
    fi
  fi
fi

# ---- Helpers ----
# mutate: run a gh command, or print it under --dry-run.
mutate() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf 'DRY-RUN: gh'; printf ' %q' "$@"; printf '\n'
  else
    gh "$@"
  fi
}
# mutate_json BODY args...: send BODY on stdin (--input -). Bodies never contain secrets.
mutate_json() {
  local body="$1"; shift
  if [ "$DRY_RUN" -eq 1 ]; then
    printf 'DRY-RUN: gh'; printf ' %q' "$@"; printf ' --input - <<< %s\n' "$body"
  else
    printf '%s' "$body" | gh "$@" --input - >/dev/null
  fi
}
# set_secret ENV NAME FILE: the value goes from the file to stdin, never through arguments or output.
set_secret() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf 'DRY-RUN: gh secret set %s --env %s --repo %s < %s  (value redacted)\n' "$2" "$1" "$REPO" "$3"
  else
    tr -d '\r\n' < "$3" | gh secret set "$2" --env "$1" --repo "$REPO"
  fi
}
set_var() { mutate variable set "$2" --env "$1" --repo "$REPO" --body "$3"; }

# ---- Environments ----
if [ "$DRY_RUN" -eq 1 ]; then
  REVIEWER_ID="<id-of-${REVIEWER}>"
else
  REVIEWER_ID="$(gh api "users/${REVIEWER}" --jq .id)"
fi
[[ "$REVIEWER_ID" =~ ^[0-9]+$ ]] || [ "$DRY_RUN" -eq 1 ] || die "cannot resolve GitHub user '${REVIEWER}'"

# production: owner is the required reviewer (self-review allowed), deployments only from main.
mutate_json "{\"reviewers\": [{\"type\": \"User\", \"id\": ${REVIEWER_ID}}], \"prevent_self_review\": false, \"deployment_branch_policy\": {\"protected_branches\": false, \"custom_branch_policies\": true}}" \
  api -X PUT "${REPO_PATH}/environments/production"
existing_policies=""
if [ "$DRY_RUN" -eq 0 ]; then
  existing_policies="$(gh api "${REPO_PATH}/environments/production/deployment-branch-policies" --jq '.branch_policies[] | "\(.type):\(.name)"')"
fi
if [ -n "$existing_policies" ] && [ "$(grep -vFx 'branch:main' <<<"$existing_policies" || true)" != "" ]; then
  die "production has deployment branch policies other than the branch 'main'; remove them first"
fi
if grep -Fxq 'branch:main' <<<"$existing_policies"; then
  echo "production: branch policy 'main' already present"
else
  mutate_json '{"name": "main", "type": "branch"}' api -X POST "${REPO_PATH}/environments/production/deployment-branch-policies"
fi

# ci-eval: no reviewers.
mutate_json '{"reviewers": []}' api -X PUT "${REPO_PATH}/environments/ci-eval"

# ---- Secrets (stdin from files) ----
set_secret production DATABRICKS_HOST "$HOST_FILE"
set_secret production DATABRICKS_CLIENT_ID "$CI_ID_FILE"
set_secret production DATABRICKS_CLIENT_SECRET "$CI_SECRET_FILE"
set_secret production SQL_WAREHOUSE_ID "$WAREHOUSE_FILE"
set_secret ci-eval DATABRICKS_HOST "$HOST_FILE"
set_secret ci-eval DATABRICKS_CLIENT_ID "$EVAL_ID_FILE"
set_secret ci-eval DATABRICKS_CLIENT_SECRET "$EVAL_SECRET_FILE"
set_secret ci-eval SQL_WAREHOUSE_ID "$WAREHOUSE_FILE"

# ---- Variables ----
set_var production CI_PRINCIPAL "$CI_PRINCIPAL"
set_var production CI_EVAL_PRINCIPAL "$CI_EVAL_PRINCIPAL"
set_var production APP_PRINCIPAL "$APP_PRINCIPAL"
set_var production APP_PRINCIPAL_DEV "$APP_PRINCIPAL_DEV"
set_var production SKILLHUB_CATALOG "$CATALOG"
set_var ci-eval SKILLHUB_CATALOG "$CATALOG"
# Experiments and CI candidates: the deploy passes it to the bundle, and the PR and merge evals upload there.
set_var production SKILLHUB_WORKSPACE_ROOT "$WORKSPACE_ROOT"
set_var ci-eval SKILLHUB_WORKSPACE_ROOT "$WORKSPACE_ROOT"
# The PR and merge gates read only rows from runs by this principal.
set_var ci-eval CI_EVAL_PRINCIPAL "$CI_EVAL_PRINCIPAL"
if [ -n "$PR_LINKS" ]; then
  mutate variable set SKILLHUB_PR_LINKS --repo "$REPO" --body "$PR_LINKS"
fi
if [ -n "$SKILL_HUB_URL" ]; then
  set_var production SKILL_HUB_URL "$SKILL_HUB_URL"
  set_var ci-eval SKILL_HUB_URL "$SKILL_HUB_URL"
fi
# SQL alerts and failed skill-switch and nightly runs notify this destination (prod deploy).
if [ -n "$ALERT_DESTINATION_ID" ]; then
  set_var production SKILLHUB_ALERT_DESTINATION_ID "$ALERT_DESTINATION_ID"
fi
if [ -n "$JOB_ID_EVAL" ]; then
  set_var production SKILLHUB_JOB_ID_EVAL "$JOB_ID_EVAL"
  set_var ci-eval SKILLHUB_JOB_ID_EVAL "$JOB_ID_EVAL"
fi
if [ -n "$JOB_ID_SWITCH" ]; then
  set_var production SKILLHUB_JOB_ID_SWITCH "$JOB_ID_SWITCH"
fi

# ---- Branch protection on main (PUT replaces the full state) ----
# The required checks must come from GitHub Actions (app 15368), so a commit status posted by hand under the
# same name does not satisfy them.
mutate_json '{"required_status_checks": {"strict": true, "checks": [{"context": "skills-pr", "app_id": 15368}, {"context": "platform", "app_id": 15368}]}, "enforce_admins": false, "required_pull_request_reviews": {"required_approving_review_count": 0, "require_code_owner_reviews": false, "dismiss_stale_reviews": true}, "restrictions": null, "allow_force_pushes": false, "allow_deletions": false}' \
  api -X PUT "${REPO_PATH}/branches/main/protection"

cat <<CHECKLIST

Configured ${REPO}$([ "$DRY_RUN" -eq 1 ] && echo " (dry run, nothing changed)").

Remaining manual steps:
  1. Databricks workspace (not GitHub): grant the principal 'ci' the Service Principal User role on the
     'ci_eval' principal, and no other role on either principal. Do this in the bootstrap or in the
     workspace admin settings.
  2. After the first prod deploy, fetch the Job IDs with the 'ci' credentials:
       databricks bundle summary -t prod -o json
     Read the ids of the jobs 'skill_eval' and 'skill_switch', then re-run this script with
       --job-id-eval <N> --job-id-switch <N>
     (sets SKILLHUB_JOB_ID_EVAL in both environments and SKILLHUB_JOB_ID_SWITCH in production).
  3. Branch protection requires the checks 'skills-pr' and 'platform'; each must have reported once
     before GitHub accepts them.
  4. The 'production' environment is restricted to main, so the manual dev deploy only runs from main.
CHECKLIST
