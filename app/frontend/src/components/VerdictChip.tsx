import type { ReactNode } from "react";
import type { Verdict } from "../api/types";

// 16-unit icons drawn with the chip's text colour. Decorative: the word carries the verdict.
const ICON: Record<Verdict, ReactNode> = {
  pass: <path d="M3.5 8.5l3 3 6-7" />,
  warn: (
    <>
      <path d="M8 2.5l6.2 11H1.8z" />
      <path d="M8 6.5v3.2" />
      <circle cx="8" cy="11.6" r="0.4" />
    </>
  ),
  fail: <path d="M4.5 4.5l7 7M11.5 4.5l-7 7" />,
  inconclusive: (
    <>
      <circle cx="8" cy="8" r="6.2" />
      <path d="M6.2 6.3a1.9 1.9 0 1 1 2.7 1.7c-.6.3-.9.7-.9 1.3" />
      <circle cx="8" cy="11.4" r="0.4" />
    </>
  ),
  advisory: (
    <>
      <circle cx="8" cy="8" r="6.2" />
      <path d="M8 7.4v3.8" />
      <circle cx="8" cy="5" r="0.4" />
    </>
  ),
};

const LABEL: Record<Verdict, { text: string; meaning: string }> = {
  pass: { text: "Pass", meaning: "The reviewed cases met the pass bar." },
  warn: { text: "Warn", meaning: "Below the pass bar, above the fail bar. It does not block a release." },
  fail: { text: "Fail", meaning: "Below the fail bar. A fail in a gating mode blocks the release." },
  inconclusive: { text: "Inconclusive", meaning: "Too many runs errored to judge. Run the eval again." },
  advisory: {
    text: "Advisory",
    meaning: "Too few reviewed test cases to judge. The scores are shown, and this result never blocks a release.",
  },
};

// The word for a verdict, as the chip shows it. Charts use it for their legends.
export function verdictText(verdict: Verdict): string {
  return LABEL[verdict].text;
}

// What a verdict means, for the chip's hover text and for legends that explain the verdicts.
export function verdictMeaning(verdict: Verdict): string {
  return LABEL[verdict].meaning;
}

// Colour is never the only signal: the text and icon carry the verdict.
export function VerdictChip({ verdict }: { verdict: Verdict | undefined }) {
  if (!verdict) return <span className="chip chip-none">No result</span>;
  const { text, meaning } = LABEL[verdict];
  return (
    <span className={`chip chip-${verdict}`} title={`${text}: ${meaning}`}>
      <svg className="chip-icon" viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" focusable="false">
        {ICON[verdict]}
      </svg>
      <span>{text}</span>
    </span>
  );
}
