import { render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import CandidateDetail from "./CandidateDetail";
import { api, ApiError } from "../api/client";
import type { CandidateDetail as CandidateDetailT } from "../api/types";

vi.mock("../api/client", async (orig) => {
  const real = await orig<typeof import("../api/client")>();
  return { ...real, api: { ...real.api, candidate: vi.fn(), me: vi.fn() } };
});

const KEY = "pr5_c7ecd3e_a1";

const detail: CandidateDetailT = {
  dept: "common", name: "new-skill", description: "Drafts the quarterly note", content_hash: "sha256:abc",
  verdicts: { "claude-sonnet": "warn" },
  files: { "SKILL.md": "# New skill steps\n\nDraft the note.", "references/guide.md": "guide text" },
  evals: {
    skill_name: "new-skill",
    evals: [
      { id: 1, kind: "trigger", prompt: "Draft the quarterly note", should_trigger: true, expected_skill: null,
        assertions: [], fixtures: [], origin: "human", reviewed: true },
    ],
  },
  rubric: {
    skill_name: "new-skill", source_hash: "sha256:abc", generated_by: "claude-sonnet", pass_score: 0.7, reviewed: false,
    criteria: [1, 2, 3, 4, 5].map((i) => ({
      id: `c${i}`, text: `Criterion ${i}`, type: "task_quality" as const, weight: 1, source: "SKILL.md",
    })),
  },
  eval_access: "ok",
  results: [
    { run_key: KEY, trigger_source: "pr", skill: "new-skill", model_profile: "claude-sonnet", mode: "trigger",
      verdict: "warn", run_id: "r1", experiment_id: "17", n_cases: 4, pass_rate: 0.75, error_rate: 0, accuracy: 1,
      content_hash: "sha256:abc", started_at: "2026-10-28T10:00:00Z" },
  ],
  candidate: { pr_number: 5, run_key: KEY, git_sha: "c7ecd3e0123456789", uploaded_at: "2026-10-28T09:00:00Z" },
};

beforeEach(() => {
  vi.mocked(api.candidate).mockReset();
  vi.mocked(api.me).mockResolvedValue({ user: "ana@example.com", groups: [], is_reviewer: false, is_author: true });
});

function renderAt(path = `/candidates/${KEY}/common/new-skill`) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes><Route path="/candidates/:runKey/:dept/:skill" element={<CandidateDetail />} /></Routes>
    </MemoryRouter>,
  );
}

describe("CandidateDetail", () => {
  it("says the skill is not released and is under review in its pull request", async () => {
    vi.mocked(api.candidate).mockResolvedValue(detail);
    renderAt();
    const banner = await screen.findByRole("note", { name: /release status/i });
    expect(banner).toHaveTextContent("Not released. Under review in PR #5.");
    expect(banner).toHaveTextContent("c7ecd3e");
    expect(api.candidate).toHaveBeenCalledWith(KEY, "common", "new-skill");
    expect(screen.getByRole("heading", { level: 1, name: "new-skill" })).toBeTruthy();
    expect(screen.getByText("Candidate · PR #5")).toBeTruthy();
  });

  it("shows the skill, its test cases, rubric and eval results", async () => {
    vi.mocked(api.candidate).mockResolvedValue(detail);
    renderAt();
    expect(await screen.findByRole("heading", { name: "New skill steps" })).toBeTruthy();
    expect(within(screen.getByRole("list", { name: /files/i })).getByText("references/guide.md")).toBeTruthy();
    const cases = screen.getByRole("table", { name: /test cases/i });
    expect(within(cases).getByText("Draft the quarterly note")).toBeTruthy();
    const rubric = screen.getByRole("table", { name: /rubric criteria/i });
    expect(within(rubric).getAllByRole("row")).toHaveLength(6); // a header row and five criteria
    const results = screen.getByRole("table", { name: /eval results/i });
    expect(within(results).getByText(KEY)).toBeTruthy();
    expect(within(results).getByText("Warn")).toBeTruthy();
    expect(within(results).getByText("75%")).toBeTruthy();
  });

  it("links Run eval to the run page with this candidate picked", async () => {
    vi.mocked(api.candidate).mockResolvedValue(detail);
    renderAt();
    const link = await screen.findByRole("link", { name: /run eval/i });
    expect(link).toHaveAttribute("href", `/run?candidate=${KEY}&skill=common%2Fnew-skill`);
  });

  it("explains a candidate with no results yet", async () => {
    vi.mocked(api.candidate).mockResolvedValue({ ...detail, results: [], verdicts: {} });
    renderAt();
    expect(await screen.findByRole("heading", { name: /no eval results yet/i })).toBeTruthy();
  });

  it("answers a missing or hidden candidate with one message", async () => {
    vi.mocked(api.candidate).mockRejectedValue(new ApiError(404, "candidate_not_found"));
    renderAt();
    expect(await screen.findByRole("alert")).toHaveTextContent(/not found, or you do not have access/i);
  });
});
