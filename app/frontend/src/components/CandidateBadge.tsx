import type { CandidateKind } from "../api/types";

// Marks content that is not released: a skill under review in a pull request, or a Skill Hub draft. The words carry
// the meaning; colour only reinforces it.
export function CandidateBadge({ kind = "pr", pr }: { kind?: CandidateKind; pr: number | null }) {
  if (kind === "draft") return <span className="badge badge-review badge-draft">Draft</span>;
  return <span className="badge badge-review">{`Candidate · PR #${pr}`}</span>;
}
