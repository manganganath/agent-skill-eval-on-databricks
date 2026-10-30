import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { ReleaseStatus } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { ErrorState } from "./ErrorState";
import { Loading } from "./Loading";

// The release is followed on the page: the status is read again while a step is pending.
export const POLL_MS = 25_000;

// Done once the skill is live after the merge, or once the pull request was closed without a merge.
export function releaseDone(s: ReleaseStatus): boolean {
  return s.live.switched_after_merge || (s.opened.state === "closed" && !s.merged.merged);
}

type Tone = "done" | "active" | "waiting" | "failed" | "todo";
interface Step {
  label: string;
  tone: Tone;
  text: string;
  link?: { to: string; text: string; internal?: boolean };
}

// The five steps, each with its state in words. The colour of a step only repeats what the words say.
export function releaseSteps(s: ReleaseStatus): Step[] {
  const { opened, gate, merged, release, live } = s;
  const closedUnmerged = opened.state === "closed" && !merged.merged;
  const gateStep: Step = gate.state === "passed" ? { label: "Eval gate", tone: "done", text: "Passed" }
    : gate.state === "failed" ? { label: "Eval gate", tone: "failed", text: "Failed" }
      : gate.state === "pending" ? { label: "Eval gate", tone: "active", text: "Running" }
        : { label: "Eval gate", tone: "todo", text: "Not run yet" };
  const mergeStep: Step = merged.merged
    ? { label: "Merged", tone: "done", text: `Merged${merged.sha ? ` at ${merged.sha.slice(0, 7)}` : ""}` }
    : closedUnmerged ? { label: "Merged", tone: "failed", text: "Closed without merging" }
      : { label: "Merged", tone: "todo", text: "Waiting for a reviewer" };
  const releaseStep: Step = release.state === "succeeded" ? { label: "Release approved", tone: "done", text: "Approved and published" }
    : release.state === "failed" ? { label: "Release approved", tone: "failed", text: "The release run failed" }
      : release.state === "waiting_approval" ? { label: "Release approved", tone: "waiting", text: "Waiting for approval in GitHub" }
        : release.state === "running" ? { label: "Release approved", tone: "active", text: "Running" }
          : { label: "Release approved", tone: "todo", text: "Not started" };
  const liveStep: Step = live.switched_after_merge && live.version !== null
    ? { label: "Live", tone: "done", text: `Live as v${live.version}`, link: { to: `/skills/${encodeURIComponent(live.fqn)}`, text: "Open the skill", internal: true } }
    : { label: "Live", tone: closedUnmerged ? "failed" : "todo", text: closedUnmerged ? "Not released" : "Not live yet" };
  return [
    {
      label: "Pull request opened",
      tone: "done",
      text: opened.state === "open" ? "Open" : merged.merged ? "Opened" : "Closed",
      link: { to: opened.url, text: "View the pull request" },
    },
    { ...gateStep, link: { to: gate.url, text: "View the checks" } },
    mergeStep,
    {
      ...releaseStep,
      link: release.url
        ? { to: release.url, text: release.state === "waiting_approval" ? "Approve in GitHub" : "Open the run in GitHub" }
        : undefined,
    },
    liveStep,
  ];
}

function MergeButton({ pr, onMerged }: { pr: number; onMerged: () => void }) {
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function merge() {
    setBusy(true);
    setError(null);
    try {
      await api.mergePull(pr);
      setAsking(false);
      setBusy(false);
      onMerged();
    } catch (err) {
      setError(err);
      setBusy(false);
    }
  }

  return (
    <div className="release-merge">
      {asking ? (
        <div role="group" aria-label="Merge this pull request" className="discard-confirm">
          <p>
            Merge pull request #{pr}? It is merged with a merge commit. Then the release waits for approval in the
            GitHub production environment.
          </p>
          <div className="discard-buttons">
            <button type="button" className="btn btn-primary" disabled={busy} onClick={merge}>
              {busy ? "Merging" : "Merge"}
            </button>
            <button type="button" disabled={busy} onClick={() => setAsking(false)}>Cancel</button>
          </div>
        </div>
      ) : (
        <button type="button" className="btn btn-primary" onClick={() => setAsking(true)}>Merge pull request</button>
      )}
      {error ? <ErrorState error={error} /> : null}
    </div>
  );
}

export function ReleasePath({ pr, dept, skill, isReviewer }: {
  pr: number; dept: string; skill: string; isReviewer: boolean;
}) {
  const status = useAsync<ReleaseStatus>(() => api.releaseStatus(pr, dept, skill), [pr, dept, skill]);
  const { reload } = status;
  const s = status.data;
  const pending = s !== undefined && !releaseDone(s);

  // A new answer (or a failed one) schedules the next read, so the timer runs once per answer.
  useEffect(() => {
    if (!pending) return undefined;
    const timer = window.setTimeout(reload, POLL_MS);
    return () => window.clearTimeout(timer);
  }, [s, status.error, pending, reload]);

  if (!s) {
    return status.error ? <ErrorState error={status.error} onRetry={reload} /> : <Loading label="Loading the release path" />;
  }
  const canMerge = isReviewer && s.gate.state === "passed" && s.opened.state === "open" && !s.merged.merged;
  return (
    <section aria-labelledby="release-path" className="panel release-path">
      <h2 id="release-path">Release path</h2>
      <p className="muted">
        The skill goes live in five steps. The page refreshes while a step is pending.
      </p>
      {status.error ? <ErrorState error={status.error} onRetry={reload} /> : null}
      <ol className="release-steps">
        {releaseSteps(s).map((step, i) => (
          <li key={step.label} className={`release-step release-step-${step.tone}`}>
            <span className="release-step-title">{i + 1}. {step.label}</span>
            <span className="release-step-state">{step.text}</span>
            {step.link && (step.link.internal
              ? <Link to={step.link.to}>{step.link.text}</Link>
              : <a href={step.link.to} target="_blank" rel="noreferrer">{step.link.text}</a>)}
          </li>
        ))}
      </ol>
      {canMerge && <MergeButton pr={pr} onMerged={reload} />}
    </section>
  );
}
