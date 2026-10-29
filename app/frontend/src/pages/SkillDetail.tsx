import { Link, useParams } from "react-router-dom";
import { api, ApiError } from "../api/client";
import type { EvalAccess, JournalRow, Me, SkillDetail as SkillDetailT, SkillSummary } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { Loading } from "../components/Loading";
import { Markdown } from "../components/Markdown";
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

function EvalEmpty({ access, ok }: { access: EvalAccess; ok: string }) {
  if (access === "denied") return <EmptyState title="You can't read eval results for this department" />;
  if (access === "unavailable") return <EmptyState title="Eval results are temporarily unavailable" />;
  return <EmptyState title={ok} />;
}

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

// The summary line for the header: the catalog's live version and latest verdicts for this skill. It comes from
// the same list the catalog shows, so both pages agree. If it cannot be read, the header leaves those parts out.
function useSummary(fqn: string) {
  const name = fqn.split(".").pop() ?? fqn;
  return useAsync<SkillSummary | undefined>(
    async () => (await api.skills(name)).find((x) => x.fqn === fqn),
    [fqn],
  );
}

function SummaryVerdicts({ summary }: { summary: SkillSummary }) {
  const entries = Object.entries(summary.verdicts);
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
          {summary.eval_access === "denied"
            ? "You can't read eval results for this department"
            : summary.eval_access === "unavailable"
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
  const me = useAsync<Me>(() => api.me(), []);
  const summary = useSummary(fqn);

  if (skill.loading) return <Loading label="Loading skill" />;
  if (skill.error) {
    // Any 404 means "not found, or no access", whatever reason code the server gave.
    const error = skill.error instanceof ApiError && skill.error.status === 404
      ? new ApiError(404, "skill_not_found") : skill.error;
    return <ErrorState error={error} onRetry={skill.reload} />;
  }
  const s = skill.data;
  if (!s) return <EmptyState title="No skill to show" />;

  const skillMd = s.files["SKILL.md"];
  const paths = Object.keys(s.files).sort();
  const sum = summary.data;

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
            {sum && (
              <span className="tag">{sum.live_version === null ? "No live version" : `Live version ${sum.live_version}`}</span>
            )}
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
        </div>
        {sum && <SummaryVerdicts summary={sum} />}
      </header>

      <section aria-labelledby="skill-md" className="panel">
        <h2 id="skill-md">Skill</h2>
        <p className="muted">SKILL.md: the instructions an agent follows when it uses this skill.</p>
        {skillMd ? <div className="prose"><Markdown source={skillMd} /></div> : <EmptyState title="No SKILL.md found" />}
      </section>

      <section aria-labelledby="files" className="panel">
        <h2 id="files">Files</h2>
        <p className="muted">Open a file to read it.</p>
        <ul aria-label="Files" className="file-list">
          {paths.map((p) => (
            <li key={p}>
              {p === "SKILL.md" ? <>{p} <span className="muted">shown above</span></> : (
                <details>
                  <summary>{p}</summary>
                  <pre>{s.files[p]}</pre>
                </details>
              )}
            </li>
          ))}
        </ul>
      </section>

      <div className="panel">
        <TryPanel fqn={s.fqn} evalAccess={s.eval_access} />
      </div>

      <VersionHistory fqn={fqn} />

      <section aria-labelledby="tests" className="panel">
        <h2 id="tests">Test cases</h2>
        <p className="muted">Read only. Changes are reviewed in pull requests.</p>
        {s.evals && s.evals.evals.length > 0 ? (
          <div className="table-scroll" role="region" aria-label="Test cases" tabIndex={0}>
            <table>
              <caption>Test cases</caption>
              <thead>
                <tr><th scope="col" className="num">ID</th><th scope="col">Kind</th><th scope="col">Prompt</th><th scope="col">Expected skill</th></tr>
              </thead>
              <tbody>
                {s.evals.evals.map((c) => (
                  <tr key={c.id}>
                    <td className="num">{c.id}</td><td>{c.kind}</td><td className="wide">{c.prompt}</td>
                    <td>{c.expected_skill ?? (c.should_trigger ? s.name : "none")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <EvalEmpty access={s.eval_access} ok="No test cases published" />}
      </section>

      <section aria-labelledby="rubric" className="panel">
        <h2 id="rubric">Rubric</h2>
        <p className="muted">Read only. Changes are reviewed in pull requests.</p>
        {s.rubric ? (
          <>
            <p>{s.rubric.reviewed ? "Reviewed by a person." : "Not yet reviewed."}</p>
            <div className="table-scroll" role="region" aria-label="Rubric criteria" tabIndex={0}>
              <table>
                <caption>Rubric criteria</caption>
                <thead>
                  <tr><th scope="col">ID</th><th scope="col">Type</th><th scope="col" className="num">Weight</th><th scope="col">Criterion</th></tr>
                </thead>
                <tbody>
                  {s.rubric.criteria.map((c) => (
                    <tr key={c.id}><td className="mono">{c.id}</td><td>{c.type}</td><td className="num">{c.weight}</td><td className="wide">{c.text}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        ) : <EvalEmpty access={s.eval_access} ok="No rubric published" />}
      </section>
    </article>
  );
}
