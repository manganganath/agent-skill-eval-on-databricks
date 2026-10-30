import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Dashboard from "./Dashboard";
import { api } from "../api/client";
import type { Dashboard as DashboardData } from "../api/types";

vi.mock("../api/client", async (orig) => {
  const real = await orig<typeof import("../api/client")>();
  return { ...real, api: { ...real.api, dashboard: vi.fn() } };
});

const summary = {
  skills: 2, verdicts: { pass: 3, warn: 0, fail: 1, inconclusive: 1, advisory: 0 },
  trigger_accuracy: 0.75, uplift_task_quality: 0.2, error_rate: 0.025,
};

function data(over: Partial<DashboardData> = {}): DashboardData {
  return {
    summary,
    verdicts: [
      { dept: "common", model_profile: "claude-sonnet", verdict: "pass", n: 3 },
      { dept: "common", model_profile: "claude-sonnet", verdict: "inconclusive", n: 1 },
      { dept: "equity_research", model_profile: "gemini-flash", verdict: "fail", n: 1 },
    ],
    uplift: [
      { dept: "common", skill: "sales-brief", skill_version: 2, model_profile: "claude-sonnet",
        pass_rate_without: 0.5, pass_rate: 0.8, uplift_task_quality: 0.3, uplift_skill_format: 0.1 },
    ],
    routing: [
      { dept: "common", model_profile: "claude-sonnet", expected_skill: "sales-brief", invoked_skill: "sales-brief",
        trigger_outcome: "correct", n: 4 },
      { dept: "common", model_profile: "claude-sonnet", expected_skill: "sales-brief", invoked_skill: "audit-xls",
        trigger_outcome: "wrong_skill", n: 2 },
    ],
    trigger_trend: [
      { dept: "common", skill: "sales-brief", skill_version: 2, model_profile: "claude-sonnet", day: "2026-10-01",
        accuracy: 0.75, runs: 2 },
    ],
    pass_rate_trend: [
      { dept: "common", skill: "sales-brief", skill_version: 2, model_profile: "claude-sonnet", day: "2026-10-01",
        pass_rate: 0.8, runs: 1 },
    ],
    health: [
      { dept: "common", model_profile: "claude-sonnet", cells: 4, error_rate: 0.05, inconclusive_rate: 0.25 },
    ],
    tokens: [
      { model_profile: "claude-sonnet", endpoint_name: "ep-claude", day: "2026-10-01", source: "eval", input_tokens: 1200, output_tokens: 300 },
      { model_profile: "claude-sonnet", endpoint_name: "ep-claude", day: "2026-10-01", source: "try", input_tokens: 400, output_tokens: 100 },
    ],
    deniedDepartments: [],
    ...over,
  };
}

function renderPage() {
  return render(<MemoryRouter><Dashboard /></MemoryRouter>);
}

beforeEach(() => {
  vi.mocked(api.dashboard).mockReset().mockResolvedValue(data());
});

describe("Dashboard", () => {
  it("shows the summary tiles and a heading for each section", async () => {
    renderPage();
    const tiles = await screen.findByRole("region", { name: "Summary" });
    expect(within(tiles).getByText("Skills with results")).toBeTruthy();
    expect(within(tiles).getByText("75%")).toBeTruthy(); // trigger accuracy
    expect(within(tiles).getByText("+20%")).toBeTruthy(); // task-quality uplift
    for (const name of [
      "Verdicts by model", "Uplift with and without the skill", "Trigger routing",
      "Trigger accuracy over time", "Health by model", "Token usage",
    ]) {
      expect(screen.getByRole("heading", { level: 2, name })).toBeTruthy();
    }
  });

  it("names every chart and gives it a table alternative", async () => {
    renderPage();
    await screen.findByRole("heading", { name: "Verdicts by model" });
    expect(screen.getByRole("img", { name: "Verdicts by model, stacked counts" })).toBeTruthy();
    expect(screen.getByRole("img", { name: "Task-quality uplift by skill and model" })).toBeTruthy();
    expect(screen.getByRole("img", { name: "Trigger outcomes by expected skill" })).toBeTruthy();
    expect(screen.getByRole("img", { name: "Trigger accuracy per day, by model" })).toBeTruthy();
    expect(screen.getByRole("img", { name: "Error and inconclusive rate by model" })).toBeTruthy();
    expect(screen.getByRole("img", { name: "Input tokens per day, by model" })).toBeTruthy();
    expect(screen.getByRole("table", { name: "Verdict counts by model" })).toBeTruthy();
  });

  it("filters every section by department, and the tiles keep the whole result", async () => {
    renderPage();
    await screen.findByRole("heading", { name: "Verdicts by model" });
    const verdicts = screen.getByRole("heading", { name: "Verdicts by model" }).closest("section")!;
    expect(within(verdicts).getByRole("img", { name: "Verdicts by model, stacked counts" })).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Department"), { target: { value: "equity_research" } });
    const counts = within(verdicts).getByRole("table", { name: "Verdict counts by model" });
    expect(within(counts).queryByText("claude-sonnet")).toBeNull();
    expect(within(counts).getByText("gemini-flash")).toBeTruthy();
    const tiles = screen.getByRole("region", { name: "Summary" });
    expect(within(tiles).getByText("75%")).toBeTruthy();
  });

  it("filters by model, which also narrows the token rows", async () => {
    renderPage();
    await screen.findByRole("heading", { name: "Token usage" });
    fireEvent.change(screen.getByLabelText("Model"), { target: { value: "gemini-flash" } });
    expect(screen.getByText("No evaluation or Try token usage in the last 30 days for this selection.")).toBeTruthy();
  });

  it("totals the tokens of each source, and the source filter narrows the chart and table only", async () => {
    renderPage();
    await screen.findByRole("heading", { name: "Token usage" });
    const totals = screen.getByRole("region", { name: "Token totals by source" });
    expect(within(totals).getByText("Evaluation tokens")).toBeTruthy();
    expect(within(totals).getByText("1,500")).toBeTruthy();
    expect(within(totals).getByText("Try tokens")).toBeTruthy();
    expect(within(totals).getByText("500")).toBeTruthy();

    const rows = screen.getByRole("region", { name: "Token usage rows" });
    expect(within(rows).getAllByRole("row")).toHaveLength(3); // header and two rows
    fireEvent.change(screen.getByLabelText("Source"), { target: { value: "try" } });
    expect(within(rows).getAllByRole("row")).toHaveLength(2);
    expect(within(rows).getByText("Try")).toBeTruthy();
    expect(within(rows).getByText("400")).toBeTruthy();
    expect(within(totals).getByText("1,500")).toBeTruthy(); // the totals keep both sources
    expect(screen.getByRole("img", { name: "Input tokens per day, by model" })).toBeTruthy();
  });

  it("says so when the chosen source has no tokens in the period", async () => {
    vi.mocked(api.dashboard).mockResolvedValue(data({
      tokens: [{ model_profile: "claude-sonnet", endpoint_name: "ep-claude", day: "2026-10-01", source: "eval", input_tokens: 5, output_tokens: 1 }],
    }));
    renderPage();
    await screen.findByRole("heading", { name: "Token usage" });
    fireEvent.change(screen.getByLabelText("Source"), { target: { value: "try" } });
    expect(screen.getByText("No Try token usage in the last 30 days for this selection.")).toBeTruthy();
    expect(screen.queryByRole("img", { name: "Input tokens per day, by model" })).toBeNull();
  });

  it("links a skill to its matrix rows for its department", async () => {
    renderPage();
    await screen.findByRole("heading", { name: "Uplift with and without the skill" });
    const link = screen.getByRole("link", { name: /sales-brief/ });
    expect(link.getAttribute("href")).toBe("/matrix?skill=sales-brief&dept=common");
  });

  it("lists the top mis-routes with their expected and invoked skill", async () => {
    renderPage();
    const table = await screen.findByRole("region", { name: "Top mis-routes" });
    expect(within(table).getByText("audit-xls")).toBeTruthy();
    expect(within(table).getByText("Wrong skill")).toBeTruthy();
  });

  it("says the denied departments are hidden", async () => {
    vi.mocked(api.dashboard).mockResolvedValue(data({ deniedDepartments: ["external"] }));
    renderPage();
    expect(await screen.findByRole("note")).toHaveTextContent("you can't read external");
  });

  it("shows the empty state when there are no results", async () => {
    vi.mocked(api.dashboard).mockResolvedValue(data({
      summary: { ...summary, skills: 0 }, verdicts: [], uplift: [], routing: [], trigger_trend: [],
      pass_rate_trend: [], health: [], tokens: [],
    }));
    renderPage();
    expect(await screen.findByRole("heading", { name: "No results yet" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Verdicts by model" })).toBeNull();
  });

  it("asks for system.serving access when the token section is null", async () => {
    vi.mocked(api.dashboard).mockResolvedValue(data({ tokens: null }));
    renderPage();
    const note = await screen.findByText(/needs access to the system\.serving tables/i);
    expect(note.closest("p")?.getAttribute("role")).toBe("note");
    expect(screen.queryByRole("img", { name: "Input tokens per day, by model" })).toBeNull();
    expect(screen.queryByLabelText("Source")).toBeNull();
  });

  it("shows an error with a retry when the dashboard fails", async () => {
    vi.mocked(api.dashboard).mockRejectedValue(new Error("network"));
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent("Something went wrong");
  });
});
