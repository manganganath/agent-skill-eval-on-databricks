#!/usr/bin/env bash
# Waits for a Databricks Job run that was started with `databricks jobs run-now --no-wait`.
# Usage: wait_job_run.sh <run_id> <timeout_minutes>
# The CI runner is a laptop and its network drops for minutes at a time. The CLI's own wait
# (`run-now --timeout`) fails on the first failed poll, although the run itself carries on. Here a failed
# poll is retried, so only a run that stays unreachable (or overruns the time limit) fails the step.
# Never starts or cancels a run. Leaves the last get-run JSON in $RUNNER_TEMP/run.json and exits 0 once
# the run ends in any way: the caller checks the result state.
# Optional env: JOB_ID (for the Job URL), DATABRICKS_HOST (for URLs only),
#   WAIT_POLL_SECONDS (30), WAIT_MAX_FAILED_POLLS (20, consecutive), WAIT_TIMEOUT_SECONDS (overrides minutes; tests).
set -euo pipefail

run_id="${1:?usage: wait_job_run.sh <run_id> <timeout_minutes>}"
minutes="${2:?usage: wait_job_run.sh <run_id> <timeout_minutes>}"
[[ "$run_id" =~ ^[0-9]+$ ]] || { echo "::error::The run ID must be numeric."; exit 1; }
[[ "$minutes" =~ ^[0-9]+$ ]] || { echo "::error::The time limit must be whole minutes."; exit 1; }

interval="${WAIT_POLL_SECONDS:-30}"
max_failed="${WAIT_MAX_FAILED_POLLS:-20}"
limit="${WAIT_TIMEOUT_SECONDS:-$((minutes * 60))}"
out="${RUNNER_TEMP:?}/run.json"
tmp="$out.poll"

# Until the first answer there is no run_page_url, so name the run by the URL that the page has anyway.
url="${DATABRICKS_HOST:-}/jobs/${JOB_ID:+$JOB_ID/}runs/$run_id"
fallback=$url
echo "Waiting for run $run_id: $url"

failed=0
start=$SECONDS
while :; do
  # stdin from /dev/null: a caller's `while read` loop must keep its rows.
  if databricks jobs get-run "$run_id" -o json > "$tmp" </dev/null 2>/dev/null \
    && state=$(jq -er '.state.life_cycle_state | strings' "$tmp" 2>/dev/null); then
    failed=0
    mv "$tmp" "$out"
    url=$(jq -r '.run_page_url // empty' "$out") || true
    url="${url:-$fallback}"
    case "$state" in
      TERMINATED | SKIPPED | INTERNAL_ERROR)
        echo "Job run: $url"
        exit 0
        ;;
    esac
  else
    failed=$((failed + 1))
    echo "Poll of run $run_id failed ($failed of $max_failed); retrying."
    if [ "$failed" -ge "$max_failed" ]; then
      echo "::error::No answer for run $run_id after $failed polls. The run may still be going: $url"
      exit 1
    fi
  fi
  if [ $((SECONDS - start)) -ge "$limit" ]; then
    echo "::error::Run $run_id did not finish within ${minutes} min. It was not cancelled and may still be going: $url"
    exit 1
  fi
  sleep "$interval"
done
