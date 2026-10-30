import type { CandidateKind } from "../api/types";

// Marks content that is not released: a skill under review in a pull request, or a Skill Hub draft. A draft that was
// submitted for review also says which pull request holds it. The words carry the meaning; colour only reinforces it.
export function CandidateBadge({ kind = "pr", pr, submittedPr = null }: {
  kind?: CandidateKind; pr: number | null; submittedPr?: number | null;
}) {
  if (kind === "draft") {
    return (
      <>
        <span className="badge badge-review badge-draft">Draft</span>
        {submittedPr !== null && <span className="badge badge-review">{`Submitted · PR #${submittedPr}`}</span>}
      </>
    );
  }
  return <span className="badge badge-review">{`Candidate · PR #${pr}`}</span>;
}
