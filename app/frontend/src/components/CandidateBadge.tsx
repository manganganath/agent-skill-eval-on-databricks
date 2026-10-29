// Marks content that is not released: a skill under review in a pull request. The words carry the meaning.
export function CandidateBadge({ pr }: { pr: number }) {
  return <span className="badge badge-review">{`Candidate · PR #${pr}`}</span>;
}
