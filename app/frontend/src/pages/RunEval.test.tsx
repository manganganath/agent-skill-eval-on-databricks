import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi, beforeEach } from "vitest";
import RunEval from "./RunEval";
import { api, ApiError } from "../api/client";
import type { CandidateSummary, Me } from "../api/types";

vi.mock("../api/client", async (orig) => {
  const real = await orig<typeof import("../api/client")>();
  return {
    ...real,
    api: {
      ...real.api, skills: vi.fn(), config: vi.fn(), startEval: vi.fn(), evalRun: vi.fn(), versions: vi.fn(),
      me: vi.fn(), candidates: vi.fn(),
    },
  };
});

const FQN = "skill_hub.common.audit-xls";
const KEY = "pr5_c7ecd3e_a1";
const reader: Me = { user: "rae@example.com", groups: [], is_reviewer: false, is_author: false };
const author: Me = { user: "ana@example.com", groups: ["skillhub-authors"], is_reviewer: false, is_author: true };
const candidate: CandidateSummary = {
  kind: "pr", uploaded_by: null, has_rubric: null, run_key: KEY, pr_number: 5, git_sha: "c7ecd3e", uploaded_at: null, dept: "common", name: "new-skill",
  description: null, content_hash: null, verdicts: {}, eval_access: "ok",
};

function publishedRow(version: number) {
  return {
    dept: "common", skill: "audit-xls", action: "published", version, from_version: null, to_version: null,
    versioned_fqn: null, content_hash: null, git_sha: null, eval_run_id: null, actor: "ci", job_run_id: null,
    ts: `t${version}`,
  };
}

beforeEach(() => {
  vi.mocked(api.skills).mockResolvedValue([
    { fqn: FQN, dept: "common", name: "audit-xls", description: null, live_version: 3, verdicts: {}, eval_access: "ok" },
  ]);
  vi.mocked(api.config).mockResolvedValue({
    models: { "claude-sonnet": "frontier", "gemini-flash": "fast" },
    departments: ["common"], workspace_url: "https://ws.example.test",
  });
  vi.mocked(api.versions).mockResolvedValue([publishedRow(1), publishedRow(2)]);
  vi.mocked(api.startEval).mockResolvedValue({ run_key: "hub_0123abcd", job_run_id: 99 });
  vi.mocked(api.startEval).mockClear();
  vi.mocked(api.evalRun).mockClear();
  vi.mocked(api.evalRun).mockResolvedValue({ state: "RUNNING", result_state: null, run_key: "hub_0123abcd" });
  vi.mocked(api.me).mockResolvedValue(reader);
  vi.mocked(api.candidates).mockReset();
  vi.mocked(api.candidates).mockResolvedValue([candidate]);
});

describe("RunEval", () => {
  it("submits the chosen skill, models and modes", async () => {
    render(<MemoryRouter><RunEval /></MemoryRouter>);
    fireEvent.click(await screen.findByRole("checkbox", { name: /audit-xls/i }));
    fireEvent.click(screen.getByRole("checkbox", { name: /claude-sonnet/i }));
    fireEvent.click(screen.getByRole("checkbox", { name: /trigger/i }));
    fireEvent.click(screen.getByRole("button", { name: /start evaluation/i }));
    await screen.findByText(/run hub_0123abcd/i);
    expect(api.startEval).toHaveBeenCalledWith({
      skills: [FQN], models: ["claude-sonnet"], modes: ["trigger"],
    });
  });

  it("pins the version chosen in the picker", async () => {
    render(<MemoryRouter><RunEval /></MemoryRouter>);
    fireEvent.click(await screen.findByRole("checkbox", { name: /audit-xls/i }));
    fireEvent.change(await screen.findByRole("combobox", { name: /version of audit-xls/i }), { target: { value: "1" } });
    fireEvent.click(screen.getByRole("checkbox", { name: /claude-sonnet/i }));
    fireEvent.click(screen.getByRole("checkbox", { name: /trigger/i }));
    fireEvent.click(screen.getByRole("button", { name: /start evaluation/i }));
    await screen.findByText(/run hub_0123abcd/i);
    expect(api.startEval).toHaveBeenLastCalledWith({
      skills: [FQN], models: ["claude-sonnet"], modes: ["trigger"], versions: { [FQN]: 1 },
    });
  });

  it("stops polling on unmount", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const { unmount } = render(<MemoryRouter><RunEval /></MemoryRouter>);
      fireEvent.click(await screen.findByRole("checkbox", { name: /audit-xls/i }));
      fireEvent.click(screen.getByRole("checkbox", { name: /claude-sonnet/i }));
      fireEvent.click(screen.getByRole("checkbox", { name: /trigger/i }));
      fireEvent.click(screen.getByRole("button", { name: /start evaluation/i }));
      await screen.findByText(/run hub_0123abcd/i);
      const calls = vi.mocked(api.evalRun).mock.calls.length;
      unmount();
      await vi.advanceTimersByTimeAsync(20000);
      expect(vi.mocked(api.evalRun).mock.calls.length).toBe(calls);
    } finally {
      vi.useRealTimers();
    }
  });

  it("explains an empty skill list instead of showing an empty box", async () => {
    vi.mocked(api.skills).mockResolvedValue([]);
    render(<MemoryRouter><RunEval /></MemoryRouter>);
    const skills = await screen.findByRole("group", { name: /skills/i });
    expect(skills).toHaveTextContent(/no skills to evaluate/i);
    expect(screen.getByRole("button", { name: /start evaluation/i })).toBeDisabled();
  });

  it("blocks submit until at least one skill, model and mode are chosen", async () => {
    render(<MemoryRouter><RunEval /></MemoryRouter>);
    const button = await screen.findByRole("button", { name: /start evaluation/i });
    expect(button).toBeDisabled();
  });

  async function fillAndStart(clicks = 1) {
    fireEvent.click(await screen.findByRole("checkbox", { name: /common\/audit-xls/i }));
    await screen.findByRole("combobox", { name: /version of audit-xls/i });
    fireEvent.click(screen.getByRole("checkbox", { name: /claude-sonnet/i }));
    fireEvent.click(screen.getByRole("checkbox", { name: /trigger/i }));
    const start = screen.getByRole("button", { name: /start evaluation/i });
    await act(async () => { for (let i = 0; i < clicks; i++) fireEvent.click(start); });
    return start;
  }

  it("calls startEval once on a double click and keeps Start disabled while the run is active", async () => {
    render(<MemoryRouter><RunEval /></MemoryRouter>);
    const start = await fillAndStart(2);
    await screen.findByText(/run hub_0123abcd/i);
    expect(api.startEval).toHaveBeenCalledTimes(1);
    expect(start).toBeDisabled();
  });

  it("shows a startEval error and lets the user retry", async () => {
    vi.mocked(api.startEval).mockRejectedValueOnce(new ApiError(409, "job_conflict"));
    render(<MemoryRouter><RunEval /></MemoryRouter>);
    const start = await fillAndStart();
    expect(await screen.findByRole("alert")).toHaveTextContent(/already in progress/i);
    await waitFor(() => expect(start).toBeEnabled());
  });

  it("stops polling on a terminal state and re-enables Start", async () => {
    vi.mocked(api.evalRun).mockResolvedValue({ state: "TERMINATED", result_state: "SUCCESS", run_key: "hub_0123abcd" });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      render(<MemoryRouter><RunEval /></MemoryRouter>);
      const start = await fillAndStart();
      await screen.findByText(/SUCCESS/);
      const calls = vi.mocked(api.evalRun).mock.calls.length;
      await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
      expect(vi.mocked(api.evalRun).mock.calls.length).toBe(calls);
      expect(start).toBeEnabled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("stops after 40 polls and shows a notice", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      render(<MemoryRouter><RunEval /></MemoryRouter>);
      await fillAndStart();
      await screen.findByText(/run hub_0123abcd/i);
      await act(async () => { await vi.advanceTimersByTimeAsync(5000 * 45); });
      expect(vi.mocked(api.evalRun).mock.calls.length).toBe(40);
      expect(screen.getByRole("status")).toHaveTextContent(/status checks stopped/i);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("RunEval with candidates", () => {
  it("lists an author's candidates in their own group, badged with the pull request", async () => {
    vi.mocked(api.me).mockResolvedValue(author);
    render(<MemoryRouter><RunEval /></MemoryRouter>);
    const group = await screen.findByRole("group", { name: /candidates under review/i });
    expect(within(group).getByRole("checkbox", { name: /common\/new-skill/i })).toBeTruthy();
    expect(within(group).getByText("Candidate · PR #5")).toBeTruthy();
    expect(within(screen.getByRole("group", { name: /live skills/i })).getByRole("checkbox", { name: /audit-xls/i })).toBeTruthy();
  });

  it("does not ask a reader for candidates", async () => {
    render(<MemoryRouter><RunEval /></MemoryRouter>);
    await screen.findByRole("checkbox", { name: /audit-xls/i });
    expect(api.candidates).not.toHaveBeenCalled();
    expect(screen.queryByRole("group", { name: /candidates under review/i })).toBeNull();
  });

  it("runs a picked candidate on its own, without live skills or versions", async () => {
    vi.mocked(api.me).mockResolvedValue(author);
    render(<MemoryRouter><RunEval /></MemoryRouter>);
    fireEvent.click(await screen.findByRole("checkbox", { name: /common\/new-skill/i }));
    // One run evaluates one candidate upload: live skills wait for another run.
    expect(screen.getByRole("checkbox", { name: /common\/audit-xls/i })).toBeDisabled();
    expect(screen.queryByRole("group", { name: /versions/i })).toBeNull();
    fireEvent.click(screen.getByRole("checkbox", { name: /claude-sonnet/i }));
    fireEvent.click(screen.getByRole("checkbox", { name: /trigger/i }));
    fireEvent.click(screen.getByRole("button", { name: /start evaluation/i }));
    await screen.findByText(/run hub_0123abcd/i);
    expect(api.startEval).toHaveBeenCalledWith({
      candidate: { run_key: KEY, skills: ["common/new-skill"] }, models: ["claude-sonnet"], modes: ["trigger"],
    });
  });

  it("picks the candidate named in the link from its page", async () => {
    vi.mocked(api.me).mockResolvedValue(author);
    render(
      <MemoryRouter initialEntries={[`/run?candidate=${KEY}&skill=common%2Fnew-skill`]}><RunEval /></MemoryRouter>,
    );
    expect(await screen.findByRole("checkbox", { name: /common\/new-skill/i })).toBeChecked();
  });
});

describe("RunEval with drafts", () => {
  const DRAFT = "draft_6720a1b2c3d4e5f6a7b8c9d0";
  const draft: CandidateSummary = {
    ...candidate, kind: "draft", run_key: DRAFT, pr_number: null, git_sha: null, name: "draft-skill",
    uploaded_by: "ana@example.com", has_rubric: false,
  };

  it("badges a draft, and runs a draft without a rubric in trigger mode only", async () => {
    vi.mocked(api.me).mockResolvedValue(author);
    vi.mocked(api.candidates).mockResolvedValue([candidate, draft]);
    render(<MemoryRouter><RunEval /></MemoryRouter>);
    const group = await screen.findByRole("group", { name: /candidates under review/i });
    expect(within(group).getByText("Draft")).toBeTruthy();
    fireEvent.click(within(group).getByRole("checkbox", { name: /common\/draft-skill, draft/i }));
    expect(screen.getByRole("checkbox", { name: /effectiveness/i })).toBeDisabled();
    expect(screen.getByText(/without a rubric runs in trigger mode only/i)).toBeTruthy();
    fireEvent.click(screen.getByRole("checkbox", { name: /claude-sonnet/i }));
    fireEvent.click(screen.getByRole("checkbox", { name: /trigger/i }));
    fireEvent.click(screen.getByRole("button", { name: /start evaluation/i }));
    await screen.findByText(/run hub_0123abcd/i);
    expect(api.startEval).toHaveBeenLastCalledWith({
      candidate: { run_key: DRAFT, skills: ["common/draft-skill"] }, models: ["claude-sonnet"], modes: ["trigger"],
    });
  });
});
