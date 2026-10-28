import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi, beforeEach } from "vitest";
import Catalog from "./Catalog";
import { api, ApiError } from "../api/client";
import type { SkillSummary } from "../api/types";

vi.mock("../api/client", async (orig) => {
  const real = await orig<typeof import("../api/client")>();
  return { ...real, api: { ...real.api, skills: vi.fn() } };
});

const skills: SkillSummary[] = [
  { fqn: "skill_hub.common.audit-xls", dept: "common", name: "audit-xls", description: "Audit a workbook",
    live_version: 3, verdicts: { "claude-sonnet": "pass", "gemini-flash": "warn" }, eval_access: "ok" },
  { fqn: "skill_hub.dept_equity_research.earnings-preview", dept: "equity_research", name: "earnings-preview",
    description: null, live_version: null, verdicts: {}, eval_access: "ok" },
];

beforeEach(() => vi.mocked(api.skills).mockReset());

function renderCatalog() {
  return render(<MemoryRouter><Catalog /></MemoryRouter>);
}

describe("Catalog", () => {
  it.each([
    ["denied", "You can't read eval results for this department"],
    ["unavailable", "Eval results are temporarily unavailable"],
  ] as const)("notes %s eval access on the department's skills", async (access, message) => {
    vi.mocked(api.skills).mockResolvedValue([{ ...skills[0], verdicts: {}, eval_access: access }, skills[1]]);
    renderCatalog();
    const common = await screen.findByRole("region", { name: /common/i });
    expect(within(common).getByRole("note")).toHaveTextContent(message);
    const other = screen.getByRole("region", { name: /equity_research/i });
    expect(within(other).queryByRole("note")).toBeNull();
  });

  it("groups skills by schema and shows a verdict chip per model", async () => {
    vi.mocked(api.skills).mockResolvedValue(skills);
    renderCatalog();
    const common = await screen.findByRole("region", { name: /common/i });
    expect(within(common).getByText("audit-xls")).toBeTruthy();
    expect(within(common).getByText("Live version 3")).toBeTruthy();
    expect(within(common).getByText("Pass")).toBeTruthy();
    expect(within(common).getByText("Warn")).toBeTruthy();
    const equity = screen.getByRole("region", { name: /equity_research/i });
    expect(within(equity).getByText("No live version")).toBeTruthy();
    expect(within(equity).getByText("No results yet")).toBeTruthy();
  });

  it("shows a loading status first", async () => {
    let release: (v: SkillSummary[]) => void = () => {};
    vi.mocked(api.skills).mockReturnValue(new Promise((r) => { release = r; }));
    renderCatalog();
    expect(screen.getByRole("status")).toHaveTextContent(/loading skills/i);
    release([]);
    expect(await screen.findByRole("heading", { name: /no skills/i })).toBeTruthy();
  });

  it("shows an empty state when nothing is visible", async () => {
    vi.mocked(api.skills).mockResolvedValue([]);
    renderCatalog();
    expect(await screen.findByRole("heading", { name: /no skills/i })).toBeTruthy();
    expect(screen.getByText(/may not have access/i)).toBeTruthy();
  });

  it("shows an error with retry that reloads", async () => {
    vi.mocked(api.skills).mockRejectedValueOnce(new ApiError(500, "boom")).mockResolvedValue(skills);
    renderCatalog();
    expect(await screen.findByRole("alert")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /try again/i }));
    expect(await screen.findByText("audit-xls")).toBeTruthy();
  });

  it("sends the search text as q", async () => {
    vi.mocked(api.skills).mockResolvedValue([]);
    renderCatalog();
    const box = await screen.findByRole("searchbox", { name: /search skills/i });
    fireEvent.change(box, { target: { value: "audit" } });
    expect(await screen.findByRole("heading", { name: /no skills match/i })).toBeTruthy();
    expect(api.skills).toHaveBeenLastCalledWith("audit");
  });
});
