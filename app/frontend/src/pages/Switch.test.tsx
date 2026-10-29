import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi, beforeEach } from "vitest";
import Switch from "./Switch";
import { api, ApiError } from "../api/client";

vi.mock("../api/client", async (orig) => {
  const real = await orig<typeof import("../api/client")>();
  return {
    ...real,
    api: { ...real.api, me: vi.fn(), versions: vi.fn(), matrix: vi.fn(), switchVersion: vi.fn() },
  };
});

const FQN = "skill_hub.common.audit-xls";

function renderSwitch() {
  return render(
    <MemoryRouter initialEntries={[`/skills/${FQN}/switch`]}>
      <Routes><Route path="/skills/:fqn/switch" element={<Switch />} /></Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.mocked(api.versions).mockResolvedValue([
    { dept: "common", skill: "audit-xls", action: "published", version: 1, from_version: null, to_version: null,
      versioned_fqn: null, content_hash: null, git_sha: null, eval_run_id: null, actor: "ci", job_run_id: null, ts: "t1" },
    { dept: "common", skill: "audit-xls", action: "published", version: 2, from_version: null, to_version: null,
      versioned_fqn: null, content_hash: null, git_sha: null, eval_run_id: null, actor: "ci", job_run_id: null, ts: "t2" },
  ]);
  vi.mocked(api.matrix).mockReset().mockResolvedValue({ rows: [], deniedDepartments: [], truncated: false });
  vi.mocked(api.switchVersion).mockReset().mockResolvedValue({ job_run_id: 42 });
});

describe("Switch", () => {
  it("shows a message and no controls to a non-reviewer", async () => {
    vi.mocked(api.me).mockResolvedValue({ user: "ana@example.com", groups: [], is_reviewer: false, is_author: false });
    renderSwitch();
    expect(await screen.findByText(/only skill reviewers/i)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /confirm switch/i })).toBeNull();
  });

  it("keeps the confirm button disabled until the change is acknowledged", async () => {
    vi.mocked(api.me).mockResolvedValue({ user: "rita@example.com", groups: ["skillhub-reviewers"], is_reviewer: true, is_author: false });
    renderSwitch();
    fireEvent.change(await screen.findByLabelText(/target version/i), { target: { value: "1" } });
    const confirm = screen.getByRole("button", { name: /confirm switch/i });
    expect(confirm).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox", { name: /i understand/i }));
    expect(confirm).toBeEnabled();
  });

  it("does not treat a published version as live", async () => {
    vi.mocked(api.me).mockResolvedValue({ user: "rita@example.com", groups: [], is_reviewer: true, is_author: false });
    renderSwitch();
    expect(await screen.findByText("Live version: none")).toBeTruthy();
    // both published versions can be targeted, since none is live
    expect(screen.getByRole("option", { name: "v2" })).toBeTruthy();
  });

  it("maps a 409 to a plain message", async () => {
    vi.mocked(api.me).mockResolvedValue({ user: "rita@example.com", groups: ["skillhub-reviewers"], is_reviewer: true, is_author: false });
    vi.mocked(api.switchVersion).mockRejectedValue(new ApiError(409, "already_live"));
    renderSwitch();
    fireEvent.change(await screen.findByLabelText(/target version/i), { target: { value: "1" } });
    fireEvent.click(screen.getByRole("checkbox", { name: /i understand/i }));
    fireEvent.click(screen.getByRole("button", { name: /confirm switch/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/already live/i);
  });

  const reviewer = () =>
    vi.mocked(api.me).mockResolvedValue({ user: "rita@example.com", groups: ["skillhub-reviewers"], is_reviewer: true, is_author: false });
  async function pickAndAck() {
    fireEvent.change(await screen.findByLabelText(/target version/i), { target: { value: "1" } });
    fireEvent.click(screen.getByRole("checkbox", { name: /i understand/i }));
  }

  it("states from and to on the acknowledgement even when the matrix is empty", async () => {
    reviewer();
    renderSwitch();
    await pickAndAck();
    expect(screen.getByRole("checkbox", { name: /switch audit-xls from none to v1/i })).toBeTruthy();
  });

  it("states the live version as the from side", async () => {
    reviewer();
    vi.mocked(api.versions).mockResolvedValue([
      ...(await api.versions("x")),
      { dept: "common", skill: "audit-xls", action: "switched", version: null, from_version: null, to_version: 2,
        versioned_fqn: null, content_hash: null, git_sha: null, eval_run_id: null, actor: "r", job_run_id: null, ts: "t3" },
    ]);
    renderSwitch();
    fireEvent.change(await screen.findByLabelText(/target version/i), { target: { value: "1" } });
    expect(screen.getByRole("checkbox", { name: /switch audit-xls from v2 to v1/i })).toBeTruthy();
  });

  it("calls switchVersion once on a double click and shows the job id in the status region", async () => {
    reviewer();
    renderSwitch();
    await pickAndAck();
    const confirm = screen.getByRole("button", { name: /confirm switch/i });
    fireEvent.click(confirm);
    fireEvent.click(confirm);
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(/job 42/i));
    expect(api.switchVersion).toHaveBeenCalledTimes(1);
    expect(confirm).toBeDisabled();
  });

  it("maps a 403 not_reviewer to a plain message", async () => {
    reviewer();
    vi.mocked(api.switchVersion).mockRejectedValue(new ApiError(403, "not_reviewer"));
    renderSwitch();
    await pickAndAck();
    fireEvent.click(screen.getByRole("button", { name: /confirm switch/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/only skill reviewers/i);
  });

  it("shows live against target verdicts and the pass rate change", async () => {
    reviewer();
    vi.mocked(api.versions).mockResolvedValue([
      { dept: "common", skill: "audit-xls", action: "published", version: 1, from_version: null, to_version: null,
        versioned_fqn: null, content_hash: null, git_sha: null, eval_run_id: null, actor: "ci", job_run_id: null, ts: "t1" },
      { dept: "common", skill: "audit-xls", action: "published", version: 2, from_version: null, to_version: null,
        versioned_fqn: null, content_hash: null, git_sha: null, eval_run_id: null, actor: "ci", job_run_id: null, ts: "t2" },
      { dept: "common", skill: "audit-xls", action: "switched", version: null, from_version: null, to_version: 2,
        versioned_fqn: null, content_hash: null, git_sha: null, eval_run_id: null, actor: "r", job_run_id: null, ts: "t3" },
    ]);
    const base = {
      dept: "common", skill: "audit-xls", content_hash: "h", model_profile: "claude-sonnet", endpoint: "e",
      model_class: "frontier", mode: "trigger" as const, run_id: "r", experiment_id: "e1", n_cases: 1, n_reviewed: 1,
      spread: null, error_rate: null, accuracy: null, precision: null, recall: null, pass_rate_without: null,
      uplift_task_quality: null, uplift_skill_format: null, started_at: "t",
    };
    vi.mocked(api.matrix).mockResolvedValue({
      rows: [
        { ...base, skill_version: 2, verdict: "pass", pass_rate: 0.9 },
        { ...base, skill_version: 1, verdict: "fail", pass_rate: 0.5 },
      ],
      deniedDepartments: [],
      truncated: false,
    });
    renderSwitch();
    fireEvent.change(await screen.findByLabelText(/target version/i), { target: { value: "1" } });
    const table = await screen.findByRole("table", { name: /matrix difference/i });
    expect(within(table).getByText("Pass")).toBeTruthy();
    expect(within(table).getByText("Fail")).toBeTruthy();
    expect(within(table).getByText("-40 pts")).toBeTruthy();
    expect(api.matrix).toHaveBeenCalledWith({ fqn: FQN });
  });

  it("keeps one diff row per harness", async () => {
    reviewer();
    vi.mocked(api.versions).mockResolvedValue([
      { dept: "common", skill: "audit-xls", action: "published", version: 1, from_version: null, to_version: null,
        versioned_fqn: null, content_hash: null, git_sha: null, eval_run_id: null, actor: "ci", job_run_id: null, ts: "t1" },
      { dept: "common", skill: "audit-xls", action: "published", version: 2, from_version: null, to_version: null,
        versioned_fqn: null, content_hash: null, git_sha: null, eval_run_id: null, actor: "ci", job_run_id: null, ts: "t2" },
    ]);
    const base = {
      dept: "common", skill: "audit-xls", content_hash: "h", model_profile: "claude-sonnet", endpoint: "e",
      model_class: "frontier", mode: "trigger" as const, run_id: "r", experiment_id: "e1", n_cases: 1, n_reviewed: 1,
      spread: null, error_rate: null, accuracy: null, precision: null, recall: null, pass_rate_without: null,
      uplift_task_quality: null, uplift_skill_format: null, started_at: "t", skill_version: 1, pass_rate: 0.5,
    };
    vi.mocked(api.matrix).mockResolvedValue({
      rows: [
        { ...base, harness: "reference", verdict: "pass" },
        { ...base, harness: "external", verdict: "fail" },
      ],
      deniedDepartments: [],
      truncated: false,
    });
    renderSwitch();
    fireEvent.change(await screen.findByLabelText(/target version/i), { target: { value: "1" } });
    const table = await screen.findByRole("table", { name: /matrix difference/i });
    expect(within(table).getByText("claude-sonnet")).toBeTruthy();
    expect(within(table).getByText("claude-sonnet (external)")).toBeTruthy();
    expect(within(table).getAllByRole("row")).toHaveLength(3); // the header and one row per harness
  });

  it("asks for the matrix by FQN for a dept_ schema, never with the schema name as the department", async () => {
    reviewer();
    const deptFqn = "skill_hub.dept_equity_research.earnings-preview";
    render(
      <MemoryRouter initialEntries={[`/skills/${deptFqn}/switch`]}>
        <Routes><Route path="/skills/:fqn/switch" element={<Switch />} /></Routes>
      </MemoryRouter>,
    );
    expect(await screen.findByRole("heading", { name: /switch version: earnings-preview/i })).toBeTruthy();
    await waitFor(() => expect(api.matrix).toHaveBeenCalledWith({ fqn: deptFqn }));
    for (const [params] of vi.mocked(api.matrix).mock.calls) {
      expect((params as { dept?: string }).dept).not.toBe("dept_equity_research");
    }
  });
});
