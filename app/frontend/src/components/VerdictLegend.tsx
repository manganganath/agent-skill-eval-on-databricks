import type { Verdict, VerdictRules } from "../api/types";
import { VerdictChip } from "./VerdictChip";

const pct = (rate: number) => `${Math.round(rate * 100)}%`;
const ORDER: Verdict[] = ["pass", "warn", "fail", "inconclusive", "advisory"];

// The defaults that ship with the product, for a config that does not send its rules.
const FALLBACK: VerdictRules = {
  fail_rate: 0.5, warn_rate: 0.8, max_error_rate: 0.1, min_reviewed_cases: 5, pass_score: 0.7, gating_modes: ["trigger"],
};

function rule(v: Verdict, r: VerdictRules, gating: string): string {
  switch (v) {
    case "pass":
      return `At least ${pct(r.warn_rate)} of the reviewed cases passed.`;
    case "warn":
      return `${pct(r.fail_rate)} up to ${pct(r.warn_rate)} passed. It does not block a release.`;
    case "fail":
      return `Fewer than ${pct(r.fail_rate)} passed. In a mode that gates a release (${gating}), it blocks the release.`;
    case "inconclusive":
      return `More than ${pct(r.max_error_rate)} of the runs errored, or most judges gave no answer. Run the eval again. In a gating mode it blocks the release until a run succeeds.`;
    case "advisory":
      return `Fewer than ${r.min_reviewed_cases} test cases in this mode are reviewed by a person. The scores are shown, and the result never blocks a release. Review the cases in a pull request to get a real verdict.`;
  }
}

// What each verdict means, with the thresholds this deployment uses.
export function VerdictLegend({ rules }: { rules?: VerdictRules }) {
  const r = rules ?? FALLBACK;
  const gating = r.gating_modes.length > 0 ? r.gating_modes.join(" and ") : "none";
  return (
    <section aria-labelledby="verdict-legend" className="panel verdict-legend">
      <h2 id="verdict-legend">What the verdicts mean</h2>
      <p className="muted">
        A verdict is worked out per row, from the reviewed test cases of one skill version, model and mode. A trigger
        case passes when the agent picks the expected skill, or no skill when none is expected. An effectiveness case
        passes when its weighted rubric score reaches {pct(r.pass_score)} and its assertions hold. A trigger case with
        assertions also runs as one; without a rubric, only its assertions count. Modes that gate a release: {gating}.
      </p>
      <dl className="legend-list">
        {ORDER.map((v) => (
          <div key={v} className="legend-row">
            <dt><VerdictChip verdict={v} /></dt>
            <dd>{rule(v, r, gating)}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}
