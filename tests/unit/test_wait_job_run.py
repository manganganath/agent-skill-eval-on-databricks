"""scripts/wait_job_run.sh: waits for a Job run and survives failed polls."""

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "wait_job_run.sh"

# A PATH shim for `databricks`: the first $FAIL_POLLS polls fail, the rest answer from $STATES (one per
# poll, the last repeats). $COUNT counts the calls; the argv of each call is logged to $CALLS.
SHIM = r"""#!/usr/bin/env bash
echo "$*" >> "$CALLS"
[ "$1 $2" = "jobs get-run" ] || exit 2
n=$(( $(cat "$COUNT" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$COUNT"
if [ "$n" -le "$FAIL_POLLS" ]; then
  if [ "${FAIL_MODE:-exit}" = garbage ]; then echo "<html>bad gateway"; exit 0; fi
  echo "read tcp 10.0.0.1:1->1.1.1.1:443: operation timed out" >&2; exit 1
fi
i=$(( n - FAIL_POLLS )); IFS=, read -ra states <<< "$STATES"
[ "$i" -gt "${#states[@]}" ] && i=${#states[@]}
s=${states[$((i - 1))]}; life=${s%/*}; result=${s#*/}
printf '{"run_page_url": "https://ws.example/jobs/42/runs/7", "state": {"life_cycle_state": "%s", "result_state": "%s"}}\n' "$life" "$result"
"""


def run_wait(tmp_path, fail_polls=0, states="TERMINATED/SUCCESS", max_failed=3, args=("7", "30"), **extra):
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "databricks"
    shim.write_text(SHIM)
    shim.chmod(0o755)
    env = {
        "PATH": f"{shim_dir}:/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "RUNNER_TEMP": str(tmp_path),
        "DATABRICKS_HOST": "https://ws.example",
        "JOB_ID": "42",
        "WAIT_POLL_SECONDS": "0.01",
        "WAIT_MAX_FAILED_POLLS": str(max_failed),
        "FAIL_POLLS": str(fail_polls),
        "STATES": states,
        "COUNT": str(tmp_path / "count"),
        "CALLS": str(tmp_path / "calls"),
    } | extra
    proc = subprocess.run(
        ["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    calls = (tmp_path / "calls").read_text().splitlines() if (tmp_path / "calls").exists() else []
    return proc, calls


def test_passes_after_failed_polls_and_leaves_the_final_json(tmp_path):
    proc, calls = run_wait(tmp_path, fail_polls=2, states="RUNNING/,TERMINATED/SUCCESS")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Job run: https://ws.example/jobs/42/runs/7" in proc.stdout
    run = json.loads((tmp_path / "run.json").read_text())
    assert run["state"] == {"life_cycle_state": "TERMINATED", "result_state": "SUCCESS"}
    assert calls == ["jobs get-run 7 -o json"] * 4  # 2 failed, RUNNING, TERMINATED


def test_unparseable_output_counts_as_a_failed_poll(tmp_path):
    proc, _ = run_wait(tmp_path, fail_polls=2, FAIL_MODE="garbage")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads((tmp_path / "run.json").read_text())["state"]["result_state"] == "SUCCESS"


@pytest.mark.parametrize("life", ["TERMINATED", "SKIPPED", "INTERNAL_ERROR"])
def test_every_end_state_stops_the_wait_and_the_caller_reads_the_result(tmp_path, life):
    proc, calls = run_wait(tmp_path, states=f"{life}/FAILED")
    assert proc.returncode == 0
    assert len(calls) == 1
    run = json.loads((tmp_path / "run.json").read_text())
    assert run["state"]["result_state"] == "FAILED"


def test_a_failed_run_leaves_a_result_that_fails_the_success_check(tmp_path):
    proc, _ = run_wait(tmp_path, states="TERMINATED/FAILED")
    assert proc.returncode == 0
    check = subprocess.run(
        ["jq", "-e", '.state.result_state == "SUCCESS"', str(tmp_path / "run.json")], capture_output=True
    )
    assert check.returncode != 0


def test_failed_polls_reset_when_one_succeeds(tmp_path):
    # 2 failures, then RUNNING: the counter restarts, so the limit of 3 is never reached by the total of 2.
    proc, _ = run_wait(tmp_path, fail_polls=2, states="RUNNING/,TERMINATED/SUCCESS", max_failed=3)
    assert proc.returncode == 0


def test_too_many_consecutive_failed_polls_fail_with_the_run_url(tmp_path):
    proc, calls = run_wait(tmp_path, fail_polls=99, max_failed=3)
    assert proc.returncode == 1
    assert len(calls) == 3
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("::error::"))
    assert "https://ws.example/jobs/42/runs/7" in line and "may still be going" in line


def test_the_deadline_fails_naming_the_run_and_does_not_cancel(tmp_path):
    proc, calls = run_wait(tmp_path, states="RUNNING/", WAIT_TIMEOUT_SECONDS="0")
    assert proc.returncode == 1
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("::error::"))
    assert "https://ws.example/jobs/42/runs/7" in line and "not cancelled" in line
    assert all(c.startswith("jobs get-run") for c in calls)  # no cancel-run


def test_rejects_a_non_numeric_run_id(tmp_path):
    proc, calls = run_wait(tmp_path, args=("7; rm -rf x", "30"))
    assert proc.returncode == 1 and calls == []


def test_keeps_the_callers_stdin_rows(tmp_path):
    """Inside a `while read` loop the databricks call must not read the loop's rows."""
    (tmp_path / "bin").mkdir()
    shim = tmp_path / "bin" / "databricks"
    shim.write_text(
        "#!/usr/bin/env bash\ncat > /dev/null\n"
        'echo \'{"state": {"life_cycle_state": "TERMINATED", "result_state": "SUCCESS"}}\'\n'
    )
    shim.chmod(0o755)
    env = {"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin", "RUNNER_TEMP": str(tmp_path)}
    script = f'while IFS= read -r row; do bash "{SCRIPT}" 7 30 >/dev/null; echo "$row"; done'
    proc = subprocess.run(["bash", "-c", script], env=env, input="a\nb\n", capture_output=True, text=True)
    assert proc.stdout.splitlines() == ["a", "b"]
