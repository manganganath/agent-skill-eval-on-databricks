import type { Verdict } from "../api/types";

const LABEL: Record<Verdict, { text: string; icon: string }> = {
  pass: { text: "Pass", icon: "✓" },
  warn: { text: "Warn", icon: "!" },
  fail: { text: "Fail", icon: "✕" },
  inconclusive: { text: "Inconclusive", icon: "?" },
  advisory: { text: "Advisory", icon: "i" },
};

// Colour is never the only signal: the text and icon carry the verdict.
export function VerdictChip({ verdict }: { verdict: Verdict | undefined }) {
  if (!verdict) return <span className="chip chip-none">No result</span>;
  const { text, icon } = LABEL[verdict];
  return (
    <span className={`chip chip-${verdict}`}>
      <span aria-hidden="true">{icon} </span>
      <span>{text}</span>
    </span>
  );
}
