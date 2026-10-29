import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi, beforeEach } from "vitest";
import Catalog, { SkillCard } from "./Catalog";
import { api, ApiError } from "../api/client";
import type { CandidateSummary, Me, SkillSummary } from "../api/types";

vi.mock("../api/client", async (orig) => {
  const real = await orig<typeof import("../api/client")>();
  return { ...real, api: { ...real.api, skills: vi.fn(), me: vi.fn(), candidates: vi.fn() } };
});

const reader: Me = { user: "rae@example.com", groups: ["skillhub-users"], is_reviewer: false, is_author: false };
const author: Me = { user: "ana@example.com", groups: ["skillhub-authors"], is_reviewer: false, is_author: true };

const skills: SkillSummary[] = [
  { fqn: "skill_hub.common.audit-xls", dept: "common", name: "audit-xls", description: "Audit a workbook",
    live_version: 3, verdicts: { "claude-sonnet": "pass", "gemini-flash": "warn" }, eval_access: "ok" },
  { fqn: "skill_hub.dept_equity_research.earnings-preview", dept: "equity_research", name: "earnings-preview",
    description: null, live_version: null, verdicts: {}, eval_access: "ok" },
];

// Four skills in two departments with mixed verdicts, for the filter tests.
const more: SkillSummary[] = [
  ...skills,
  { fqn: "skill_hub.dept_equity_research.morning-note", dept: "equity_research", name: "morning-note",
    description: "Draft the morning note", live_version: 1, verdicts: { "claude-sonnet": "fail" }, eval_access: "ok" },
  { fqn: "skill_hub.common.variance-check", dept: "common", name: "variance-check", description: null,
    live_version: 2, verdicts: { "claude-sonnet": "pass" }, eval_access: "ok" },
];

const candidate: CandidateSummary = {
  kind: "pr", uploaded_by: null, has_rubric: null, run_key: "pr5_c7ecd3e_a1", pr_number: 5, git_sha: "c7ecd3e", uploaded_at: "2026-10-28T10:00:00Z",
  dept: "common", name: "new-skill", description: "Drafts the quarterly note", content_hash: null,
  verdicts: { "claude-sonnet": "pass" }, eval_access: "ok",
};

beforeEach(() => {
  vi.mocked(api.skills).mockReset();
  vi.mocked(api.me).mockReset();
  vi.mocked(api.me).mockResolvedValue(reader);
  vi.mocked(api.candidates).mockReset();
  vi.mocked(api.candidates).mockResolvedValue([]);
});

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

  it("names the department in the section heading only, not again on each card", async () => {
    vi.mocked(api.skills).mockResolvedValue(more);
    renderCatalog();
    for (const [dept, name] of [["common", "audit-xls"], ["equity_research", "morning-note"]]) {
      const section = await screen.findByRole("region", { name: dept });
      expect(within(section).getByRole("heading", { level: 2, name: dept })).toBeTruthy();
      const card = within(section).getByRole("link", { name }).closest(".skill-card") as HTMLElement;
      expect(within(card).queryByText(dept)).toBeNull();
    }
  });

  it("keeps the department on a card shown outside a department section", () => {
    render(<MemoryRouter><ul><SkillCard s={skills[1]} /></ul></MemoryRouter>);
    expect(screen.getByText("equity_research")).toBeTruthy();
  });

  it("shows a loading status first", async () => {
    let release: (v: SkillSummary[]) => void = () => {};
    vi.mocked(api.skills).mockReturnValue(new Promise((r) => { release = r; }));
    renderCatalog();
    expect(screen.getByRole("status")).toHaveTextContent(/loading skills/i);
    release([]);
    expect(await screen.findByRole("heading", { name: /no skills/i })).toBeTruthy();
  });

  it("explains that no skills are live yet when nothing loads, and how to get access", async () => {
    vi.mocked(api.skills).mockResolvedValue([]);
    renderCatalog();
    expect(await screen.findByRole("heading", { name: /no skills are live yet/i })).toBeTruthy();
    expect(screen.getByText(/after its pull request merges and a reviewer approves the release/i)).toBeTruthy();
    expect(screen.getByText(/ask your team lead for access to your department/i)).toBeTruthy();
    // nothing is filtered, so there is nothing to clear
    expect(screen.queryByRole("button", { name: /clear filters/i })).toBeNull();
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

  it("shows the live count, which a search does not change", async () => {
    vi.mocked(api.skills).mockResolvedValue(more);
    renderCatalog();
    expect(await screen.findByText("4 live")).toBeTruthy();
    vi.mocked(api.skills).mockResolvedValue([more[0]]);
    fireEvent.change(screen.getByRole("searchbox", { name: /search skills/i }), { target: { value: "audit" } });
    expect(await screen.findByText("Showing 1 of 4")).toBeTruthy();
    expect(screen.getByText("4 live")).toBeTruthy();
  });

  it("filters by department, offering only the departments that loaded", async () => {
    vi.mocked(api.skills).mockResolvedValue(more);
    renderCatalog();
    const select = await screen.findByRole("combobox", { name: /department/i });
    expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual(["All", "common", "equity_research"]);
    fireEvent.change(select, { target: { value: "equity_research" } });
    expect(screen.queryByRole("region", { name: /common/i })).toBeNull();
    const equity = screen.getByRole("region", { name: /equity_research/i });
    expect(within(equity).getByText("earnings-preview")).toBeTruthy();
    expect(within(equity).getByText("morning-note")).toBeTruthy();
    expect(screen.getByText("Showing 2 of 4")).toBeTruthy();
    expect(api.skills).toHaveBeenCalledTimes(1); // filtering is local: no new request
  });

  it("filters by verdict: a skill matches when any model has that verdict", async () => {
    vi.mocked(api.skills).mockResolvedValue(more);
    renderCatalog();
    const select = await screen.findByRole("combobox", { name: /verdict/i });
    fireEvent.change(select, { target: { value: "warn" } });
    expect(screen.getByText("audit-xls")).toBeTruthy(); // pass on one model, warn on another
    expect(screen.queryByText("morning-note")).toBeNull();
    expect(screen.queryByText("variance-check")).toBeNull();
    fireEvent.change(select, { target: { value: "fail" } });
    expect(screen.getByText("morning-note")).toBeTruthy();
    expect(screen.queryByText("audit-xls")).toBeNull();
    fireEvent.change(select, { target: { value: "none" } });
    expect(screen.getByText("earnings-preview")).toBeTruthy();
    expect(screen.queryByText("morning-note")).toBeNull();
  });

  it("says no skills match the filters and offers to clear them", async () => {
    vi.mocked(api.skills).mockResolvedValue(more);
    renderCatalog();
    fireEvent.change(await screen.findByRole("combobox", { name: /department/i }), { target: { value: "common" } });
    fireEvent.change(screen.getByRole("combobox", { name: /verdict/i }), { target: { value: "fail" } });
    expect(screen.getByRole("heading", { name: /no skills match your filters/i })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: /no skills are live yet/i })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /clear filters/i }));
    expect(screen.queryByRole("heading", { name: /no skills match/i })).toBeNull();
    expect(screen.getByRole("combobox", { name: /department/i })).toHaveValue("");
    expect(screen.getByRole("combobox", { name: /verdict/i })).toHaveValue("");
    expect(screen.getByText("morning-note")).toBeTruthy();
    expect(screen.getByText("audit-xls")).toBeTruthy();
    expect(screen.getByRole("searchbox", { name: /search skills/i })).toHaveFocus();
  });

  it("clears the search as well, and reloads every skill", async () => {
    vi.mocked(api.skills).mockResolvedValue([]);
    renderCatalog();
    const box = await screen.findByRole("searchbox", { name: /search skills/i });
    fireEvent.change(box, { target: { value: "nothing-like-this" } });
    expect(await screen.findByRole("heading", { name: /no skills match/i })).toBeTruthy();
    vi.mocked(api.skills).mockResolvedValue(more);
    fireEvent.click(screen.getByRole("button", { name: /clear filters/i }));
    expect(await screen.findByText("morning-note")).toBeTruthy();
    expect(box).toHaveValue("");
    expect(api.skills).toHaveBeenLastCalledWith(undefined);
  });
});

describe("Catalog skills under review", () => {
  it("lists skills under review in their department, after the live skills, with no separate section", async () => {
    vi.mocked(api.me).mockResolvedValue(author);
    vi.mocked(api.skills).mockResolvedValue(skills);
    vi.mocked(api.candidates).mockResolvedValue([candidate]);
    renderCatalog();
    const common = await screen.findByRole("region", { name: "common" });
    const review = await within(common).findByRole("link", { name: "new-skill" });
    const live = within(common).getByRole("link", { name: "audit-xls" });
    // DOCUMENT_POSITION_FOLLOWING: the candidate card comes after the live card.
    expect(live.compareDocumentPosition(review) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.queryByRole("region", { name: /under review/i })).toBeNull();
    expect(within(common).getByText("1 live, 1 under review")).toBeTruthy();
  });

  it("badges a candidate with its pull request, in the live card shape", async () => {
    vi.mocked(api.me).mockResolvedValue(author);
    vi.mocked(api.skills).mockResolvedValue(skills);
    vi.mocked(api.candidates).mockResolvedValue([candidate]);
    renderCatalog();
    const common = await screen.findByRole("region", { name: "common" });
    const link = await within(common).findByRole("link", { name: "new-skill" });
    expect(link).toHaveAttribute("href", "/candidates/pr5_c7ecd3e_a1/common/new-skill");
    const card = link.closest(".skill-card") as HTMLElement;
    expect(within(card).getByText("Candidate · PR #5")).toBeTruthy();
    expect(within(card).queryByText("common")).toBeNull(); // the section heading names the department
    expect(within(card).getByText("Pass")).toBeTruthy();
    expect(screen.getByText("1 under review")).toBeTruthy();
  });

  it("gives a department that has only skills under review its own section", async () => {
    vi.mocked(api.me).mockResolvedValue(author);
    vi.mocked(api.skills).mockResolvedValue(skills);
    vi.mocked(api.candidates).mockResolvedValue([{ ...candidate, dept: "private_equity" }]);
    renderCatalog();
    const pe = await screen.findByRole("region", { name: "private_equity" });
    expect(within(pe).getByRole("link", { name: "new-skill" })).toBeTruthy();
  });

  it("does not ask for candidates for a reader, and offers no status filter", async () => {
    vi.mocked(api.skills).mockResolvedValue(skills);
    renderCatalog();
    expect(await screen.findByRole("region", { name: "common" })).toBeTruthy();
    expect(api.candidates).not.toHaveBeenCalled();
    expect(screen.queryByRole("combobox", { name: /status/i })).toBeNull();
    expect(screen.queryByText("Candidate · PR #5")).toBeNull();
  });

  it("filters by status: live skills only, or skills under review only", async () => {
    vi.mocked(api.me).mockResolvedValue(author);
    vi.mocked(api.skills).mockResolvedValue(skills);
    vi.mocked(api.candidates).mockResolvedValue([candidate]);
    renderCatalog();
    const status = await screen.findByRole("combobox", { name: /status/i });
    await screen.findByRole("link", { name: "new-skill" });
    expect(within(status).getAllByRole("option").map((o) => o.textContent)).toEqual(["All", "Live", "Under review"]);
    fireEvent.change(status, { target: { value: "live" } });
    expect(screen.queryByRole("link", { name: "new-skill" })).toBeNull();
    expect(screen.getByRole("link", { name: "audit-xls" })).toBeTruthy();
    fireEvent.change(status, { target: { value: "candidate" } });
    expect(within(screen.getByRole("region", { name: "common" })).getByRole("link", { name: "new-skill" })).toBeTruthy();
    expect(screen.queryByRole("link", { name: "audit-xls" })).toBeNull();
    expect(screen.queryByRole("region", { name: "equity_research" })).toBeNull();
  });

  it("applies the search and department filters to skills under review too", async () => {
    vi.mocked(api.me).mockResolvedValue(author);
    vi.mocked(api.skills).mockResolvedValue(more);
    vi.mocked(api.candidates).mockResolvedValue([candidate]);
    renderCatalog();
    await screen.findByRole("link", { name: "new-skill" });
    fireEvent.change(screen.getByRole("combobox", { name: /department/i }), { target: { value: "equity_research" } });
    expect(screen.queryByRole("link", { name: "new-skill" })).toBeNull();
    fireEvent.change(screen.getByRole("combobox", { name: /department/i }), { target: { value: "" } });
    fireEvent.change(screen.getByRole("searchbox", { name: /search skills/i }), { target: { value: "quarterly" } });
    vi.mocked(api.skills).mockResolvedValue([]);
    expect(await screen.findByText("new-skill")).toBeTruthy();
    expect(screen.queryByRole("heading", { name: /no skills match/i })).toBeNull();
  });

  it("explains an empty under-review view", async () => {
    vi.mocked(api.me).mockResolvedValue(author);
    vi.mocked(api.skills).mockResolvedValue(skills);
    renderCatalog();
    fireEvent.change(await screen.findByRole("combobox", { name: /status/i }), { target: { value: "candidate" } });
    expect(await screen.findByRole("heading", { name: /no skills are under review/i })).toBeTruthy();
  });

  it("keeps the live catalog when the candidates cannot load", async () => {
    vi.mocked(api.me).mockResolvedValue(author);
    vi.mocked(api.skills).mockResolvedValue(skills);
    vi.mocked(api.candidates).mockRejectedValue(new ApiError(502, "candidate_unreadable"));
    renderCatalog();
    expect(await screen.findByRole("alert")).toHaveTextContent(/candidate/i);
    expect(screen.getByRole("region", { name: "common" })).toBeTruthy();
  });
});

describe("Catalog drafts", () => {
  const draft: CandidateSummary = {
    ...candidate, kind: "draft", run_key: "draft_6720a1b2c3d4e5f6a7b8c9d0", pr_number: null, git_sha: null,
    name: "draft-skill", uploaded_by: "ana@example.com", has_rubric: false,
  };

  it("badges a draft as a draft and links to its page", async () => {
    vi.mocked(api.me).mockResolvedValue(author);
    vi.mocked(api.skills).mockResolvedValue(skills);
    vi.mocked(api.candidates).mockResolvedValue([draft]);
    renderCatalog();
    const common = await screen.findByRole("region", { name: "common" });
    const link = await within(common).findByRole("link", { name: "draft-skill" });
    expect(link).toHaveAttribute("href", "/candidates/draft_6720a1b2c3d4e5f6a7b8c9d0/common/draft-skill");
    expect(within(link.closest(".skill-card") as HTMLElement).getByText("Draft")).toBeTruthy();
  });

  it("offers Upload draft to authors and reviewers only", async () => {
    vi.mocked(api.skills).mockResolvedValue(skills);
    vi.mocked(api.me).mockResolvedValue(author);
    const { unmount } = renderCatalog();
    expect(await screen.findByRole("link", { name: /upload draft/i })).toHaveAttribute("href", "/drafts/new");
    unmount();
    vi.mocked(api.me).mockResolvedValue(reader);
    renderCatalog();
    await screen.findByRole("region", { name: "common" });
    expect(screen.queryByRole("link", { name: /upload draft/i })).toBeNull();
  });
});
