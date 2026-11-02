import { useEffect } from "react";
import { Link } from "react-router-dom";
import { api, ApiError } from "../api/client";
import type { Me, PendingRelease, PendingSkill, ScanState } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { Loading } from "../components/Loading";
import { POLL_MS } from "../components/ReleasePath";
import { VerdictChip } from "../components/VerdictChip";

export const GITHUB_NOT_SET_UP =
  "GitHub is not set up for the Skill Hub yet, so the releases waiting for approval cannot be listed. An administrator sets the GitHub repository and token secret. See the operations guide.";

// How long a release has waited, in the two largest units that are not zero: "3 h 12 min", "2 d 4 h", "under a minute".
export function waitedFor(since: string | null, now: number): string {
  const start = since ? Date.parse(since) : NaN;
  if (Number.isNaN(start)) return "unknown";
  const minutes = Math.max(0, Math.floor((now - start) / 60_000));
  if (minutes < 1) return "under a minute";
  const days = Math.floor(minutes / 1440);
  const hours = Math.floor((minutes % 1440) / 60);
  if (days > 0) return `${days} d ${hours} h`;
  return hours > 0 ? `${hours} h ${minutes % 60} min` : `${minutes} min`;
}

// The run's own scan, in words. The findings themselves are in the run summary in GitHub.
export function scanText(scan: ScanState): string {
  switch (scan) {
    case "passed": return "No critical findings. Any high findings are listed in the run summary in GitHub.";
    case "failed": return "The scan found critical findings. Details are in the run summary in GitHub.";
    case "not_run": return "The scan was skipped for this release.";
    default: return "The scan result is not known yet. See the run summary in GitHub.";
  }
}

export const EVAL_SOURCE = {
  merge: "Merge eval: the merged content was evaluated again",
  pull_request: "Pull request eval, reused by the release",
} as const;

function Verdicts({ skill }: { skill: PendingSkill }) {
  const verdicts = Object.entries(skill.verdicts);
  if (verdicts.length === 0) {
    return (
      <span className="muted">
        {skill.eval_access === "denied" ? "You can't read eval results for this department"
          : skill.eval_access === "unavailable" ? "Eval results are temporarily unavailable" : "No results"}
      </span>
    );
  }
  return (
    <ul className="chips" aria-label={`Verdicts of ${skill.skill}`}>
      {verdicts.map(([model, verdict]) => (
        <li key={model}><span className="muted">{model}</span> <VerdictChip verdict={verdict} /></li>
      ))}
    </ul>
  );
}

function Skills({ release }: { release: PendingRelease }) {
  if (release.skills.length === 0) {
    return (
      <p className="muted">
        {release.details === "unavailable"
          ? "GitHub could not be read for this release, so its pull request and skills are not shown. Open the run in GitHub."
          : release.pull
            ? "No skill of this pull request could be read."
            : "No skills are shown: this commit has no pull request."}
      </p>
    );
  }
  return (
    <div className="table-scroll" role="region" aria-label={`Skills of run ${release.run_number}`} tabIndex={0}>
      <table>
        <thead>
          <tr><th scope="col">Skill</th><th scope="col">Eval gate verdicts</th><th scope="col">Eval</th></tr>
        </thead>
        <tbody>
          {release.skills.map((s) => (
            <tr key={`${s.dept}/${s.skill}`}>
              <th scope="row">
                {release.run_key
                  ? <Link to={`/candidates/${encodeURIComponent(release.run_key)}/${encodeURIComponent(s.dept)}/${encodeURIComponent(s.skill)}`}>{s.dept}/{s.skill}</Link>
                  : <>{s.dept}/{s.skill}</>}
              </th>
              <td><Verdicts skill={s} /></td>
              <td>{s.eval_source ? EVAL_SOURCE[s.eval_source] : <span className="muted">No eval found</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Release({ release, now }: { release: PendingRelease; now: number }) {
  const { pull } = release;
  return (
    <li className="panel release-item">
      <div className="panel-head">
        <h2>
          {pull ? <>Pull request #{pull.number}: {pull.title || "(no title)"}</> : <>Commit {release.sha.slice(0, 7)}</>}
        </h2>
        <p className="muted" title={release.waiting_since ?? undefined}>Waiting {waitedFor(release.waiting_since, now)}</p>
      </div>
      {pull ? (
        <p className="release-meta">
          Opened by {pull.author || "unknown"}. Merged by {pull.merged_by ?? "unknown"}.{" "}
          <a href={pull.url} target="_blank" rel="noreferrer">View the pull request</a>
        </p>
      ) : release.details === "ok" ? (
        <p className="release-meta">No pull request: this commit was pushed to main directly.</p>
      ) : null}
      <Skills release={release} />
      <p className={release.scan === "failed" ? "release-scan scan-flag" : "release-scan"}>
        <strong>Security scan.</strong> {scanText(release.scan)}
      </p>
      <p className="release-actions">
        <a className="btn btn-primary" href={release.run_url} target="_blank" rel="noreferrer">
          Review in GitHub<span className="visually-hidden"> (run {release.run_number})</span>
        </a>
        <span className="muted">Run {release.run_number}. Approve or reject there with Review deployments.</span>
      </p>
    </li>
  );
}

function Queue() {
  const releases = useAsync<PendingRelease[]>(() => api.pendingReleases(), []);
  const { reload } = releases;
  const { data, error } = releases;

  // A new answer (or a failed one) schedules the next read, so the timer runs once per answer, as on the release path.
  useEffect(() => {
    const timer = window.setTimeout(reload, POLL_MS);
    return () => window.clearTimeout(timer);
  }, [data, error, reload]);

  if (!data) {
    if (!error) return <Loading label="Loading the releases" />;
    const unset = error instanceof ApiError && error.reason === "github_not_configured";
    return <ErrorState error={error} onRetry={reload} message={unset ? GITHUB_NOT_SET_UP : undefined} />;
  }
  return (
    <>
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {data.length === 0 ? (
        <EmptyState title="No release is waiting for approval">
          <p className="muted">When a merged pull request waits for the production approval, it is listed here.</p>
        </EmptyState>
      ) : (
        <ul className="release-queue" aria-label="Releases waiting for approval">
          {data.map((r) => <Release key={r.run_url} release={r} now={Date.now()} />)}
        </ul>
      )}
    </>
  );
}

export default function Releases() {
  const me = useAsync<Me>(() => api.me(), []);
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Releases</h1>
          <p className="lede">
            Merged skills that wait for the production approval. The approval is given in GitHub by the required
            reviewers of the production environment: the Skill Hub never approves or rejects a release. The page
            refreshes by itself.
          </p>
        </div>
      </div>
      {me.loading ? <Loading label="Loading the releases" />
        : me.error ? <ErrorState error={me.error} onRetry={me.reload} />
        : !me.data?.is_reviewer
          ? <EmptyState title="Releases"><p>Only skill reviewers can see the releases waiting for approval. <Link to="/access">Who can do what</Link></p></EmptyState>
          : <Queue />}
    </>
  );
}
