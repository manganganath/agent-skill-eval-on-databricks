import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { SkillSummary } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { Loading } from "../components/Loading";
import { VerdictChip } from "../components/VerdictChip";

const SEARCH_DEBOUNCE_MS = 250;

function groupByDept(skills: SkillSummary[]): Map<string, SkillSummary[]> {
  const groups = new Map<string, SkillSummary[]>();
  for (const s of skills) groups.set(s.dept, [...(groups.get(s.dept) ?? []), s]);
  return groups;
}

export default function Catalog() {
  const [input, setInput] = useState("");
  const [q, setQ] = useState("");

  useEffect(() => {
    const t = setTimeout(() => setQ(input.trim()), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [input]);

  const { data, error, loading, reload } = useAsync<SkillSummary[]>(() => api.skills(q || undefined), [q]);
  const groups = groupByDept(data ?? []);
  const empty = !loading && !error && data !== undefined && data.length === 0;

  return (
    <>
      <h1>Skills</h1>
      <p>
        <label htmlFor="skill-search">Search skills</label>{" "}
        <input
          id="skill-search"
          type="search"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Name or description"
        />
      </p>
      {loading && <Loading label="Loading skills" />}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {empty && q === "" && (
        <EmptyState title="No skills to show">
          <p>You may not have access to any skills yet, or none are registered. Ask your team lead for access.</p>
        </EmptyState>
      )}
      {empty && q !== "" && (
        <EmptyState title="No skills match your search">
          <p>Try a different word, or clear the search box to see every skill you can use.</p>
        </EmptyState>
      )}
      {!error && [...groups.entries()].map(([dept, items]) => (
        <section key={dept} aria-labelledby={`dept-${dept}`}>
          <h2 id={`dept-${dept}`}>{dept}</h2>
          <ul className="cards">
            {items.map((s) => {
              const entries = Object.entries(s.verdicts);
              return (
                <li key={s.fqn}>
                  <h3><Link to={`/skills/${encodeURIComponent(s.fqn)}`}>{s.name}</Link></h3>
                  {s.description && <p>{s.description}</p>}
                  <p className="muted">
                    {s.live_version === null ? "No live version" : `Live version ${s.live_version}`}
                  </p>
                  {s.eval_access !== "ok" && (
                    <p className="muted" role="note">
                      {s.eval_access === "denied"
                        ? "You can't read eval results for this department"
                        : "Eval results are temporarily unavailable"}
                    </p>
                  )}
                  <ul className="chips" aria-label={`Latest verdicts for ${s.name}`}>
                    {entries.map(([model, verdict]) => (
                      <li key={model}><span className="model">{model}</span> <VerdictChip verdict={verdict} /></li>
                    ))}
                    {entries.length === 0 && <li className="muted">No results yet</li>}
                  </ul>
                </li>
              );
            })}
          </ul>
        </section>
      ))}
    </>
  );
}
