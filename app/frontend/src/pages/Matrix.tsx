import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, type Listed } from "../api/client";
import type { AppConfig, CaseRow, MatrixRow } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { Loading } from "../components/Loading";
import { VerdictChip } from "../components/VerdictChip";
import { VerdictLegend } from "../components/VerdictLegend";
import { mlflowRunUrl, traceUrl } from "../links";
import { KIND_BADGES, KIND_LABELS, KIND_ORDER, kindOf } from "../models";

const ALL = "all";
const harnessOf = (r: MatrixRow) => r.harness ?? "reference";

const pct = (v: number | null) => (v == null ? "-" : `${Math.round(v * 100)}%`);
// with_skill rows: the pass rate minus the pass rate without the skill on the same cases, in percentage points.
const passUplift = (r: MatrixRow) => {
  if (r.mode !== "with_skill" || r.pass_rate == null || r.pass_rate_without == null) return "-";
  const pp = Math.round((r.pass_rate - r.pass_rate_without) * 100);
  return `${pp > 0 ? "+" : ""}${pp} pp`;
};
// The eval engine records version 0 when the skill content matches no published version (a repo or PR run).
const versionLabel = (v: number) => (v > 0 ? `v${v}` : "unpublished");

// A metric cell: right-aligned, tabular figures, tinted by its row's verdict. A missing value is muted.
function Num({ children }: { children: string }) {
  return <td className="num heat">{children === "-" ? <span className="empty-value">-</span> : children}</td>;
}

function HiddenNote({ departments }: { departments: string[] }) {
  if (departments.length === 0) return null;
  return (
    <p role="note" className="notice">
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
  const [kind, setKind] = useState(ALL);
  const [harness, setHarness] = useState(ALL);
  const cases = useAsync<Listed<CaseRow>>(
    () => (open ? api.cases(open.runId) : Promise.resolve({ rows: [], deniedDepartments: [], truncated: false })),
    [open?.runId],
  );

  if (matrix.loading) return <Loading label="Loading results" />;
  if (matrix.error) return <ErrorState error={matrix.error} onRetry={matrix.reload} />;
  const all = matrix.data?.rows ?? [];
  const kinds = KIND_ORDER.filter((k) => all.some((r) => kindOf(r.profile_kind) === k));
  const harnesses = [...new Set(all.map(harnessOf))].sort();
  // A choice that the data no longer holds counts as All, so the selects and the rows never disagree.
  const kindPick = (kinds as string[]).includes(kind) ? kind : ALL;
  const harnessPick = harnesses.includes(harness) ? harness : ALL;
  const rows = all.filter((r) => (kindPick === ALL || kindOf(r.profile_kind) === kindPick) &&
    (harnessPick === ALL || harnessOf(r) === harnessPick));
  const hidden = matrix.data?.deniedDepartments ?? [];
  const caseRows = cases.data?.rows ?? [];
  const caseHidden = cases.data?.deniedDepartments ?? [];
  const host = config.data?.workspace_url ?? "";
  // Without a workspace URL a link would point at the app's own origin, so no MLflow links are rendered.
  const classes: Record<string, string> = config.data?.models ?? {};

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Evaluation matrix{skill ? `: ${skill}` : ""}</h1>
          <p className="lede">Metric cells are tinted with the row's verdict colour.</p>
        </div>
        <Link className="btn" to="/run">Run an evaluation</Link>
      </div>
      <div className="notices">
        <HiddenNote departments={hidden} />
        {matrix.data?.truncated && <p role="note" className="notice">Only the first 5000 rows are shown.</p>}
        {!host && !config.loading && (
          <p role="note" className="notice">MLflow links are unavailable because the workspace address is not configured.</p>
        )}
      </div>
      {all.length > 0 && (
        <div className="dash-filters">
          <div className="field">
            <label htmlFor="matrix-kind">Kind</label>
            <select id="matrix-kind" value={kindPick} onChange={(e) => setKind(e.target.value)}>
              <option value={ALL}>All kinds</option>
              {kinds.map((k) => <option key={k} value={k}>{KIND_LABELS[k]}</option>)}
            </select>
          </div>
          <div className="field">
            <label htmlFor="matrix-harness">Harness</label>
            <select id="matrix-harness" value={harnessPick} onChange={(e) => setHarness(e.target.value)}>
              <option value={ALL}>All harnesses</option>
              {harnesses.map((h) => <option key={h} value={h}>{h}</option>)}
            </select>
          </div>
        </div>
      )}
      {all.length === 0 ? (
        <EmptyState title="No results yet">
          <p>No evaluation has run for this selection. Start one from Run eval.</p>
        </EmptyState>
      ) : rows.length === 0 ? (
        <EmptyState title="No rows match the filters" />
      ) : (
        <div className="table-scroll matrix-scroll" role="region" aria-label="Evaluation matrix" tabIndex={0}>
          <table className="matrix">
            <caption>Evaluation matrix: one row per version, model and mode</caption>
            <thead>
              <tr>
                <th scope="col">Skill</th><th scope="col">Version</th><th scope="col">Model</th><th scope="col">Mode</th>
                <th scope="col">Verdict</th>
                <th scope="col" className="num">Accuracy</th><th scope="col" className="num">Precision</th>
                <th scope="col" className="num">Recall</th><th scope="col" className="num">Pass rate</th>
                <th scope="col" className="num">Pass rate (without)</th><th scope="col" className="num">Uplift (pass)</th>
                <th scope="col" className="num">Spread</th>
                <th scope="col" className="num">Uplift (task)</th><th scope="col" className="num">Uplift (format)</th>
                <th scope="col" className="num">Unreviewed</th><th scope="col">Links</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const label = `${r.model_profile} ${r.mode} ${versionLabel(r.skill_version)}`;
                const isOpen = open?.runId === r.run_id;
                return (
                  <tr key={`${r.dept}-${r.skill}-${r.skill_version}-${r.model_profile}-${r.mode}-${r.run_id}`}
                    className={`heat-${r.verdict}${isOpen ? " is-open" : ""}`}>
                    <td><span className="cell-main">{r.skill}</span></td>
                    <td className="nowrap">{versionLabel(r.skill_version)}</td>
                    <td>
                      <span className="cell-main">{r.model_profile}</span>{" "}
                      <span className="badge">{KIND_BADGES[kindOf(r.profile_kind)]}</span>
                      <span className="cell-sub">
                        <span>{classes[r.model_profile] ?? r.model_class}</span>{r.harness && <> <span>{r.harness}</span></>}
                      </span>
                    </td>
                    <td className="nowrap">{r.mode}</td>
                    <td className="heat-verdict"><VerdictChip verdict={r.verdict} /></td>
                    <Num>{pct(r.accuracy)}</Num><Num>{pct(r.precision)}</Num><Num>{pct(r.recall)}</Num>
                    <Num>{pct(r.pass_rate)}</Num>
                    <Num>{pct(r.pass_rate_without)}</Num><Num>{passUplift(r)}</Num>
                    <Num>{r.spread == null ? "-" : r.spread.toFixed(2)}</Num>
                    <Num>{pct(r.uplift_task_quality)}</Num><Num>{pct(r.uplift_skill_format)}</Num>
                    <td className="num">{Math.max(0, r.n_cases - r.n_reviewed)}</td>
                    <td className="actions">
                      {host && (
                        <><a href={mlflowRunUrl(host, r.experiment_id, r.run_id)} target="_blank" rel="noreferrer"
                          aria-label={`MLflow run for ${label}`}>MLflow run</a>{" "}</>
                      )}
                      <button type="button" className="btn-sm" aria-label={`Show cases for ${label}`}
                        onClick={() => setOpen({ runId: r.run_id, experimentId: r.experiment_id })}>
                        Show cases
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {open && (
        <section aria-labelledby="cases-heading" className="cases-section">
          <h2 id="cases-heading">Cases for run {open.runId}</h2>
          {cases.loading && <Loading label="Loading cases" />}
          {cases.error ? <ErrorState error={cases.error} onRetry={cases.reload} /> : null}
          {!cases.loading && !cases.error && <HiddenNote departments={caseHidden} />}
          {!cases.loading && !cases.error && caseRows.length === 0 && <EmptyState title="No cases recorded for this run" />}
          {!cases.loading && caseRows.length > 0 && (
            <div className="table-scroll matrix-scroll" role="region" aria-label="Cases in this run" tabIndex={0}>
              <table className="matrix">
                <caption>Cases in this run</caption>
                <thead>
                  <tr>
                    <th scope="col" className="num">Case</th><th scope="col" className="num">Repeat</th><th scope="col">Assessment</th>
                    <th scope="col">Value</th><th scope="col">Outcome</th><th scope="col">Trace</th>
                  </tr>
                </thead>
                <tbody>
                  {caseRows.map((c, i) => (
                    <tr key={`${c.trace_id}-${c.assessment_name}-${i}`}>
                      <td className="num">{c.case_id}</td><td className="num">{c.repeat}</td><td>{c.assessment_name}</td>
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
      <VerdictLegend rules={config.data?.verdict_rules} />
    </>
  );
}
