// Dashboard charts, drawn with CSS bars and inline SVG (no chart library). Each chart is labelled and has a table
// alternative on its page, so the marks carry no information that is not also in text. Colours come from the
// .dashboard tokens in styles.css; a series takes its slot in a fixed order, never a generated hue.

export interface LegendItem { name: string; color: string; shape: "square" | "line" }

export function Legend({ items }: { items: LegendItem[] }) {
  return (
    <ul className="chart-legend">
      {items.map((item) => (
        <li key={item.name}>
          <span className={`legend-mark legend-${item.shape}`} style={{ background: item.color }} aria-hidden="true" />
          {item.name}
        </li>
      ))}
    </ul>
  );
}

export interface Segment { name: string; value: number; color: string }

// Stacked horizontal bars: one row per category, its segments in order, the row total at the end.
export function StackedBars({ label, rows, format }: {
  label: string; rows: { label: string; segments: Segment[] }[]; format: (v: number) => string;
}) {
  const max = Math.max(1, ...rows.map((r) => r.segments.reduce((s, seg) => s + seg.value, 0)));
  return (
    <div className="hbars" role="img" aria-label={label}>
      {rows.map((row) => {
        const total = row.segments.reduce((s, seg) => s + seg.value, 0);
        return (
          <div className="hbar-row" key={row.label}>
            <span className="hbar-label">{row.label}</span>
            <span className="hbar-track">
              {row.segments.filter((seg) => seg.value > 0).map((seg) => (
                <span
                  key={seg.name}
                  className="hbar-seg"
                  title={`${row.label}, ${seg.name}: ${format(seg.value)}`}
                  style={{ width: `${(seg.value / max) * 100}%`, background: seg.color }}
                />
              ))}
            </span>
            <span className="hbar-value">{format(total)}</span>
          </div>
        );
      })}
    </div>
  );
}

export interface BarValue { name: string; color: string; value: number | null }

// Grouped horizontal bars on one scale. Bars grow from zero, so a negative value points left.
export function GroupedBars({ label, groups, domain, format }: {
  label: string; groups: { label: string; bars: BarValue[] }[]; domain: [number, number]; format: (v: number) => string;
}) {
  const [lo, hi] = domain;
  const span = hi - lo || 1;
  const at = (v: number) => ((v - lo) / span) * 100;
  return (
    <div className="hbars" role="img" aria-label={label}>
      {groups.map((group) => (
        <div className="hbar-group" key={group.label}>
          <span className="hbar-label">{group.label}</span>
          <div className="hbar-bars">
            {group.bars.map((bar) => (
              <div className="hbar-line" key={bar.name}>
                <span className="hbar-track">
                  <span className="hbar-zero" style={{ left: `${at(0)}%` }} />
                  {bar.value != null && (
                    <span
                      className="hbar-bar"
                      title={`${group.label}, ${bar.name}: ${format(bar.value)}`}
                      style={{
                        left: `${at(Math.min(0, bar.value))}%`,
                        width: `${at(Math.max(0, bar.value)) - at(Math.min(0, bar.value))}%`,
                        background: bar.color,
                      }}
                    />
                  )}
                </span>
                <span className="hbar-value">{bar.value == null ? "-" : format(bar.value)}</span>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

export interface LineSeries { name: string; color: string; values: (number | null)[] }

const W = 640;
const H = 240;
const PL = 52; // left room for the y ticks
const PR = 16;
const PT = 12;
const PB = 28; // room for the x labels

// One line per series over shared days. Each point has a hover title; the page's table gives the same values.
export function LineChart({ label, days, series, yMax, yTicks, format }: {
  label: string; days: string[]; series: LineSeries[]; yMax: number; yTicks: number[]; format: (v: number) => string;
}) {
  const innerW = W - PL - PR;
  const innerH = H - PT - PB;
  const x = (i: number) => PL + (days.length <= 1 ? innerW / 2 : (i / (days.length - 1)) * innerW);
  const y = (v: number) => PT + innerH - (Math.min(Math.max(v, 0), yMax) / yMax) * innerH;
  const labelled = days.length <= 6 ? days.map((_, i) => i) : [0, 1, 2, 3, 4, 5].map((k) => Math.round((k * (days.length - 1)) / 5));
  return (
    <svg className="chart-svg" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={label}>
      {yTicks.map((t) => (
        <g key={t}>
          <line x1={PL} x2={W - PR} y1={y(t)} y2={y(t)} className="chart-grid" />
          <text x={PL - 8} y={y(t)} textAnchor="end" dominantBaseline="middle" className="chart-tick">{format(t)}</text>
        </g>
      ))}
      {labelled.map((i) => (
        <text key={i} x={x(i)} y={H - 8} textAnchor="middle" className="chart-tick">{dayLabel(days[i])}</text>
      ))}
      {series.map((s) => {
        const points = s.values.flatMap((v, i) => (v == null ? [] : [{ i, v, px: x(i), py: y(v) }]));
        return (
          <g key={s.name}>
            {points.length > 1 && (
              <polyline
                points={points.map((p) => `${p.px},${p.py}`).join(" ")}
                fill="none" stroke={s.color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round"
              />
            )}
            {points.map((p) => (
              <g key={p.i}>
                <title>{`${s.name}, ${dayLabel(days[p.i])}: ${format(p.v)}`}</title>
                <circle cx={p.px} cy={p.py} r={12} fill="transparent" />
                <circle cx={p.px} cy={p.py} r={4} fill={s.color} stroke="var(--canvas)" strokeWidth={2} />
              </g>
            ))}
          </g>
        );
      })}
    </svg>
  );
}

// "2026-10-01" to "Oct 1". Days are calendar dates, so they are read in UTC.
export function dayLabel(day: string): string {
  return new Date(`${day}T00:00:00Z`).toLocaleDateString("en", { month: "short", day: "numeric", timeZone: "UTC" });
}
