import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi, beforeEach } from "vitest";
import Matrix from "./Matrix";
import { api } from "../api/client";

vi.mock("../api/client", async (orig) => {
  const real = await orig<typeof import("../api/client")>();
  return { ...real, api: { ...real.api, matrix: vi.fn(), config: vi.fn(), cases: vi.fn() } };
});

const row = {
  dept: "common", skill: "audit-xls", skill_version: 3, content_hash: "sha256:x", model_profile: "claude-sonnet",
  harness: "reference", endpoint: "ep", model_class: "frontier", mode: "trigger" as const, verdict: "pass" as const,
  run_id: "r1", experiment_id: "e1", n_cases: 10, n_reviewed: 6, pass_rate: 0.9, spread: 0.05, error_rate: 0,
  accuracy: 0.9, precision: 0.85, recall: 0.95, pass_rate_without: null, uplift_task_quality: null,
  uplift_skill_format: null, started_at: "2026-10-28T00:00:00",
};

const listed = <T,>(rows: T[], deniedDepartments: string[] = [], truncated = false) => ({ rows, deniedDepartments, truncated });

beforeEach(() => {
  vi.mocked(api.matrix).mockReset().mockResolvedValue(listed([row]));
  vi.mocked(api.config).mockReset().mockResolvedValue({
    models: { "claude-sonnet": "frontier" }, departments: ["common"], workspace_url: "https://ws.example.test",
  });
});

describe("Matrix", () => {
  it("links to the trace of a case and shows an empty state with no cases", async () => {
    vi.mocked(api.cases).mockResolvedValue(listed([{
      run_id: "r1", trace_id: "tr1", assessment_name: "triggered", case_id: 7, repeat: 0, mode: "trigger",
      user_input: "u", expected_skill: null, invoked_skill: null, trigger_outcome: "tp", value: "yes",
      rationale: null, judge_model: null, source_type: null, reviewed: false, dept: "common",
    }]));
    render(<MemoryRouter><Matrix /></MemoryRouter>);
    fireEvent.click(await screen.findByRole("button", { name: /show cases/i }));
    const link = await screen.findByRole("link", { name: /trace for case 7/i });
    expect(link.getAttribute("href")).toBe("https://ws.example.test/ml/experiments/e1/traces?selectedEvaluationId=tr1");
  });

  it("shows a table with the unreviewed count and the model class", async () => {
    render(<MemoryRouter initialEntries={["/matrix?skill=audit-xls&dept=common"]}><Matrix /></MemoryRouter>);
    const table = await screen.findByRole("table", { name: /evaluation matrix/i });
    expect(within(table).getByText("claude-sonnet")).toBeTruthy();
    expect(within(table).getByText("frontier")).toBeTruthy();
    expect(within(table).getByText("reference")).toBeTruthy(); // the harness column
    expect(within(table).getByText("4")).toBeTruthy(); // 10 cases - 6 reviewed
    expect(vi.mocked(api.matrix)).toHaveBeenCalledWith({ skill: "audit-xls", dept: "common" });
  });

  it("links to the MLflow run", async () => {
    render(<MemoryRouter><Matrix /></MemoryRouter>);
    const link = await screen.findByRole("link", { name: /mlflow run/i });
    expect(link.getAttribute("href")).toBe("https://ws.example.test/ml/experiments/e1/runs/r1");
  });

  it("encodes special characters in run and trace URLs and trims a trailing slash", async () => {
    vi.mocked(api.config).mockResolvedValue({
      models: {}, departments: [], workspace_url: "https://ws.example.test/",
    });
    vi.mocked(api.matrix).mockResolvedValue(listed([{ ...row, run_id: "r/1?x", experiment_id: "e 1" }]));
    vi.mocked(api.cases).mockResolvedValue(listed([{
      run_id: "r", trace_id: "t#1", assessment_name: "a", case_id: 1, repeat: 0, mode: "trigger", user_input: "u",
      expected_skill: null, invoked_skill: null, trigger_outcome: null, value: null, rationale: null,
      judge_model: null, source_type: null, reviewed: false, dept: "common",
    }]));
    render(<MemoryRouter><Matrix /></MemoryRouter>);
    const link = await screen.findByRole("link", { name: /mlflow run/i });
    expect(link.getAttribute("href")).toBe("https://ws.example.test/ml/experiments/e%201/runs/r%2F1%3Fx");
    fireEvent.click(screen.getByRole("button", { name: /show cases for claude-sonnet trigger v3/i }));
    const trace = await screen.findByRole("link", { name: /trace for case 1/i });
    expect(trace.getAttribute("href")).toContain("selectedEvaluationId=t%231");
  });

  it("renders no MLflow links and shows a notice when the workspace URL is empty", async () => {
    vi.mocked(api.config).mockResolvedValue({ models: {}, departments: [], workspace_url: "" });
    render(<MemoryRouter><Matrix /></MemoryRouter>);
    await screen.findByRole("table", { name: /evaluation matrix/i });
    await screen.findByText(/mlflow links are unavailable/i);
    expect(screen.queryByRole("link", { name: /mlflow run/i })).toBeNull();
  });

  it("renders no MLflow links when config fails", async () => {
    vi.mocked(api.config).mockRejectedValue(new Error("down"));
    render(<MemoryRouter><Matrix /></MemoryRouter>);
    await screen.findByRole("table", { name: /evaluation matrix/i });
    await screen.findByText(/mlflow links are unavailable/i);
    expect(screen.queryByRole("link", { name: /mlflow run/i })).toBeNull();
  });

  it("shows an accessible note naming the departments the user cannot read", async () => {
    vi.mocked(api.matrix).mockResolvedValue(listed([row], ["equity_research", "external"]));
    render(<MemoryRouter><Matrix /></MemoryRouter>);
    const note = await screen.findByText(/some departments' results are hidden/i);
    expect(note.getAttribute("role")).toBe("note");
    expect(note.textContent).toContain("equity_research, external");
  });

  it("shows no hidden-departments note when nothing is refused", async () => {
    render(<MemoryRouter><Matrix /></MemoryRouter>);
    await screen.findByRole("table", { name: /evaluation matrix/i });
    expect(screen.queryByText(/results are hidden/i)).toBeNull();
  });
});
