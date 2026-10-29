import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { SkillSummary, Verdict } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { Loading } from "../components/Loading";
import { VerdictChip } from "../components/VerdictChip";

const SEARCH_DEBOUNCE_MS = 250;

// "none" picks skills with no results yet. Any other value picks skills where at least one model has that verdict.
type VerdictFilter = "" | Verdict | "none";
const VERDICT_OPTIONS: { value: VerdictFilter; label: string }[] = [
  { value: "", label: "Any" },
  { value: "pass", label: "Pass" },
  { value: "warn", label: "Warn" },
  { value: "fail", label: "Fail" },
  { value: "inconclusive", label: "Inconclusive" },
  { value: "advisory", label: "Advisory" },
  { value: "none", label: "No results yet" },
];

function matchesVerdict(s: SkillSummary, verdict: VerdictFilter): boolean {
  if (verdict === "") return true;
  const values = Object.values(s.verdicts);
  return verdict === "none" ? values.length === 0 : values.includes(verdict);
}

function groupByDept(skills: SkillSummary[]): Map<string, SkillSummary[]> {
  const groups = new Map<string, SkillSummary[]>();
  for (const s of skills) groups.set(s.dept, [...(groups.get(s.dept) ?? []), s]);
  return groups;
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

// `showDept` is off inside a department section, whose heading already names the department.
export function SkillCard({ s, showDept = true }: { s: SkillSummary; showDept?: boolean }) {
  const entries = Object.entries(s.verdicts);
  return (
    <li className="skill-card">
      <div className="skill-card-head">
        <h3><Link className="card-link" to={`/skills/${encodeURIComponent(s.fqn)}`}>{s.name}</Link></h3>
        {s.live_version === null ? (
          <span className="version version-none">No live version</span>
        ) : (
          <span className="version">
            <span aria-hidden="true">v{s.live_version}</span>
            <span className="visually-hidden">Live version {s.live_version}</span>
          </span>
        )}
      </div>
      {showDept && <p className="skill-card-dept">{s.dept}</p>}
      {s.description && <p className="skill-card-desc">{s.description}</p>}
      {s.eval_access !== "ok" && (
        <p className="card-note" role="note">
          {s.eval_access === "denied"
            ? "You can't read eval results for this department"
            : "Eval results are temporarily unavailable"}
        </p>
      )}
      <ul className="scorecard" aria-label={`Latest verdicts for ${s.name}`}>
        {entries.map(([model, verdict]) => (
          <li key={model}><span className="model">{model}</span> <VerdictChip verdict={verdict} /></li>
        ))}
        {entries.length === 0 && <li className="muted">No results yet</li>}
      </ul>
    </li>
  );
}

export default function Catalog() {
  const [input, setInput] = useState("");
  const [q, setQ] = useState("");
  const [dept, setDept] = useState("");
  const [verdict, setVerdict] = useState<VerdictFilter>("");
  const [total, setTotal] = useState<number | null>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const t = setTimeout(() => setQ(input.trim()), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [input]);

  const { data, error, loading, reload } = useAsync<SkillSummary[]>(() => api.skills(q || undefined), [q]);

  // The live count is the unsearched list, so a search does not change it.
  useEffect(() => {
    if (!loading && !error && q === "" && data) setTotal(data.length);
  }, [data, error, loading, q]);

  const all = data ?? [];
  // Departments come from the loaded skills, so the list only offers departments the user can see.
  // The chosen one stays listed even when the current search returns none of its skills.
  const departments = [...new Set([...all.map((s) => s.dept), ...(dept ? [dept] : [])])];
  const visible = all.filter((s) => (dept === "" || s.dept === dept) && matchesVerdict(s, verdict));
  const groups = groupByDept(visible);
  const filtering = q !== "" || dept !== "" || verdict !== "";
  const settled = !loading && !error && data !== undefined;
  const nothingLoaded = settled && all.length === 0 && q === "";
  const noMatches = settled && !nothingLoaded && visible.length === 0;

  function clearFilters() {
    setInput("");
    setQ("");
    setDept("");
    setVerdict("");
    searchRef.current?.focus(); // the button goes away with the empty state, so focus moves to the search box
  }

  return (
    <>
      <div className="page-head">
        <div className="title-row">
          <h1>Skills</h1>
          {total !== null && total > 0 && <span className="count-pill">{total} live</span>}
        </div>
      </div>
      <div className="catalog-tools">
        <div className="field search">
          <label htmlFor="skill-search">Search skills</label>
          <input
            id="skill-search"
            ref={searchRef}
            type="search"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Name or description"
          />
        </div>
        <div className="field">
          <label htmlFor="dept-filter">Department</label>
          <select id="dept-filter" value={dept} onChange={(e) => setDept(e.target.value)}>
            <option value="">All</option>
            {departments.map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
        </div>
        <div className="field">
          <label htmlFor="verdict-filter">Verdict</label>
          <select id="verdict-filter" value={verdict} onChange={(e) => setVerdict(e.target.value as VerdictFilter)}>
            {VERDICT_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        </div>
        <p className="result-count" aria-live="polite">
          {settled && filtering && !noMatches ? `Showing ${visible.length} of ${total ?? all.length}` : ""}
        </p>
      </div>
      {loading && <Loading label="Loading skills" />}
      {error ? <ErrorState error={error} onRetry={reload} /> : null}
      {nothingLoaded && (
        <EmptyState title="No skills are live yet">
          <p>
            A skill appears here after its pull request merges and a reviewer approves the release.
            If you expected to see skills, ask your team lead for access to your department.
          </p>
        </EmptyState>
      )}
      {noMatches && (
        <EmptyState title="No skills match your filters">
          <p>Try another search or filter, or clear them to see every skill you can use.</p>
          <button type="button" onClick={clearFilters}>Clear filters</button>
        </EmptyState>
      )}
      {!error && [...groups.entries()].map(([d, items]) => (
        <section key={d} aria-labelledby={`dept-${d}`} className="dept-group">
          <div className="group-head">
            <h2 id={`dept-${d}`}>{d}</h2>
            <span>{plural(items.length, "skill")}</span>
          </div>
          <ul className="cards">
            {items.map((s) => <SkillCard key={s.fqn} s={s} showDept={false} />)}
          </ul>
        </section>
      ))}
    </>
  );
}
