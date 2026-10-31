import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import CandidateDetail from "./CandidateDetail";
import { api, ApiError } from "../api/client";
import type { CandidateDetail as CandidateDetailT, ReleaseStatus } from "../api/types";

vi.mock("../api/client", async (orig) => {
  const real = await orig<typeof import("../api/client")>();
  return {
    ...real,
    api: {
      ...real.api, candidate: vi.fn(), me: vi.fn(), config: vi.fn(), tryCandidate: vi.fn(), discardDraft: vi.fn(),
      submitDraft: vi.fn(), releaseStatus: vi.fn(), mergePull: vi.fn(),
    },
  };
});

const KEY = "pr5_c7ecd3e_a1";
const OPEN_PR: ReleaseStatus = {
  pr_number: 5, dept: "common", skill: "new-skill",
  opened: { state: "open", url: "https://github.test/acme/skills/pull/5" },
  gate: { state: "passed", url: "https://github.test/acme/skills/pull/5/checks" },
  merged: { merged: false, sha: null, at: null },
  release: { state: "not_started", url: null },
  live: { fqn: "skill_hub.common.new-skill", version: null, switched_after_merge: false, eval_access: "ok" },
};

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
  candidate: {
    kind: "pr", pr_number: 5, run_key: KEY, git_sha: "c7ecd3e0123456789", uploaded_at: "2026-10-28T09:00:00Z",
    uploaded_by: null,
  },
};

beforeEach(() => {
  vi.mocked(api.candidate).mockReset();
  vi.mocked(api.config).mockResolvedValue({
    models: { "claude-sonnet": "frontier" }, departments: ["common"], workspace_url: "https://ws.example.test",
  });
  vi.mocked(api.tryCandidate).mockReset();
  vi.mocked(api.tryCandidate).mockResolvedValue([
    { model: "claude-sonnet", with_skill: { text: "draft", invoked: true }, without_skill: { text: "plain" },
      trace_url: null },
  ]);
  vi.mocked(api.me).mockResolvedValue({ user: "ana@example.com", groups: [], is_reviewer: false, is_author: true });
  vi.mocked(api.releaseStatus).mockReset();
  vi.mocked(api.releaseStatus).mockResolvedValue(OPEN_PR);
  vi.mocked(api.submitDraft).mockReset();
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

describe("CandidateDetail for a draft", () => {
  const DRAFT = "draft_6720a1b2c3d4e5f6a7b8c9d0";
  const draft: CandidateDetailT = {
    ...detail,
    rubric: null,
    candidate: { kind: "draft", pr_number: null, run_key: DRAFT, git_sha: null, uploaded_at: "2026-10-29T08:00:00Z",
      uploaded_by: "ana@example.com" },
  };

  it("says a draft is never released, who uploaded it, and that it runs trigger mode only without a rubric", async () => {
    vi.mocked(api.candidate).mockResolvedValue(draft);
    renderAt(`/candidates/${DRAFT}/common/new-skill`);
    const banner = await screen.findByRole("note", { name: /release status/i });
    expect(banner).toHaveTextContent("Not released. Draft uploaded by ana@example.com.");
    expect(banner).toHaveTextContent(/never released/i);
    expect(screen.getByText("Draft")).toBeTruthy();
    expect(screen.getByText(/no rubric, so it can run in trigger mode only/i)).toBeTruthy();
    expect(screen.getByRole("link", { name: /run eval/i })).toHaveAttribute(
      "href", `/run?candidate=${DRAFT}&skill=common%2Fnew-skill`,
    );
  });

  it("says a draft without a rubric is judged on its assertions when its cases allow it", async () => {
    vi.mocked(api.candidate).mockResolvedValue({ ...draft, rubric_optional: true });
    renderAt(`/candidates/${DRAFT}/common/new-skill`);
    expect(await screen.findByText(/no rubric, so effectiveness mode judges its cases on their assertions only/i))
      .toBeTruthy();
    expect(screen.queryByText(/trigger mode only/i)).toBeNull();
  });
});

describe("CandidateDetail try panel", () => {
  it("offers Try after the files and runs the candidate at its own path", async () => {
    vi.mocked(api.candidate).mockResolvedValue(detail);
    renderAt();
    const heading = await screen.findByRole("heading", { name: "Try this skill" });
    expect(heading.compareDocumentPosition(screen.getByRole("list", { name: /files/i }))
      & Node.DOCUMENT_POSITION_PRECEDING).toBeTruthy();
    fireEvent.change(screen.getByLabelText(/test input/i), { target: { value: "draft the note" } });
    fireEvent.click(screen.getByRole("checkbox", { name: "claude-sonnet" }));
    fireEvent.click(screen.getByRole("button", { name: /try skill/i }));
    await waitFor(() => expect(api.tryCandidate).toHaveBeenCalledWith(KEY, "common", "new-skill", {
      prompt: "draft the note", models: ["claude-sonnet"],
    }));
  });

  it("offers Try on a draft too", async () => {
    vi.mocked(api.candidate).mockResolvedValue({
      ...detail, rubric: null,
      candidate: { ...detail.candidate, kind: "draft", pr_number: null, run_key: "draft_6720a1b2c3d4e5f6a7b8c9d0" },
    });
    renderAt("/candidates/draft_6720a1b2c3d4e5f6a7b8c9d0/common/new-skill");
    expect(await screen.findByRole("heading", { name: "Try this skill" })).toBeTruthy();
  });
});


describe("CandidateDetail discard", () => {
  const DRAFT = "draft_6720a1b2c3d4e5f6a7b8c9d0";
  const draft: CandidateDetailT = {
    ...detail,
    candidate: { kind: "draft", pr_number: null, run_key: DRAFT, git_sha: null, uploaded_at: "2026-10-29T08:00:00Z",
      uploaded_by: "ana@example.com" },
  };

  function renderWithCatalog() {
    return render(
      <MemoryRouter initialEntries={[`/candidates/${DRAFT}/common/new-skill`]}>
        <Routes>
          <Route path="/candidates/:runKey/:dept/:skill" element={<CandidateDetail />} />
          <Route path="/" element={<p>catalog page</p>} />
        </Routes>
      </MemoryRouter>,
    );
  }

  it("lets the uploader discard after a confirmation, then goes to the catalog", async () => {
    vi.mocked(api.candidate).mockResolvedValue(draft);
    vi.mocked(api.discardDraft).mockResolvedValue({ discarded: DRAFT });
    renderWithCatalog();
    fireEvent.click(await screen.findByRole("button", { name: "Discard draft" }));
    expect(api.discardDraft).not.toHaveBeenCalled(); // the first click only asks
    const confirm = screen.getByRole("group", { name: /discard this draft/i });
    expect(confirm).toHaveTextContent("Its eval results stay in the Eval matrix.");
    fireEvent.click(within(confirm).getByRole("button", { name: "Discard" }));
    expect(await screen.findByText("catalog page")).toBeTruthy();
    expect(api.discardDraft).toHaveBeenCalledWith(DRAFT);
  });

  it("offers no discard to another author, nor on a pull request candidate", async () => {
    vi.mocked(api.me).mockResolvedValue({ user: "bo@example.com", groups: [], is_reviewer: false, is_author: true });
    vi.mocked(api.candidate).mockResolvedValue(draft);
    const { unmount } = renderWithCatalog();
    await screen.findByRole("note", { name: /release status/i });
    expect(screen.queryByRole("button", { name: "Discard draft" })).toBeNull();
    unmount();
    vi.mocked(api.me).mockResolvedValue({ user: "rita@example.com", groups: [], is_reviewer: true, is_author: false });
    vi.mocked(api.candidate).mockResolvedValue(detail);
    renderAt();
    await screen.findByRole("note", { name: /release status/i });
    expect(screen.queryByRole("button", { name: "Discard draft" })).toBeNull();
  });

  it("shows a pull request candidate's release path for its skill", async () => {
    vi.mocked(api.candidate).mockResolvedValue(detail);
    renderAt();
    expect(await screen.findByRole("heading", { name: "Release path" })).toBeTruthy();
    expect(api.releaseStatus).toHaveBeenCalledWith(5, "common", "new-skill");
    expect(screen.getByText("Passed")).toBeTruthy();
  });

  it("opens a pull request for a draft after a confirmation, then shows its pull request and release path", async () => {
    vi.mocked(api.candidate).mockResolvedValue(draft);
    vi.mocked(api.submitDraft).mockResolvedValue({
      pr_number: 7, url: "https://github.test/acme/skills/pull/7", branch: "skillhub/new-skill-00000000", notes: [],
    });
    vi.mocked(api.releaseStatus).mockResolvedValue({ ...OPEN_PR, pr_number: 7 });
    renderAt(`/candidates/${DRAFT}/common/new-skill`);
    fireEvent.click(await screen.findByRole("button", { name: "Submit for review" }));
    expect(api.submitDraft).not.toHaveBeenCalled(); // the first click only asks
    const confirm = screen.getByRole("group", { name: /submit this draft for review/i });
    expect(confirm).toHaveTextContent(
      "Open a pull request with this draft? Its eval gate runs automatically, a reviewer merges it, and the release is approved in GitHub.",
    );
    fireEvent.click(within(confirm).getByRole("button", { name: "Open pull request" }));
    const link = await screen.findByRole("link", { name: "Submitted as PR #7" });
    expect(link).toHaveAttribute("href", "https://github.test/acme/skills/pull/7");
    expect(api.submitDraft).toHaveBeenCalledWith(DRAFT);
    expect(screen.queryByRole("button", { name: "Submit for review" })).toBeNull();
    expect(await screen.findByRole("heading", { name: "Release path" })).toBeTruthy();
    expect(api.releaseStatus).toHaveBeenCalledWith(7, "common", "new-skill");
  });

  it("shows a draft's recorded pull request and no submit button", async () => {
    vi.mocked(api.candidate).mockResolvedValue({
      ...draft, candidate: { ...draft.candidate, submitted_pr: 7, submitted_url: "https://github.test/acme/skills/pull/7" },
    });
    renderAt(`/candidates/${DRAFT}/common/new-skill`);
    expect(await screen.findByRole("link", { name: "Submitted as PR #7" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Submit for review" })).toBeNull();
    expect(api.releaseStatus).toHaveBeenCalledWith(7, "common", "new-skill");
  });

  it("offers no submit to another author", async () => {
    vi.mocked(api.me).mockResolvedValue({ user: "bo@example.com", groups: [], is_reviewer: false, is_author: true });
    vi.mocked(api.candidate).mockResolvedValue(draft);
    renderAt(`/candidates/${DRAFT}/common/new-skill`);
    await screen.findByRole("note", { name: /release status/i });
    expect(screen.queryByRole("button", { name: "Submit for review" })).toBeNull();
  });

  it("reloads the draft when it was already submitted, and explains a draft without test cases", async () => {
    vi.mocked(api.candidate).mockResolvedValue(draft);
    vi.mocked(api.submitDraft).mockRejectedValueOnce(new ApiError(409, "already_submitted", []));
    renderAt(`/candidates/${DRAFT}/common/new-skill`);
    fireEvent.click(await screen.findByRole("button", { name: "Submit for review" }));
    fireEvent.click(screen.getByRole("button", { name: "Open pull request" }));
    await waitFor(() => expect(api.candidate).toHaveBeenCalledTimes(2));
  });

  it("explains a draft that has no test cases", async () => {
    vi.mocked(api.candidate).mockResolvedValue(draft);
    vi.mocked(api.submitDraft).mockRejectedValueOnce(new ApiError(422, "evals_missing", []));
    renderAt(`/candidates/${DRAFT}/common/new-skill`);
    fireEvent.click(await screen.findByRole("button", { name: "Submit for review" }));
    fireEvent.click(screen.getByRole("button", { name: "Open pull request" }));
    expect(await screen.findByText(/has no test cases/i)).toBeTruthy();
  });
});
