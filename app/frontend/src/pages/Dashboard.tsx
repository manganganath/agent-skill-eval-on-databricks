import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { Dashboard as DashboardData, DashboardPassRateTrend, DashboardTriggerTrend, Verdict } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { Loading } from "../components/Loading";
import { verdictText } from "../components/VerdictChip";
import { GroupedBars, Legend, LineChart, StackedBars, dayLabel, type LineSeries } from "../components/Charts";

const ALL = "all";
const VERDICTS: Verdict[] = ["pass", "warn", "fail", "inconclusive", "advisory"];
// Verdict fills are the chip colours, so a verdict reads the same here as in the matrix.
const VERDICT_FILL: Record<Verdict, string> = {
  pass: "var(--pass)", warn: "var(--warn)", fail: "var(--fail)", inconclusive: "var(--info)", advisory: "var(--line-strong)",
};
// Trigger outcomes: the four correct and wrong routings, in a fixed order. Unknown outcomes fall to --info.
const OUTCOMES = ["correct", "correct_reject", "wrong_skill", "missed", "false_trigger"];
const OUTCOME_TEXT: Record<string, string> = {
  correct: "Correct", correct_reject: "Correct reject", wrong_skill: "Wrong skill", missed: "Missed", false_trigger: "False trigger",
};
const OUTCOME_FILL: Record<string, string> = {
  correct: "var(--pass)", correct_reject: "var(--outcome-correct-reject)", wrong_skill: "var(--fail)",
  missed: "var(--warn)", false_trigger: "var(--outcome-false-trigger)",
};

const pct = (v: number | null) => (v == null ? "-" : `${Math.round(v * 100)}%`);
const signedPct = (v: number | null) => (v == null ? "-" : `${v > 0 ? "+" : ""}${Math.round(v * 100)}%`);
const count = (n: number) => n.toLocaleString("en");
const versionLabel = (v: number) => (v > 0 ? `v${v}` : "unpublished");
const uniq = <T,>(xs: T[]): T[] => [...new Set(xs)];
const seriesColor = (i: number) => (i < 8 ? `var(--series-${i + 1})` : "var(--info)"); // eight slots, then one neutral
// The mean of values, each weighted by its row count. Rows without a value are left out of both sums.
function weightedMean(rows: { value: number | null; weight: number }[]): number | null {
  const parts = rows.filter((r) => r.value != null && r.weight > 0);
  const weight = parts.reduce((s, r) => s + r.weight, 0);
  return weight === 0 ? null : parts.reduce((s, r) => s + (r.value as number) * r.weight, 0) / weight;
}

function HiddenNote({ departments }: { departments: string[] }) {
  if (departments.length === 0) return null;
  return (
    <p role="note" className="notice">
      Some departments' results are hidden: you can't read {departments.join(", ")}.
    </p>
  );
}

function Tile({ label, value }: { label: string; value: string }) {
  return (
    <div className="dash-tile">
      <p className="dash-tile-label">{label}</p>
      <p className="dash-tile-value">{value}</p>
    </div>
  );
}

// The table behind a chart. Closed by default, so the page stays short for sighted readers. A table that carries
// the page's links (uplift) opens by default, so the links are visible.
function TableAlt({ label, children, open = false }: { label: string; children: ReactNode; open?: boolean }) {
  return (
    <details className="dash-table-alt" open={open}>
      <summary>{label}</summary>
      <div className="table-scroll" role="region" aria-label={label} tabIndex={0}>{children}</div>
    </details>
  );
}

export default function Dashboard() {
  const dash = useAsync<DashboardData>(() => api.dashboard(), []);
  const [dept, setDept] = useState(ALL);
  const [model, setModel] = useState(ALL);
  const [trendSkill, setTrendSkill] = useState(ALL);
  const [trendVersion, setTrendVersion] = useState(ALL);

  if (dash.loading) return <Loading label="Loading dashboard" />;
  if (dash.error) return <ErrorState error={dash.error} onRetry={dash.reload} />;
  const d = dash.data;
  if (!d) return null;

  const allRows = [...d.verdicts, ...d.uplift, ...d.routing, ...d.trigger_trend, ...d.pass_rate_trend, ...d.health];
  const depts = uniq(allRows.map((r) => r.dept)).sort();
  const models = uniq([...allRows.map((r) => r.model_profile), ...(d.tokens ?? []).map((r) => r.model_profile)]).sort();
  const modelColor = (m: string) => seriesColor(models.indexOf(m));
  const multiDept = depts.length > 1;
  const skillLabel = (r: { dept: string; skill: string }) => (multiDept ? `${r.skill} (${r.dept})` : r.skill);

  const keep = (r: { dept?: string; model_profile: string }) =>
    (dept === ALL || r.dept === dept) && (model === ALL || r.model_profile === model);
  const verdicts = d.verdicts.filter(keep);
  const uplift = d.uplift.filter(keep);
  const routing = d.routing.filter(keep);
  const health = d.health.filter(keep);
  const tokens = (d.tokens ?? []).filter((r) => model === ALL || r.model_profile === model);

  // Trend filters: skill and version narrow the two trend charts only.
  const trendRows = [...d.trigger_trend, ...d.pass_rate_trend].filter(keep);
  const trendSkills = uniq(trendRows.map((r) => r.skill)).sort();
  const skillPick = trendSkills.includes(trendSkill) ? trendSkill : ALL;
  const trendVersions = uniq(trendRows.filter((r) => skillPick === ALL || r.skill === skillPick).map((r) => r.skill_version)).sort((a, b) => a - b);
  const versionPick = trendVersions.some((v) => String(v) === trendVersion) ? trendVersion : ALL;
  const trendKeep = (r: { skill: string; skill_version: number }) =>
    (skillPick === ALL || r.skill === skillPick) && (versionPick === ALL || String(r.skill_version) === versionPick);

  const hasResults = d.verdicts.length > 0 || (d.tokens ?? []).length > 0;
  const s = d.summary;

  // Verdicts by model.
  const verdictRows = models.filter((m) => verdicts.some((r) => r.model_profile === m)).map((m) => ({
    label: m,
    segments: VERDICTS.map((v) => ({
      name: verdictText(v), color: VERDICT_FILL[v],
      value: verdicts.filter((r) => r.model_profile === m && r.verdict === v).reduce((n, r) => n + r.n, 0),
    })),
  }));

  // Uplift: one group per skill, one bar per model, on the task-quality uplift.
  const upliftKeys = uniq(uplift.map((r) => `${r.dept}\u0000${r.skill}\u0000${r.skill_version}`));
  const upliftGroups = upliftKeys.map((key) => {
    const [rowDept, rowSkill] = key.split("\u0000");
    const rows = uplift.filter((r) => r.dept === rowDept && r.skill === rowSkill);
    return {
      label: skillLabel({ dept: rowDept, skill: rowSkill }),
      bars: models.filter((m) => rows.some((r) => r.model_profile === m)).map((m) => {
        const row = rows.find((r) => r.model_profile === m);
        return { name: m, color: modelColor(m), value: row?.uplift_task_quality ?? null };
      }),
    };
  });
  const upliftValues = upliftGroups.flatMap((g) => g.bars.map((b) => b.value)).filter((v): v is number => v != null);
  const upliftDomain: [number, number] = [Math.min(0, ...upliftValues), Math.max(0, ...upliftValues, 0.01)];

  // Trigger routing: outcomes per expected skill, and the wrong routings with the most cases.
  const outcomeNames = uniq(routing.map((r) => r.trigger_outcome ?? "none"));
  const knownOutcomes = [...OUTCOMES.filter((o) => outcomeNames.includes(o)), ...outcomeNames.filter((o) => !OUTCOMES.includes(o))];
  const outcomeText = (o: string) => OUTCOME_TEXT[o] ?? (o === "none" ? "No outcome" : o);
  const outcomeFill = (o: string) => OUTCOME_FILL[o] ?? "var(--info)";
  // "-" stands for the cases that expect no skill: unrelated requests, and near-misses aimed at another skill.
  const expectedText = (e: string) => (e === "-" ? "No skill expected" : e);
  const expectedKeys = uniq(routing.map((r) => `${r.dept}\u0000${r.expected_skill ?? "-"}`)).sort();
  const routeRows = expectedKeys.map((key) => {
    const [rowDept, expected] = key.split("\u0000");
    const rows = routing.filter((r) => r.dept === rowDept && (r.expected_skill ?? "-") === expected);
    return {
      label: multiDept ? `${expectedText(expected)} (${rowDept})` : expectedText(expected),
      segments: knownOutcomes.map((o) => ({
        name: outcomeText(o), color: outcomeFill(o),
        value: rows.filter((r) => (r.trigger_outcome ?? "none") === o).reduce((n, r) => n + r.n, 0),
      })),
    };
  });
  const misroutes = uniq(routing.filter((r) => r.trigger_outcome !== "correct" && r.trigger_outcome !== "correct_reject")
    .map((r) => `${r.dept}\u0000${r.expected_skill ?? "-"}\u0000${r.invoked_skill ?? "none"}\u0000${r.trigger_outcome ?? "none"}`))
    .map((key) => {
      const [rowDept, expected, invoked, outcome] = key.split("\u0000");
      const n = routing.filter((r) => r.dept === rowDept && (r.expected_skill ?? "-") === expected
        && (r.invoked_skill ?? "none") === invoked && (r.trigger_outcome ?? "none") === outcome)
        .reduce((sum, r) => sum + r.n, 0);
      return { rowDept, expected, invoked, outcome, n };
    })
    .sort((a, b) => b.n - a.n)
    .slice(0, 10);

  // Trend charts: one line per model, averaged over the selected skills and versions, weighted by runs.
  const trendLines = <T extends DashboardTriggerTrend | DashboardPassRateTrend>(rows: T[], value: (r: T) => number | null) => {
    const days = uniq(rows.map((r) => r.day)).sort();
    const lines: LineSeries[] = models.filter((m) => rows.some((r) => r.model_profile === m)).map((m) => ({
      name: m, color: modelColor(m),
      values: days.map((day) => weightedMean(rows.filter((r) => r.model_profile === m && r.day === day)
        .map((r) => ({ value: value(r), weight: r.runs })))),
    }));
    return { days, lines };
  };
  const triggerKept = d.trigger_trend.filter((r) => keep(r) && trendKeep(r));
  const passKept = d.pass_rate_trend.filter((r) => keep(r) && trendKeep(r));
  const trigger = trendLines(triggerKept, (r) => r.accuracy);
  const pass = trendLines(passKept, (r) => r.pass_rate);
  const trendTable = <T extends DashboardTriggerTrend | DashboardPassRateTrend>(
    rows: T[], value: (r: T) => number | null, head: string,
  ) => (
    <table className="matrix">
      <caption>{head}</caption>
      <thead>
        <tr>
          <th scope="col">Day</th><th scope="col">Skill</th><th scope="col">Version</th><th scope="col">Model</th>
          <th scope="col" className="num">{head}</th><th scope="col" className="num">Runs</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={`${r.day}-${r.skill}-${r.skill_version}-${r.model_profile}-${i}`}>
            <td className="nowrap">{dayLabel(r.day)}</td><td>{r.skill}</td><td>{versionLabel(r.skill_version)}</td>
            <td>{r.model_profile}</td>
            <td className="num">{pct(value(r))}</td>
            <td className="num">{count(r.runs)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );

  // Health by model: error and inconclusive rates over each skill's live-version rows, weighted by row count.
  const healthRows = models.filter((m) => health.some((r) => r.model_profile === m)).map((m) => {
    const rows = health.filter((r) => r.model_profile === m);
    const cells = rows.reduce((n, r) => n + r.cells, 0);
    return {
      model: m, cells,
      error: weightedMean(rows.map((r) => ({ value: r.error_rate, weight: r.cells }))),
      inconclusive: weightedMean(rows.map((r) => ({ value: r.inconclusive_rate, weight: r.cells }))),
    };
  });
  const healthMax = Math.max(0.05, ...healthRows.flatMap((r) => [r.error ?? 0, r.inconclusive ?? 0]));

  // Tokens per day per model, summed over the model's endpoints.
  const tokenDays = uniq(tokens.map((r) => r.day)).sort();
  const tokenLines: LineSeries[] = models.filter((m) => tokens.some((r) => r.model_profile === m)).map((m) => ({
    name: m, color: modelColor(m),
    values: tokenDays.map((day) => {
      const rows = tokens.filter((r) => r.model_profile === m && r.day === day);
      return rows.length === 0 ? null : rows.reduce((n, r) => n + (r.input_tokens ?? 0), 0);
    }),
  }));
  const tokenMax = Math.max(1, ...tokenLines.flatMap((l) => l.values.map((v) => v ?? 0)));
  const tokenTop = Math.ceil(tokenMax * 1.1);

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Dashboard</h1>
          <p className="lede">Evaluation results across the departments you can read.</p>
        </div>
      </div>
      <div className="notices">
        <HiddenNote departments={d.deniedDepartments} />
      </div>
      {!hasResults ? (
        <EmptyState title="No results yet">
          <p>No evaluation has run for the departments you can read. Start one from Run eval.</p>
        </EmptyState>
      ) : (
        <>
          <section aria-label="Summary" className="dash-tiles">
            <Tile label="Skills with results" value={count(s.skills)} />
            <Tile label="Pass" value={count(s.verdicts.pass)} />
            <Tile label="Warn" value={count(s.verdicts.warn)} />
            <Tile label="Fail" value={count(s.verdicts.fail)} />
            <Tile label="Inconclusive" value={count(s.verdicts.inconclusive)} />
            <Tile label="Trigger accuracy" value={pct(s.trigger_accuracy)} />
            <Tile label="Task-quality uplift" value={signedPct(s.uplift_task_quality)} />
            <Tile label="Error rate" value={pct(s.error_rate)} />
          </section>
          <p className="muted dash-caption">The tiles cover every department and model. The sections below follow the filters.</p>

          <div className="dash-filters">
            <div className="field">
              <label htmlFor="dash-dept">Department</label>
              <select id="dash-dept" value={dept} onChange={(e) => setDept(e.target.value)}>
                <option value={ALL}>All departments</option>
                {depts.map((x) => <option key={x} value={x}>{x}</option>)}
              </select>
            </div>
            <div className="field">
              <label htmlFor="dash-model">Model</label>
              <select id="dash-model" value={model} onChange={(e) => setModel(e.target.value)}>
                <option value={ALL}>All models</option>
                {models.map((m) => <option key={m} value={m}>{m}</option>)}
              </select>
            </div>
          </div>

          <section className="panel" aria-labelledby="verdicts-heading">
            <h2 id="verdicts-heading">Verdicts by model</h2>
            <p className="muted">Each skill's live version, by model and mode, without the baseline runs.</p>
            {verdictRows.length === 0 ? <p className="muted">No verdicts for this selection.</p> : (
              <>
                <Legend items={VERDICTS.map((v) => ({ name: verdictText(v), color: VERDICT_FILL[v], shape: "square" as const }))} />
                <StackedBars label="Verdicts by model, stacked counts" rows={verdictRows} format={count} />
                <TableAlt label="Verdict counts by model">
                  <table className="matrix">
                    <caption>Verdict counts by model</caption>
                    <thead><tr>
                      <th scope="col">Model</th>
                      {VERDICTS.map((v) => <th key={v} scope="col" className="num">{verdictText(v)}</th>)}
                    </tr></thead>
                    <tbody>
                      {verdictRows.map((row) => (
                        <tr key={row.label}>
                          <td>{row.label}</td>
                          {row.segments.map((seg) => <td key={seg.name} className="num">{count(seg.value)}</td>)}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </TableAlt>
              </>
            )}
          </section>

          <section className="panel" aria-labelledby="uplift-heading">
            <h2 id="uplift-heading">Uplift with and without the skill</h2>
            <p className="muted">Each skill's live version. Task-quality uplift is the gain with the skill over the same model without it.</p>
            {uplift.length === 0 ? <p className="muted">No with-skill results for this selection.</p> : (
              <>
                {upliftGroups.length > 0 && (
                  <>
                    <Legend items={models.filter((m) => uplift.some((r) => r.model_profile === m))
                      .map((m) => ({ name: m, color: modelColor(m), shape: "square" as const }))} />
                    <GroupedBars label="Task-quality uplift by skill and model" groups={upliftGroups}
                      domain={upliftDomain} format={signedPct} />
                  </>
                )}
                <TableAlt label="Uplift rows">
                  <table className="matrix">
                    <caption>Uplift with and without the skill, by skill and model</caption>
                    <thead><tr>
                      <th scope="col">Skill</th><th scope="col">Version</th><th scope="col">Model</th>
                      <th scope="col" className="num">Without skill</th><th scope="col" className="num">With skill</th>
                      <th scope="col" className="num">Uplift (task)</th><th scope="col" className="num">Uplift (format)</th>
                    </tr></thead>
                    <tbody>
                      {uplift.map((r) => (
                        <tr key={`${r.dept}-${r.skill}-${r.skill_version}-${r.model_profile}`}>
                          <td>
                            <Link to={`/matrix?skill=${encodeURIComponent(r.skill)}&dept=${encodeURIComponent(r.dept)}`}>
                              {skillLabel(r)}
                            </Link>
                          </td>
                          <td className="nowrap">{versionLabel(r.skill_version)}</td>
                          <td>{r.model_profile}</td>
                          <td className="num">{pct(r.pass_rate_without)}</td>
                          <td className="num">{pct(r.pass_rate)}</td>
                          <td className="num">{signedPct(r.uplift_task_quality)}</td>
                          <td className="num">{signedPct(r.uplift_skill_format)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </TableAlt>
              </>
            )}
          </section>

          <section className="panel" aria-labelledby="routing-heading">
            <h2 id="routing-heading">Trigger routing</h2>
            <p className="muted">Each trigger case, by the skill it should have invoked and what happened.</p>
            {routeRows.length === 0 ? <p className="muted">No trigger cases for this selection.</p> : (
              <>
                <Legend items={knownOutcomes.map((o) => ({ name: outcomeText(o), color: outcomeFill(o), shape: "square" as const }))} />
                <StackedBars label="Trigger outcomes by expected skill" rows={routeRows} format={count} />
                <TableAlt label="Trigger outcomes by expected skill">
                  <table className="matrix">
                    <caption>Trigger outcomes by expected skill</caption>
                    <thead><tr>
                      <th scope="col">Expected skill</th>
                      {knownOutcomes.map((o) => <th key={o} scope="col" className="num">{outcomeText(o)}</th>)}
                    </tr></thead>
                    <tbody>
                      {routeRows.map((row) => (
                        <tr key={row.label}>
                          <td>{row.label}</td>
                          {row.segments.map((seg) => <td key={seg.name} className="num">{count(seg.value)}</td>)}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </TableAlt>
                <h3 className="dash-subhead">Top mis-routes</h3>
                {misroutes.length === 0 ? <p className="muted">No mis-routes for this selection.</p> : (
                  <div className="table-scroll" role="region" aria-label="Top mis-routes" tabIndex={0}>
                    <table className="matrix">
                      <caption>Top mis-routes: expected skill, invoked skill and outcome</caption>
                      <thead><tr>
                        <th scope="col">Expected skill</th><th scope="col">Invoked skill</th>
                        <th scope="col">Outcome</th><th scope="col" className="num">Cases</th>
                      </tr></thead>
                      <tbody>
                        {misroutes.map((m) => (
                          <tr key={`${m.rowDept}-${m.expected}-${m.invoked}-${m.outcome}`}>
                            <td>{multiDept ? `${expectedText(m.expected)} (${m.rowDept})` : expectedText(m.expected)}</td>
                            <td>{m.invoked === "none" ? "None" : m.invoked}</td>
                            <td>{outcomeText(m.outcome)}</td>
                            <td className="num">{count(m.n)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </>
            )}
          </section>

          <section className="panel" aria-labelledby="trend-heading">
            <h2 id="trend-heading">Trigger accuracy over time</h2>
            <div className="dash-filters">
              <div className="field">
                <label htmlFor="trend-skill">Skill</label>
                <select id="trend-skill" value={skillPick} onChange={(e) => { setTrendSkill(e.target.value); setTrendVersion(ALL); }}>
                  <option value={ALL}>All skills</option>
                  {trendSkills.map((x) => <option key={x} value={x}>{x}</option>)}
                </select>
              </div>
              <div className="field">
                <label htmlFor="trend-version">Version</label>
                <select id="trend-version" value={versionPick} onChange={(e) => setTrendVersion(e.target.value)}>
                  <option value={ALL}>All versions</option>
                  {trendVersions.map((v) => <option key={v} value={String(v)}>{versionLabel(v)}</option>)}
                </select>
              </div>
            </div>
            {trigger.days.length === 0 ? <p className="muted">No trigger runs in the last 30 days for this selection.</p> : (
              <>
                <Legend items={trigger.lines.map((l) => ({ name: l.name, color: l.color, shape: "line" as const }))} />
                <LineChart label="Trigger accuracy per day, by model" days={trigger.days} series={trigger.lines}
                  yMax={1} yTicks={[0, 0.25, 0.5, 0.75, 1]} format={pct} />
                <TableAlt label="Trigger accuracy rows">
                  {trendTable(triggerKept, (r) => r.accuracy, "Accuracy")}
                </TableAlt>
              </>
            )}
            <h3 className="dash-subhead">With-skill pass rate over time</h3>
            {pass.days.length === 0 ? <p className="muted">No with-skill runs in the last 30 days for this selection.</p> : (
              <>
                <Legend items={pass.lines.map((l) => ({ name: l.name, color: l.color, shape: "line" as const }))} />
                <LineChart label="With-skill pass rate per day, by model" days={pass.days} series={pass.lines}
                  yMax={1} yTicks={[0, 0.25, 0.5, 0.75, 1]} format={pct} />
                <TableAlt label="With-skill pass rate rows">
                  {trendTable(passKept, (r) => r.pass_rate, "Pass rate")}
                </TableAlt>
              </>
            )}
          </section>

          <section className="panel" aria-labelledby="health-heading">
            <h2 id="health-heading">Health by model</h2>
            <p className="muted">Each skill's live version: the share of runs with an error, and the share of results that came out inconclusive.</p>
            {healthRows.length === 0 ? <p className="muted">No results for this selection.</p> : (
              <>
                <Legend items={[
                  { name: "Error rate", color: "var(--fail)", shape: "square" },
                  { name: "Inconclusive rate", color: "var(--info)", shape: "square" },
                ]} />
                <GroupedBars label="Error and inconclusive rate by model" domain={[0, healthMax]} format={pct}
                  groups={healthRows.map((r) => ({
                    label: r.model,
                    bars: [
                      { name: "Error rate", color: "var(--fail)", value: r.error },
                      { name: "Inconclusive rate", color: "var(--info)", value: r.inconclusive },
                    ],
                  }))} />
                <TableAlt label="Health rows">
                  <table className="matrix">
                    <caption>Error and inconclusive rates by model</caption>
                    <thead><tr>
                      <th scope="col">Model</th><th scope="col" className="num">Rows</th>
                      <th scope="col" className="num">Error rate</th><th scope="col" className="num">Inconclusive rate</th>
                    </tr></thead>
                    <tbody>
                      {healthRows.map((r) => (
                        <tr key={r.model}>
                          <td>{r.model}</td><td className="num">{count(r.cells)}</td>
                          <td className="num">{pct(r.error)}</td><td className="num">{pct(r.inconclusive)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </TableAlt>
              </>
            )}
          </section>

          <section className="panel" aria-labelledby="tokens-heading">
            <h2 id="tokens-heading">Token usage</h2>
            <p className="muted">Input and output tokens of the configured model endpoints, last 30 days. This covers every department.</p>
            {d.tokens === null ? (
              <p role="note" className="notice">
                Token usage needs access to the system.serving tables. Ask an administrator for SELECT on
                system.serving.endpoint_usage and system.serving.served_entities.
              </p>
            ) : tokens.length === 0 ? (
              <p className="muted">No token usage in the last 30 days for this selection.</p>
            ) : (
              <>
                <Legend items={tokenLines.map((l) => ({ name: l.name, color: l.color, shape: "line" as const }))} />
                <LineChart label="Input tokens per day, by model" days={tokenDays} series={tokenLines}
                  yMax={tokenTop} yTicks={[0, tokenTop / 4, tokenTop / 2, (3 * tokenTop) / 4, tokenTop].map(Math.round)}
                  format={(v) => new Intl.NumberFormat("en", { notation: "compact" }).format(v)} />
                <TableAlt label="Token usage rows">
                  <table className="matrix">
                    <caption>Token usage per day and endpoint</caption>
                    <thead><tr>
                      <th scope="col">Day</th><th scope="col">Model</th><th scope="col">Endpoint</th>
                      <th scope="col" className="num">Input tokens</th><th scope="col" className="num">Output tokens</th>
                    </tr></thead>
                    <tbody>
                      {[...tokens].sort((a, b) => a.day.localeCompare(b.day)).map((r, i) => (
                        <tr key={`${r.day}-${r.endpoint_name}-${i}`}>
                          <td className="nowrap">{dayLabel(r.day)}</td><td>{r.model_profile}</td><td>{r.endpoint_name}</td>
                          <td className="num">{count(r.input_tokens ?? 0)}</td><td className="num">{count(r.output_tokens ?? 0)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </TableAlt>
              </>
            )}
          </section>
        </>
      )}
    </>
  );
}
