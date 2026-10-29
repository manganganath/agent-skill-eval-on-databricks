import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import type { JournalRow, MatrixRow, Me } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { Loading } from "../components/Loading";
import { VerdictChip } from "../components/VerdictChip";
import { liveVersion } from "./SkillDetail";

export default function Switch() {
  const { fqn = "" } = useParams(); // react-router has already decoded the param
  const skillName = fqn.split(".").pop() ?? fqn;
  const me = useAsync<Me>(() => api.me(), []);
  const versions = useAsync<JournalRow[]>(() => api.versions(fqn), [fqn]);
  // The schema name is not the department key (dept_equity_research against equity_research), so the API
  // maps the FQN to the department.
  const matrix = useAsync<MatrixRow[]>(async () => (await api.matrix({ fqn })).rows, [fqn]);
  const [target, setTarget] = useState<number | null>(null);
  const [ack, setAck] = useState(false);
  const [job, setJob] = useState<number | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [pending, setPending] = useState(false);
  const inFlight = useRef(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);

  if (me.loading || versions.loading) return <Loading label="Loading switch options" />;
  if (me.error) return <ErrorState error={me.error} onRetry={me.reload} />;
  if (!me.data?.is_reviewer) {
    return <EmptyState title="Switching versions"><p>Only skill reviewers can switch versions.</p></EmptyState>;
  }
  if (versions.error) return <ErrorState error={versions.error} onRetry={versions.reload} />;

  const rows = versions.data ?? [];
  const current = liveVersion(rows); // a published row never makes a version live
  const published = [...new Set(rows.filter((r) => r.action === "published" && r.version != null).map((r) => r.version!))];

  const rowsFor = (v: number | null) => (matrix.data ?? []).filter((r) => r.skill_version === v);
  const before = rowsFor(current);
  const after = target == null ? [] : rowsFor(target);
  // One row per harness, model and mode; rows without a harness come from the reference harness.
  const key = (r: MatrixRow) => `${r.harness ?? "reference"}|${r.model_profile}|${r.mode}`;
  const afterByKey = new Map(after.map((r) => [key(r), r]));

  async function confirm() {
    if (target == null || inFlight.current || job != null) return;
    inFlight.current = true;
    setPending(true);
    setError(null);
    try {
      const out = await api.switchVersion(fqn, target);
      if (mounted.current) setJob(out.job_run_id);
    } catch (err) {
      if (mounted.current) setError(err);
    } finally {
      inFlight.current = false;
      if (mounted.current) setPending(false);
    }
  }

  return (
    <>
      <nav aria-label="Breadcrumb" className="breadcrumb">
        <Link to="/">Catalog</Link><span aria-hidden="true">/</span>
        <Link to={`/skills/${encodeURIComponent(fqn)}`}>{skillName}</Link>
      </nav>
      <div className="page-head">
        <div>
          <h1>Switch version: {skillName}</h1>
          <p className="lede">Compare a published version with the live one, then switch. The change applies to every user.</p>
        </div>
      </div>
      <fieldset className="form-group">
        <legend>Choose the target</legend>
        <div className="switch-grid">
          <p className="stat stat-value">Live version: {current ?? "none"}</p>
          <div className="field">
            <label htmlFor="target">Target version</label>
            <select id="target" value={target ?? ""}
              onChange={(e) => { setTarget(e.target.value ? Number(e.target.value) : null); setAck(false); setJob(null); setError(null); }}>
              <option value="">Choose a version</option>
              {published.filter((v) => v !== current).map((v) => <option key={v} value={v}>v{v}</option>)}
            </select>
          </div>
        </div>
      </fieldset>

      {target != null && matrix.error ? <ErrorState error={matrix.error} onRetry={matrix.reload} /> : null}
      {target != null && !matrix.error && (
        before.length === 0 && after.length === 0 ? (
          <EmptyState title="No evaluation results">
            <p>Neither version has results. Review the change before switching.</p>
          </EmptyState>
        ) : (
          <div className="table-scroll" role="region" aria-label="Matrix difference" tabIndex={0}>
            <table>
              <caption>Matrix difference: v{current ?? "-"} (live) against v{target} (target)</caption>
              <thead><tr><th scope="col">Model</th><th scope="col">Mode</th><th scope="col">Live verdict</th><th scope="col">Target verdict</th><th scope="col" className="num">Pass rate change</th></tr></thead>
              <tbody>
                {[...new Set([...before, ...after].map(key))].map((k) => {
                  const b = before.find((r) => key(r) === k);
                  const a = afterByKey.get(k);
                  const delta = a?.pass_rate != null && b?.pass_rate != null ? Math.round((a.pass_rate - b.pass_rate) * 100) : null;
                  const [harness, model, mode] = k.split("|");
                  return (
                    <tr key={k}>
                      <td>{harness === "reference" ? model : `${model} (${harness})`}</td><td>{mode}</td>
                      <td><VerdictChip verdict={b?.verdict} /></td>
                      <td><VerdictChip verdict={a?.verdict} /></td>
                      <td className="num">{delta == null ? "-" : `${delta > 0 ? "+" : ""}${delta} pts`}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )
      )}

      {target != null && (
        <div className="confirm-box">
          <label><input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
            <span>I understand that this will switch {skillName} from {current == null ? "none" : `v${current}`} to v{target}, and changes the live skill for every user.</span></label>
          <button type="button" className="btn-primary" disabled={!ack || pending || job != null} onClick={confirm}>Confirm switch</button>
        </div>
      )}
      {error ? <ErrorState error={error} /> : null}
      <p role="status" aria-live="polite" className="status-line">
        {job != null && `Switch started (job ${job}). Follow it in the Skill Hub when the job completes.`}
      </p>
      <p className="back-link"><Link to={`/skills/${encodeURIComponent(fqn)}`}>Back to skill</Link></p>
    </>
  );
}
