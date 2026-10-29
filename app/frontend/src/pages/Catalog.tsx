import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { CandidateSummary, EvalAccess, Me, SkillSummary, Verdict } from "../api/types";
import { candidatePath, canSeeCandidates } from "../candidates";
import { useAsync } from "../hooks/useAsync";
import { CandidateBadge } from "../components/CandidateBadge";
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

// Live skills, skills under review (pull request candidates and drafts), or both. Only authors and reviewers see
// skills under review, so only they get this filter.
type StatusFilter = "all" | "live" | "candidate";
const STATUS_OPTIONS: { value: StatusFilter; label: string }[] = [
  { value: "all", label: "All" },
  { value: "live", label: "Live" },
  { value: "candidate", label: "Under review" },
];

function matchesVerdict(s: { verdicts: Record<string, Verdict> }, verdict: VerdictFilter): boolean {
  if (verdict === "") return true;
  const values = Object.values(s.verdicts);
  return verdict === "none" ? values.length === 0 : values.includes(verdict);
}

type DeptGroup = { live: SkillSummary[]; review: CandidateSummary[] };

// One section per department, live skills first and skills under review after them. Departments with live skills
// keep the order they loaded in; departments with only skills under review follow.
function groupByDept(skills: SkillSummary[], candidates: CandidateSummary[]): Map<string, DeptGroup> {
  const groups = new Map<string, DeptGroup>();
  const group = (dept: string) => {
    if (!groups.has(dept)) groups.set(dept, { live: [], review: [] });
    return groups.get(dept) as DeptGroup;
  };
  for (const s of skills) group(s.dept).live.push(s);
  for (const c of candidates) group(c.dept).review.push(c);
  return groups;
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

// The live search runs on the server; candidates are few, so their search runs here, on the same fields.
function matchesText(c: CandidateSummary, q: string): boolean {
  const needle = q.toLowerCase();
  return !needle || c.name.toLowerCase().includes(needle) || (c.description ?? "").toLowerCase().includes(needle);
}

function Scorecard({ label, verdicts, access }: { label: string; verdicts: Record<string, Verdict>; access: EvalAccess }) {
  const entries = Object.entries(verdicts);
  return (
    <>
      {access !== "ok" && (
        <p className="card-note" role="note">
          {access === "denied"
            ? "You can't read eval results for this department"
            : "Eval results are temporarily unavailable"}
        </p>
      )}
      <ul className="scorecard" aria-label={label}>
        {entries.map(([model, verdict]) => (
          <li key={model}><span className="model">{model}</span> <VerdictChip verdict={verdict} /></li>
        ))}
        {entries.length === 0 && <li className="muted">No results yet</li>}
      </ul>
    </>
  );
}

// `showDept` is off inside a department section, whose heading already names the department.
export function SkillCard({ s, showDept = true }: { s: SkillSummary; showDept?: boolean }) {
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
      <Scorecard label={`Latest verdicts for ${s.name}`} verdicts={s.verdicts} access={s.eval_access} />
    </li>
  );
}

// The live card's shape, marked as unreleased: the badge names the pull request, and a dashed edge sets it apart.
export function CandidateCard({ c, showDept = true }: { c: CandidateSummary; showDept?: boolean }) {
  return (
    <li className="skill-card skill-card-candidate">
      <div className="skill-card-head">
        <h3><Link className="card-link" to={candidatePath(c)}>{c.name}</Link></h3>
        <CandidateBadge kind={c.kind} pr={c.pr_number} />
      </div>
      {showDept && <p className="skill-card-dept">{c.dept}</p>}
      {c.description && <p className="skill-card-desc">{c.description}</p>}
      <Scorecard
        label={`Latest verdicts for ${c.name}, ${c.kind === "draft" ? "draft" : `in PR #${c.pr_number}`}`}
        verdicts={c.verdicts} access={c.eval_access}
      />
    </li>
  );
}

export default function Catalog() {
  const [input, setInput] = useState("");
  const [q, setQ] = useState("");
  const [dept, setDept] = useState("");
  const [verdict, setVerdict] = useState<VerdictFilter>("");
  const [status, setStatus] = useState<StatusFilter>("all");
  const [total, setTotal] = useState<number | null>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const t = setTimeout(() => setQ(input.trim()), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [input]);

  const { data, error, loading, reload } = useAsync<SkillSummary[]>(() => api.skills(q || undefined), [q]);
  // One /api/me request per page load, shared with the header. Candidates are asked for only by authors and reviewers.
  const me = useAsync<Me>(() => api.me(), []);
  const reviewing = canSeeCandidates(me.data);
  const cands = useAsync<CandidateSummary[]>(
    () => (reviewing ? api.candidates() : Promise.resolve([])), [reviewing],
  );

  // The live count is the unsearched list, so a search does not change it.
  useEffect(() => {
    if (!loading && !error && q === "" && data) setTotal(data.length);
  }, [data, error, loading, q]);

  const all = data ?? [];
  // Departments come from the loaded skills, so the list only offers departments the user can see.
  // The chosen one stays listed even when the current search returns none of its skills.
  const candidates = reviewing ? cands.data ?? [] : [];
  const departments = [
    ...new Set([...all.map((s) => s.dept), ...candidates.map((c) => c.dept), ...(dept ? [dept] : [])]),
  ];
  const showLive = status !== "candidate";
  const showCandidates = reviewing && status !== "live";
  const visible = showLive ? all.filter((s) => (dept === "" || s.dept === dept) && matchesVerdict(s, verdict)) : [];
  const visibleCandidates = showCandidates
    ? candidates.filter((c) => matchesText(c, q) && (dept === "" || c.dept === dept) && matchesVerdict(c, verdict))
    : [];
  const groups = groupByDept(visible, visibleCandidates);
  const filtering = q !== "" || dept !== "" || verdict !== "";
  const settled = !loading && !error && data !== undefined;
  const candidatesSettled = showCandidates && !cands.loading && !cands.error;
  const nothingLoaded = showLive && settled && all.length === 0 && q === "";
  // A candidate that matches is a match too, so "no skills match" never sits next to one.
  const noMatches = showLive && settled && !nothingLoaded && visible.length === 0 && visibleCandidates.length === 0;

  function clearFilters() {
    setInput("");
    setQ("");
    setDept("");
    setVerdict("");
    setStatus("all");
    searchRef.current?.focus(); // the button goes away with the empty state, so focus moves to the search box
  }

  return (
    <>
      <div className="page-head">
        <div className="title-row">
          <h1>Skills</h1>
          {total !== null && total > 0 && <span className="count-pill">{total} live</span>}
          {candidates.length > 0 && <span className="count-pill count-pill-review">{candidates.length} under review</span>}
        </div>
        {reviewing && <Link className="btn" to="/drafts/new">Upload draft</Link>}
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
        {reviewing && (
          <div className="field">
            <label htmlFor="status-filter">Status</label>
            <select id="status-filter" value={status} onChange={(e) => setStatus(e.target.value as StatusFilter)}>
              {STATUS_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
          </div>
        )}
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
          {showLive && settled && filtering && !noMatches
            ? `Showing ${visible.length + visibleCandidates.length} of ${(total ?? all.length) + (showCandidates ? candidates.length : 0)}`
            : ""}
        </p>
      </div>
      {showLive && loading && <Loading label="Loading skills" />}
      {showLive && error ? <ErrorState error={error} onRetry={reload} /> : null}
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
      {showCandidates && cands.loading && <Loading label="Loading skills under review" />}
      {showCandidates && cands.error ? <ErrorState error={cands.error} onRetry={cands.reload} /> : null}
      {!error && [...groups.entries()].map(([d, { live, review }]) => (
        <section key={d} aria-labelledby={`dept-${d}`} className="dept-group">
          <div className="group-head">
            <h2 id={`dept-${d}`}>{d}</h2>
            <span>
              {review.length > 0 ? `${live.length} live, ${review.length} under review` : plural(live.length, "skill")}
            </span>
          </div>
          <ul className="cards">
            {live.map((s) => <SkillCard key={s.fqn} s={s} showDept={false} />)}
            {review.map((c) => <CandidateCard key={`${c.run_key}/${c.dept}/${c.name}`} c={c} showDept={false} />)}
          </ul>
        </section>
      ))}
      {status === "candidate" && candidatesSettled && candidates.length === 0 && (
        <EmptyState title="No skills are under review">
          <p>A skill appears here when a pull request adds or changes it and CI uploads it for evaluation.</p>
        </EmptyState>
      )}
      {status === "candidate" && candidatesSettled && candidates.length > 0 && visibleCandidates.length === 0 && (
        <EmptyState title="No candidates match your filters">
          <p>Try another search or filter, or clear them to see every skill under review.</p>
          <button type="button" onClick={clearFilters}>Clear filters</button>
        </EmptyState>
      )}
    </>
  );
}
