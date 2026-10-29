import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, type Listed } from "../api/client";
import type { AppConfig, CaseRow, MatrixRow } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { Loading } from "../components/Loading";
import { VerdictChip } from "../components/VerdictChip";
import { mlflowRunUrl, traceUrl } from "../links";

const pct = (v: number | null) => (v == null ? "-" : `${Math.round(v * 100)}%`);

function HiddenNote({ departments }: { departments: string[] }) {
  if (departments.length === 0) return null;
  return (
    <p role="note" className="muted">
      Some departments' results are hidden: you can't read {departments.join(", ")}.
    </p>
  );
}

export default function Matrix() {
  const [params] = useSearchParams();
  const skill = params.get("skill") ?? undefined;
  const dept = params.get("dept") ?? undefined;
  const matrix = useAsync<Listed<MatrixRow>>(() => api.matrix({ skill, dept }), [skill, dept]);
  const config = useAsync<AppConfig>(() => api.config(), []);
  const [open, setOpen] = useState<{ runId: string; experimentId: string } | null>(null);
  const cases = useAsync<Listed<CaseRow>>(
    () => (open ? api.cases(open.runId) : Promise.resolve({ rows: [], deniedDepartments: [], truncated: false })),
    [open?.runId],
  );

  if (matrix.loading) return <Loading label="Loading results" />;
  if (matrix.error) return <ErrorState error={matrix.error} onRetry={matrix.reload} />;
  const rows = matrix.data?.rows ?? [];
  const hidden = matrix.data?.deniedDepartments ?? [];
  const caseRows = cases.data?.rows ?? [];
  const caseHidden = cases.data?.deniedDepartments ?? [];
  const host = config.data?.workspace_url ?? "";
  // Without a workspace URL a link would point at the app's own origin, so no MLflow links are rendered.
  const classes: Record<string, string> = config.data?.models ?? {};

  return (
    <>
      <h1>Evaluation matrix{skill ? `: ${skill}` : ""}</h1>
      <p><Link to="/run">Run an evaluation</Link></p>
      <HiddenNote departments={hidden} />
      {matrix.data?.truncated && <p role="note" className="muted">Only the first 5000 rows are shown.</p>}
      {!host && !config.loading && (
        <p role="note" className="muted">MLflow links are unavailable because the workspace address is not configured.</p>
      )}
      {rows.length === 0 ? (
        <EmptyState title="No results yet">
          <p>No evaluation has run for this selection. Start one from Run eval.</p>
        </EmptyState>
      ) : (
        <div className="table-scroll" role="region" aria-label="Evaluation matrix" tabIndex={0}>
          <table>
            <caption>Evaluation matrix: one row per version, model and mode</caption>
            <thead>
              <tr>
                <th scope="col">Skill</th><th scope="col">Version</th><th scope="col">Model</th><th scope="col">Mode</th>
                <th scope="col">Accuracy</th><th scope="col">Precision</th><th scope="col">Recall</th>
                <th scope="col">Pass rate</th><th scope="col">Spread</th>
                <th scope="col">Uplift (task)</th><th scope="col">Uplift (format)</th>
                <th scope="col">Verdict</th><th scope="col">Unreviewed</th><th scope="col">Links</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={`${r.dept}-${r.skill}-${r.skill_version}-${r.model_profile}-${r.mode}-${r.run_id}`}>
                  <td>{r.skill}</td>
                  <td>v{r.skill_version}</td>
                  <td>{r.model_profile} <span className="muted">{classes[r.model_profile] ?? r.model_class}</span>{r.harness && <> <span className="muted">{r.harness}</span></>}</td>
                  <td>{r.mode}</td>
                  <td>{pct(r.accuracy)}</td><td>{pct(r.precision)}</td><td>{pct(r.recall)}</td>
                  <td>{pct(r.pass_rate)}</td>
                  <td>{r.spread == null ? "-" : r.spread.toFixed(2)}</td>
                  <td>{pct(r.uplift_task_quality)}</td><td>{pct(r.uplift_skill_format)}</td>
                  <td><VerdictChip verdict={r.verdict} /></td>
                  <td>{Math.max(0, r.n_cases - r.n_reviewed)}</td>
                  <td>
                    {host && (
                      <><a href={mlflowRunUrl(host, r.experiment_id, r.run_id)} target="_blank" rel="noreferrer"
                        aria-label={`MLflow run for ${r.model_profile} ${r.mode} v${r.skill_version}`}>MLflow run</a>{" "}</>
                    )}
                    <button type="button" aria-label={`Show cases for ${r.model_profile} ${r.mode} v${r.skill_version}`} onClick={() => setOpen({ runId: r.run_id, experimentId: r.experiment_id })}>
                      Show cases
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {open && (
        <section aria-labelledby="cases-heading">
          <h2 id="cases-heading">Cases for run {open.runId}</h2>
          {cases.loading && <Loading label="Loading cases" />}
          {cases.error ? <ErrorState error={cases.error} onRetry={cases.reload} /> : null}
          {!cases.loading && !cases.error && <HiddenNote departments={caseHidden} />}
          {!cases.loading && !cases.error && caseRows.length === 0 && <EmptyState title="No cases recorded for this run" />}
          {!cases.loading && caseRows.length > 0 && (
            <div className="table-scroll" role="region" aria-label="Cases in this run" tabIndex={0}>
              <table>
                <caption>Cases in this run</caption>
                <thead>
                  <tr>
                    <th scope="col">Case</th><th scope="col">Repeat</th><th scope="col">Assessment</th>
                    <th scope="col">Value</th><th scope="col">Outcome</th><th scope="col">Trace</th>
                  </tr>
                </thead>
                <tbody>
                  {caseRows.map((c, i) => (
                    <tr key={`${c.trace_id}-${c.assessment_name}-${i}`}>
                      <td>{c.case_id}</td><td>{c.repeat}</td><td>{c.assessment_name}</td>
                      <td>{c.value ?? "-"}</td><td>{c.trigger_outcome ?? "-"}</td>
                      <td>
                        {host ? (
                          <a href={traceUrl(host, open.experimentId, c.trace_id)} target="_blank" rel="noreferrer">
                            Trace for case {c.case_id}
                          </a>
                        ) : "-"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}
    </>
  );
}
