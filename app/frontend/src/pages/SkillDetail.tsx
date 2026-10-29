import { Link, useParams } from "react-router-dom";
import { api, ApiError } from "../api/client";
import type { JournalRow, Me, SkillDetail as SkillDetailT } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { Loading } from "../components/Loading";
import { FilesSection, RubricSection, SkillMdSection, TestCasesSection } from "../components/SkillParts";
import { TryPanel } from "../components/TryPanel";
import { VerdictChip } from "../components/VerdictChip";

// Live version: the latest `switched` row's to_version, cleared by a later `retired` row. A `published` row
// never makes a version live. The rows come from the API ordered by time; a string sort of the timestamps here
// could misorder them, so the order is kept as it is.
export function liveVersion(rows: JournalRow[]): number | null {
  let live: number | null = null;
  for (const r of rows) {
    if (r.action === "switched") live = r.to_version;
    else if (r.action === "retired") live = null;
  }
  return live;
}

const READ_ONLY = "Read only. Changes are reviewed in pull requests.";

function VersionHistory({ fqn }: { fqn: string }) {
  const versions = useAsync(() => api.versions(fqn), [fqn]);
  const rows = versions.data;
  const live = rows ? liveVersion(rows) : null;
  return (
    <section aria-labelledby="versions" className="panel">
      <div className="panel-head">
        <h2 id="versions">Version history</h2>
        {rows && rows.length > 0 && <p className="muted">{live === null ? "No version is live." : `Live version: ${live}`}</p>}
      </div>
      {versions.loading && <Loading label="Loading versions" />}
      {versions.error ? <ErrorState error={versions.error} onRetry={versions.reload} /> : null}
      {rows && rows.length === 0 && <EmptyState title="No versions recorded yet" />}
      {rows && rows.length > 0 && (
        <div className="table-scroll" role="region" aria-label="Version history" tabIndex={0}>
          <table>
            <caption>Versions and actions, oldest first</caption>
            <thead>
              <tr>
                <th scope="col">Time</th><th scope="col">Action</th><th scope="col" className="num">Version</th>
                <th scope="col">Actor</th><th scope="col">Commit</th><th scope="col">Eval run</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((v, i) => (
                <tr key={`${v.ts}-${v.action}-${i}`}>
                  <td className="nowrap">{v.ts}</td><td>{v.action}</td><td className="num">{v.version ?? v.to_version ?? "-"}</td>
                  <td>{v.actor}</td><td className="mono">{v.git_sha ?? "-"}</td><td className="mono">{v.eval_run_id ?? "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

// The header's live version and latest verdicts come with the skill. The API works them out as it does for the
// catalog, so both pages agree.
function SummaryVerdicts({ skill }: { skill: SkillDetailT }) {
  const entries = Object.entries(skill.verdicts);
  return (
    <section className="summary-verdicts" aria-labelledby="summary-verdicts">
      <h2 id="summary-verdicts">Latest verdicts on the live version</h2>
      {entries.length > 0 ? (
        <ul className="chips" aria-label="Latest verdicts">
          {entries.map(([model, verdict]) => (
            <li key={model}><span className="muted">{model}</span> <VerdictChip verdict={verdict} /></li>
          ))}
        </ul>
      ) : (
        <p className="muted">
          {skill.eval_access === "denied"
            ? "You can't read eval results for this department"
            : skill.eval_access === "unavailable"
              ? "Eval results are temporarily unavailable"
              : "No results yet"}
        </p>
      )}
    </section>
  );
}

export default function SkillDetail() {
  const { fqn = "" } = useParams(); // react-router has already decoded the param
  const skill = useAsync<SkillDetailT>(() => api.skill(fqn), [fqn]);
  const me = useAsync<Me>(() => api.me(), []); // shared with the header: one /api/me request per page load

  if (skill.loading) return <Loading label="Loading skill" />;
  if (skill.error) {
    // Any 404 means "not found, or no access", whatever reason code the server gave.
    const error = skill.error instanceof ApiError && skill.error.status === 404
      ? new ApiError(404, "skill_not_found") : skill.error;
    return <ErrorState error={error} onRetry={skill.reload} />;
  }
  const s = skill.data;
  if (!s) return <EmptyState title="No skill to show" />;

  return (
    <article>
      <nav aria-label="Breadcrumb" className="breadcrumb">
        <Link to="/">Catalog</Link><span aria-hidden="true">/</span><span>{s.dept}</span>
      </nav>
      <header className="panel summary">
        <div>
          <h1>{s.name}</h1>
          <p className="summary-meta">
            <span className="tag"><span className="visually-hidden">Department: </span>{s.dept}</span>
            <span className="tag">{s.live_version === null ? "No live version" : `Live version ${s.live_version}`}</span>
            <code className="fqn">{s.fqn}</code>
          </p>
          {s.description && <p className="summary-desc">{s.description}</p>}
        </div>
        <div className="summary-actions">
          <Link className="btn" to={`/matrix?skill=${encodeURIComponent(s.name)}&dept=${encodeURIComponent(s.dept)}`}>
            View evaluation matrix
          </Link>
          {me.data?.is_reviewer && (
            <Link className="btn" to={`/skills/${encodeURIComponent(s.fqn)}/switch`}>Switch version (reviewers)</Link>
          )}
          {me.data && !me.data.is_reviewer && (
            <p className="hint">Only reviewers can switch versions. <Link to="/access">Who can do what</Link></p>
          )}
        </div>
        <SummaryVerdicts skill={s} />
      </header>

      <SkillMdSection source={s.files["SKILL.md"]} />
      <FilesSection files={s.files} />

      <div className="panel">
        <TryPanel fqn={s.fqn} evalAccess={s.eval_access} />
      </div>

      <VersionHistory fqn={fqn} />

      <TestCasesSection evals={s.evals} name={s.name} access={s.eval_access} note={READ_ONLY} empty="No test cases published" />
      <RubricSection rubric={s.rubric} access={s.eval_access} note={READ_ONLY} empty="No rubric published" />
    </article>
  );
}
