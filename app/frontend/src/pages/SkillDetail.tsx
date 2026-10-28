import { Link, useParams } from "react-router-dom";
import { api, ApiError } from "../api/client";
import type { EvalAccess, JournalRow, Me, SkillDetail as SkillDetailT } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { Loading } from "../components/Loading";
import { Markdown } from "../components/Markdown";
import { TryPanel } from "../components/TryPanel";

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
    <section aria-labelledby="versions">
      <h2 id="versions">Version history</h2>
      {versions.loading && <Loading label="Loading versions" />}
      {versions.error ? <ErrorState error={versions.error} onRetry={versions.reload} /> : null}
      {rows && rows.length === 0 && <EmptyState title="No versions recorded yet" />}
      {rows && rows.length > 0 && (
        <>
          <p>{live === null ? "No version is live." : `Live version: ${live}`}</p>
          <table>
            <caption>Versions and actions, oldest first</caption>
            <thead>
              <tr>
                <th scope="col">Time</th><th scope="col">Action</th><th scope="col">Version</th>
                <th scope="col">Actor</th><th scope="col">Commit</th><th scope="col">Eval run</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((v, i) => (
                <tr key={`${v.ts}-${v.action}-${i}`}>
                  <td>{v.ts}</td><td>{v.action}</td><td>{v.version ?? v.to_version ?? "-"}</td>
                  <td>{v.actor}</td><td>{v.git_sha ?? "-"}</td><td>{v.eval_run_id ?? "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  );
}

export default function SkillDetail() {
  const { fqn = "" } = useParams(); // react-router has already decoded the param
  const skill = useAsync<SkillDetailT>(() => api.skill(fqn), [fqn]);
  const me = useAsync<Me>(() => api.me(), []);

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

  return (
    <article>
      <h1>{s.name}</h1>
      <p className="muted">{s.dept} · {s.fqn}</p>
      {s.description && <p>{s.description}</p>}
      <p>
        <Link to={`/matrix?skill=${encodeURIComponent(s.name)}&dept=${encodeURIComponent(s.dept)}`}>
          View evaluation matrix
        </Link>
      </p>
      {me.data?.is_reviewer && (
        <p><Link to={`/skills/${encodeURIComponent(s.fqn)}/switch`}>Switch version (reviewers)</Link></p>
      )}

      <section aria-labelledby="skill-md">
        <h2 id="skill-md">Skill</h2>
        {skillMd ? <Markdown source={skillMd} /> : <EmptyState title="No SKILL.md found" />}
      </section>

      <section aria-labelledby="files">
        <h2 id="files">Files</h2>
        <ul aria-label="Files">
          {paths.map((p) => (
            <li key={p}>
              {p === "SKILL.md" ? p : (
                <details>
                  <summary>{p}</summary>
                  <pre>{s.files[p]}</pre>
                </details>
              )}
            </li>
          ))}
        </ul>
      </section>

      <TryPanel fqn={s.fqn} evalAccess={s.eval_access} />

      <VersionHistory fqn={fqn} />

      <section aria-labelledby="tests">
        <h2 id="tests">Test cases</h2>
        <p className="muted">Read only. Changes are reviewed in pull requests.</p>
        {s.evals && s.evals.evals.length > 0 ? (
          <table>
            <caption>Test cases</caption>
            <thead>
              <tr><th scope="col">ID</th><th scope="col">Kind</th><th scope="col">Prompt</th><th scope="col">Expected skill</th></tr>
            </thead>
            <tbody>
              {s.evals.evals.map((c) => (
                <tr key={c.id}>
                  <td>{c.id}</td><td>{c.kind}</td><td>{c.prompt}</td>
                  <td>{c.expected_skill ?? (c.should_trigger ? s.name : "none")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <EvalEmpty access={s.eval_access} ok="No test cases published" />}
      </section>

      <section aria-labelledby="rubric">
        <h2 id="rubric">Rubric</h2>
        <p className="muted">Read only. Changes are reviewed in pull requests.</p>
        {s.rubric ? (
          <>
            <p>{s.rubric.reviewed ? "Reviewed by a person." : "Not yet reviewed."}</p>
            <table>
              <caption>Rubric criteria</caption>
              <thead>
                <tr><th scope="col">ID</th><th scope="col">Type</th><th scope="col">Weight</th><th scope="col">Criterion</th></tr>
              </thead>
              <tbody>
                {s.rubric.criteria.map((c) => (
                  <tr key={c.id}><td>{c.id}</td><td>{c.type}</td><td>{c.weight}</td><td>{c.text}</td></tr>
                ))}
              </tbody>
            </table>
          </>
        ) : <EvalEmpty access={s.eval_access} ok="No rubric published" />}
      </section>
    </article>
  );
}
