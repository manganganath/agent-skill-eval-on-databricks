import type { Me } from "./api/types";

// Authors and reviewers see candidates (skills under review in a pull request). The API applies the same rule.
export const canSeeCandidates = (me: Me | undefined): boolean => Boolean(me && (me.is_author || me.is_reviewer));

export function candidatePath(c: { run_key: string; dept: string; name: string }): string {
  return `/candidates/${encodeURIComponent(c.run_key)}/${encodeURIComponent(c.dept)}/${encodeURIComponent(c.name)}`;
}

// The run page, with this candidate picked.
export function runCandidatePath(runKey: string, dept: string, name: string): string {
  return `/run?${new URLSearchParams({ candidate: runKey, skill: `${dept}/${name}` })}`;
}

// pr<N>_<sha7>[_a<attempt>]: the folder name CI gives a pull request's upload.
export function prNumberOf(runKey: string): number | null {
  const m = /^pr([1-9][0-9]{0,8})_[0-9a-f]{7}(?:_a[0-9]{1,4})?$/.exec(runKey);
  return m ? Number(m[1]) : null;
}

// draft_<8 hex time><16 hex random>: the folder name of a Skill Hub draft.
export const isDraftKey = (runKey: string): boolean => /^draft_[0-9a-f]{24}$/.test(runKey);
