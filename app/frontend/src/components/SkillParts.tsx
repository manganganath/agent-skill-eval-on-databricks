// Panels a live skill page and a candidate page share: SKILL.md, files, test cases and rubric.
import type { EvalAccess, EvalFile, Rubric } from "../api/types";
import { EmptyState } from "./EmptyState";
import { Markdown } from "./Markdown";

export function EvalEmpty({ access, ok }: { access: EvalAccess; ok: string }) {
  if (access === "denied") return <EmptyState title="You can't read eval results for this department" />;
  if (access === "unavailable") return <EmptyState title="Eval results are temporarily unavailable" />;
  return <EmptyState title={ok} />;
}

export function SkillMdSection({ source }: { source: string | undefined }) {
  return (
    <section aria-labelledby="skill-md" className="panel">
      <h2 id="skill-md">Skill</h2>
      <p className="muted">SKILL.md: the instructions an agent follows when it uses this skill.</p>
      {source ? <div className="prose"><Markdown source={source} /></div> : <EmptyState title="No SKILL.md found" />}
    </section>
  );
}

export function FilesSection({ files }: { files: Record<string, string> }) {
  const paths = Object.keys(files).sort();
  return (
    <section aria-labelledby="files" className="panel">
      <h2 id="files">Files</h2>
      <p className="muted">Open a file to read it.</p>
      <ul aria-label="Files" className="file-list">
        {paths.map((p) => (
          <li key={p}>
            {p === "SKILL.md" ? <>{p} <span className="muted">shown above</span></> : (
              <details>
                <summary>{p}</summary>
                <pre>{files[p]}</pre>
              </details>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

export function TestCasesSection({ evals, name, access, note, empty }: {
  evals: EvalFile | null; name: string; access: EvalAccess; note: string; empty: string;
}) {
  return (
    <section aria-labelledby="tests" className="panel">
      <h2 id="tests">Test cases</h2>
      <p className="muted">{note}</p>
      {evals && evals.evals.length > 0 ? (
        <div className="table-scroll" role="region" aria-label="Test cases" tabIndex={0}>
          <table>
            <caption>Test cases</caption>
            <thead>
              <tr><th scope="col" className="num">ID</th><th scope="col">Kind</th><th scope="col">Prompt</th><th scope="col">Expected skill</th></tr>
            </thead>
            <tbody>
              {evals.evals.map((c) => (
                <tr key={c.id}>
                  <td className="num">{c.id}</td><td>{c.kind}</td><td className="wide">{c.prompt}</td>
                  <td>{c.expected_skill ?? (c.should_trigger ? name : "none")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <EvalEmpty access={access} ok={empty} />}
    </section>
  );
}

export function RubricSection({ rubric, access, note, empty }: {
  rubric: Rubric | null; access: EvalAccess; note: string; empty: string;
}) {
  return (
    <section aria-labelledby="rubric" className="panel">
      <h2 id="rubric">Rubric</h2>
      <p className="rubric-help">
        The criteria the judges use to score each answer in effectiveness mode, with and without the skill.
        Task-quality criteria check what the answer says; skill-format criteria check that it follows the skill's
        format. Each criterion has a weight from 1 to 5. An answer passes when the weight of the criteria it meets
        reaches {Math.round((rubric?.pass_score ?? 0.7) * 100)}% of the total and its case's assertions hold. Trigger
        mode does not use the rubric. Without one, effectiveness mode runs only trigger cases with assertions, and
        judges them on their assertions.
      </p>
      <p className="muted">{note}</p>
      {rubric ? (
        <>
          <p>{rubric.reviewed ? "Reviewed by a person." : "Not yet reviewed."}</p>
          <div className="table-scroll" role="region" aria-label="Rubric criteria" tabIndex={0}>
            <table>
              <caption>Rubric criteria</caption>
              <thead>
                <tr><th scope="col">ID</th><th scope="col">Type</th><th scope="col" className="num">Weight</th><th scope="col">Criterion</th></tr>
              </thead>
              <tbody>
                {rubric.criteria.map((c) => (
                  <tr key={c.id}><td className="mono">{c.id}</td><td>{c.type}</td><td className="num">{c.weight}</td><td className="wide">{c.text}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : <EvalEmpty access={access} ok={empty} />}
    </section>
  );
}
