import type { Verdict } from "../api/types";

const LABEL: Record<Verdict, { text: string; icon: string }> = {
  pass: { text: "Pass", icon: "✓" },
  warn: { text: "Warn", icon: "!" },
  fail: { text: "Fail", icon: "✕" },
  inconclusive: { text: "Inconclusive", icon: "?" },
  advisory: { text: "Advisory", icon: "i" },
};

// The word for a verdict, as the chip shows it. Charts use it for their legends.
export function verdictText(verdict: Verdict): string {
  return LABEL[verdict].text;
}

// Colour is never the only signal: the text and icon carry the verdict.
export function VerdictChip({ verdict }: { verdict: Verdict | undefined }) {
  if (!verdict) return <span className="chip chip-none">No result</span>;
  const { text, icon } = LABEL[verdict];
  return (
    <span className={`chip chip-${verdict}`}>
      <span className="chip-icon" aria-hidden="true">{icon}</span>
      <span>{text}</span>
    </span>
  );
}
