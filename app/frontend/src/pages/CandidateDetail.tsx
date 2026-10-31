import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, ApiError } from "../api/client";
import type { CandidateDetail as CandidateDetailT, CandidateResult, Me, SubmitOut } from "../api/types";
import { runCandidatePath } from "../candidates";
import { CandidateBadge } from "../components/CandidateBadge";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { Loading } from "../components/Loading";
import { ReleasePath } from "../components/ReleasePath";
import { EvalEmpty, FilesSection, RubricSection, SkillMdSection, TestCasesSection } from "../components/SkillParts";
import { TryPanel } from "../components/TryPanel";
import { VerdictChip } from "../components/VerdictChip";
import { useAsync } from "../hooks/useAsync";

const FROM_PR = "From evals/ in the pull request. Change them there.";
const FROM_DRAFT = "From evals/ in the uploaded draft. To change them, upload the draft again.";
const SOURCES: Record<string, string> = { pr: "Pull request", manual: "Skill Hub" };

// "2026-10-28 09:00 UTC". The API sends ISO times.
export function utcMinute(iso: string | null): string {
  if (!iso) return "unknown";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : `${d.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

const percent = (x: number | null) => (x === null ? "-" : `${Math.round(x * 100)}%`);

function Results({ s }: { s: CandidateDetailT }) {
  return (
    <section aria-labelledby="results" className="panel">
      <h2 id="results">Eval results</h2>
      <p className="muted">This upload's pull request eval, if any, and the Skill Hub runs started for it, newest first.</p>
      {s.results.length > 0 ? (
        <div className="table-scroll" role="region" aria-label="Eval results" tabIndex={0}>
          <table>
            <caption>Eval results</caption>
            <thead>
              <tr>
                <th scope="col">Started</th><th scope="col">Run key</th><th scope="col">Started from</th>
                <th scope="col">Model</th><th scope="col">Mode</th><th scope="col">Verdict</th>
                <th scope="col" className="num">Pass rate</th><th scope="col" className="num">Cases</th>
              </tr>
            </thead>
            <tbody>
              {s.results.map((r: CandidateResult, i) => (
                <tr key={r.run_id ?? `${r.run_key}-${r.model_profile}-${r.mode}-${i}`}>
                  <td className="nowrap">{utcMinute(r.started_at)}</td>
                  <td className="mono">{r.run_key}</td>
                  <td>{SOURCES[r.trigger_source ?? ""] ?? r.trigger_source ?? "-"}</td>
                  <td>{r.model_profile}</td><td>{r.mode}</td>
                  <td><VerdictChip verdict={r.verdict ?? undefined} /></td>
                  <td className="num">{percent(r.pass_rate)}</td><td className="num">{r.n_cases ?? "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : s.eval_access !== "ok" ? <EvalEmpty access={s.eval_access} ok="" /> : (
        <EmptyState title="No eval results yet">
          <p>
            {s.candidate.kind === "draft" ? "Start an eval of this draft to see results here."
              : "Results appear when the pull request's eval finishes. You can also start an eval of this candidate."}
          </p>
        </EmptyState>
      )}
    </section>
  );
}

// The uploader, or a reviewer, opens a pull request with the draft. Two steps, as for discard: a click never opens one.
function SubmitDraftButton({ runKey, onSubmitted, onAlreadySubmitted }: {
  runKey: string; onSubmitted: (out: SubmitOut) => void; onAlreadySubmitted: () => void;
}) {
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function submit() {
    setBusy(true);
    setError(null);
    try {
      onSubmitted(await api.submitDraft(runKey));
    } catch (err) {
      setError(err);
      setBusy(false);
      if (err instanceof ApiError && err.reason === "already_submitted") onAlreadySubmitted();
    }
  }

  return (
    <div className="submit-draft">
      {asking ? (
        <div role="group" aria-label="Submit this draft for review" className="discard-confirm">
          <p>
            Open a pull request with this draft? Its eval gate runs automatically, a reviewer merges it, and the
            release is approved in GitHub.
          </p>
          <div className="discard-buttons">
            <button type="button" className="btn btn-primary" disabled={busy} onClick={submit}>
              {busy ? "Opening" : "Open pull request"}
            </button>
            <button type="button" disabled={busy} onClick={() => setAsking(false)}>Cancel</button>
          </div>
        </div>
      ) : (
        <button type="button" className="btn btn-primary" onClick={() => setAsking(true)}>Submit for review</button>
      )}
      {error ? <ErrorState error={error} /> : null}
    </div>
  );
}

// A draft's uploader, or a reviewer, can remove it before its 7 days are up. Two steps, so a click never deletes.
function DiscardDraft({ runKey, name, uploader }: { runKey: string; name: string; uploader: string | null }) {
  const me = useAsync<Me>(() => api.me(), []);
  const navigate = useNavigate();
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const allowed = me.data !== undefined
    && (me.data.is_reviewer || (uploader ?? "").toLowerCase() === me.data.user.toLowerCase());
  if (!allowed) return null;

  async function discard() {
    setBusy(true);
    setError(null);
    try {
      await api.discardDraft(runKey);
      navigate("/", { state: { notice: `Draft ${name} was discarded.` } });
    } catch (err) {
      setError(err);
      setBusy(false);
    }
  }

  return (
    <div className="discard">
      {asking ? (
        <div role="group" aria-label="Discard this draft" className="discard-confirm">
          <p>Discard this draft? Its files are removed. Its eval results stay in the Eval matrix.</p>
          <div className="discard-buttons">
            <button type="button" className="btn-danger" disabled={busy} onClick={discard}>
              {busy ? "Discarding" : "Discard"}
            </button>
            <button type="button" disabled={busy} onClick={() => setAsking(false)}>Cancel</button>
          </div>
        </div>
      ) : (
        <button type="button" className="btn-danger-outline" onClick={() => setAsking(true)}>Discard draft</button>
      )}
      {error ? <ErrorState error={error} /> : null}
    </div>
  );
}

export default function CandidateDetail() {
  const { runKey = "", dept = "", skill = "" } = useParams(); // react-router has already decoded the params
  const candidate = useAsync<CandidateDetailT>(() => api.candidate(runKey, dept, skill), [runKey, dept, skill]);
  const me = useAsync<Me>(() => api.me(), []);
  const [submitted, setSubmitted] = useState<SubmitOut | null>(null); // this visit's submission, until the page reloads

  if (candidate.loading) return <Loading label="Loading candidate" />;
  if (candidate.error) {
    // Any 404 means "not found, or no access", whatever reason code the server gave.
    const error = candidate.error instanceof ApiError && candidate.error.status === 404
      ? new ApiError(404, "candidate_not_found") : candidate.error;
    return (
      <>
        <ErrorState error={error} onRetry={candidate.reload} />
        <p className="hint">Skills under review are visible to authors and reviewers. <Link to="/access">Who can do what</Link></p>
      </>
    );
  }
  const s = candidate.data;
  if (!s) return <EmptyState title="No candidate to show" />;
  const { kind, pr_number: pr, git_sha: sha, run_key: key, uploaded_at: uploaded, uploaded_by: uploader } = s.candidate;
  const verdicts = Object.entries(s.verdicts);
  const draft = kind === "draft";
  // A draft's pull request: recorded on the draft, or the one this visit has just opened.
  const submittedPr = draft ? s.candidate.submitted_pr ?? submitted?.pr_number ?? null : null;
  const submittedUrl = draft ? s.candidate.submitted_url ?? submitted?.url ?? null : null;
  const canSubmit = me.data !== undefined
    && (me.data.is_reviewer || (uploader ?? "").toLowerCase() === me.data.user.toLowerCase());
  const releasePr = draft ? submittedPr : pr;

  return (
    <article>
      <nav aria-label="Breadcrumb" className="breadcrumb">
        <Link to="/">Catalog</Link><span aria-hidden="true">/</span><span>{draft ? "Drafts" : "Under review"}</span>
        <span aria-hidden="true">/</span><span>{s.dept}</span>
      </nav>
      <div className="candidate-banner" role="note" aria-label="Release status">
        {draft ? (
          <>
            <p className="candidate-banner-title"><strong>Not released.</strong> Draft uploaded by {uploader ?? "unknown"}.</p>
            <p className="candidate-banner-meta">
              Uploaded {utcMinute(uploaded)}. Run key <code>{key}</code>. A draft is never released and is removed 7 days
              after upload. {submittedPr !== null
                ? "It is in a pull request, and it is released only after a reviewer merges that."
                : "To release the skill, open a pull request."}
            </p>
          </>
        ) : (
          <>
            <p className="candidate-banner-title"><strong>Not released.</strong> Under review in PR #{pr}.</p>
            <p className="candidate-banner-meta">
              Commit <code title={sha ?? undefined}>{(sha ?? "").slice(0, 7)}</code>, uploaded {utcMinute(uploaded)}. Run
              key <code>{key}</code>. Agents cannot load this skill until the pull request merges and a reviewer approves
              the release.
            </p>
          </>
        )}
      </div>
      <header className="panel summary">
        <div>
          <h1>{s.name}</h1>
          <p className="summary-meta">
            <span className="tag"><span className="visually-hidden">Department: </span>{s.dept}</span>
            <CandidateBadge kind={kind} pr={pr} />
          </p>
          {s.description && <p className="summary-desc">{s.description}</p>}
        </div>
        <div className="summary-actions">
          <Link className="btn btn-primary" to={runCandidatePath(key, s.dept, s.name)}>Run eval</Link>
          {draft && (submittedPr !== null ? (
            <a className="submitted-note" href={submittedUrl ?? undefined} target="_blank" rel="noreferrer">
              Submitted as PR #{submittedPr}
            </a>
          ) : canSubmit && <SubmitDraftButton runKey={key} onSubmitted={setSubmitted} onAlreadySubmitted={candidate.reload} />)}
          {draft && <DiscardDraft runKey={key} name={s.name} uploader={uploader} />}
          {!s.rubric && (
            <p className="hint summary-hint">
              {s.rubric_optional
                ? "This skill has no rubric, so effectiveness mode judges its cases on their assertions only."
                : "This skill has no rubric, so it can run in trigger mode only."}
            </p>
          )}
        </div>
        <section className="summary-verdicts" aria-labelledby="summary-verdicts">
          <h2 id="summary-verdicts">Latest verdicts on this {draft ? "draft" : "candidate"}</h2>
          {verdicts.length > 0 ? (
            <ul className="chips" aria-label="Latest verdicts">
              {verdicts.map(([model, verdict]) => (
                <li key={model}><span className="muted">{model}</span> <VerdictChip verdict={verdict} /></li>
              ))}
            </ul>
          ) : (
            <p className="muted">
              {s.eval_access === "denied" ? "You can't read eval results for this department"
                : s.eval_access === "unavailable" ? "Eval results are temporarily unavailable" : "No results yet"}
            </p>
          )}
          <p className="hint">Verdicts count the modes that gate a release. The Eval matrix shows every mode, effectiveness included.</p>
        </section>
      </header>

      {releasePr !== null && (
        <ReleasePath pr={releasePr} dept={s.dept} skill={s.name} isReviewer={me.data?.is_reviewer === true} />
      )}
      <SkillMdSection source={s.files["SKILL.md"]} />
      <FilesSection files={s.files} />
      <div className="panel">
        <TryPanel target={{ runKey: key, dept: s.dept, skill: s.name }} evalAccess={s.eval_access} />
      </div>
      <Results s={s} />
      <TestCasesSection evals={s.evals} name={s.name} access="ok" note={draft ? FROM_DRAFT : FROM_PR}
        empty="This upload has no evals/evals.json" />
      <RubricSection rubric={s.rubric} access="ok" note={draft ? FROM_DRAFT : FROM_PR}
        empty="This upload has no evals/rubric.json" />
    </article>
  );
}
