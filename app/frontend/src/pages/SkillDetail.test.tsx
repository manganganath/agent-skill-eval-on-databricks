import { render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi, beforeEach } from "vitest";
import SkillDetail, { liveVersion } from "./SkillDetail";
import { api, ApiError } from "../api/client";
import type { JournalRow, SkillDetail as SkillDetailT } from "../api/types";

// `me`, `config` and `skills` are mocked so no test does a real fetch, and so a test can check that a call is not made.
vi.mock("../api/client", async (orig) => {
  const real = await orig<typeof import("../api/client")>();
  return {
    ...real,
    api: { ...real.api, skill: vi.fn(), skills: vi.fn(), versions: vi.fn(), me: vi.fn(), config: vi.fn() },
  };
});

const detail: SkillDetailT = {
  fqn: "skill_hub.common.audit-xls", dept: "common", name: "audit-xls", description: "Audit",
  live_version: null, verdicts: {},
  files: { "SKILL.md": "# Audit steps\n\n<img src=x onerror=alert(1)>\n\nDo the audit.", "references/glossary.md": "terms" },
  evals: null,
  rubric: null,
  eval_access: "ok",
};

function row(over: Partial<JournalRow>): JournalRow {
  return {
    dept: "common", skill: "audit-xls", action: "published", version: null, from_version: null, to_version: null,
    versioned_fqn: null, content_hash: null, git_sha: null, eval_run_id: null, actor: "a@x.com",
    job_run_id: null, ts: "2026-10-01T00:00:00Z", ...over,
  };
}

beforeEach(() => {
  vi.mocked(api.skill).mockReset();
  vi.mocked(api.versions).mockReset();
  vi.mocked(api.versions).mockResolvedValue([]);
  vi.mocked(api.me).mockResolvedValue({ user: "a@x.com", groups: [], is_reviewer: false, is_author: false });
  vi.mocked(api.config).mockResolvedValue({ workspace_url: "https://example.test", models: {}, departments: [] });
  vi.mocked(api.skills).mockReset();
  vi.mocked(api.skills).mockResolvedValue([]);
});

function renderAt(fqn: string) {
  return render(
    <MemoryRouter initialEntries={[`/skills/${fqn}`]}>
      <Routes><Route path="/skills/:fqn" element={<SkillDetail />} /></Routes>
    </MemoryRouter>,
  );
}

describe("SkillDetail", () => {
  it("renders SKILL.md as Markdown and lists the files", async () => {
    vi.mocked(api.skill).mockResolvedValue(detail);
    const { container } = renderAt(detail.fqn);
    expect(await screen.findByRole("heading", { name: "Audit steps" })).toBeTruthy();
    expect(container.querySelector("img[src='x']")).toBeNull();
    const files = screen.getByRole("list", { name: /files/i });
    expect(within(files).getByText("references/glossary.md")).toBeTruthy();
    expect(container.querySelector("pre")?.textContent).toBe("terms");
  });

  it("shows a loading status first", async () => {
    let release: (d: SkillDetailT) => void = () => {};
    vi.mocked(api.skill).mockReturnValue(new Promise((resolve) => { release = resolve; }));
    renderAt(detail.fqn);
    expect(screen.getByRole("status")).toHaveTextContent(/loading skill/i);
    release(detail);
    expect(await screen.findByRole("heading", { name: "Audit steps" })).toBeTruthy();
  });

  it("shows an empty state for missing test cases and rubric", async () => {
    vi.mocked(api.skill).mockResolvedValue(detail);
    renderAt(detail.fqn);
    expect(await screen.findByText(/no test cases published/i)).toBeTruthy();
    expect(screen.getByText(/no rubric published/i)).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it.each([
    ["denied", /you can't read eval results for this department/i],
    ["unavailable", /eval results are temporarily unavailable/i],
  ] as const)("explains %s eval access instead of saying nothing is published", async (access, message) => {
    vi.mocked(api.skill).mockResolvedValue({ ...detail, eval_access: access });
    renderAt(detail.fqn);
    expect((await screen.findAllByText(message)).length).toBeGreaterThan(0);
    expect(screen.queryByText(/no test cases published/i)).toBeNull();
    expect(screen.queryByText(/no rubric published/i)).toBeNull();
  });

  it("shows a not-found message for a 404", async () => {
    vi.mocked(api.skill).mockRejectedValue(new ApiError(404, "skill_not_found"));
    renderAt(detail.fqn);
    expect(await screen.findByRole("alert")).toHaveTextContent(/not found, or you do not have access/i);
  });

  it("shows the same not-found message for a 404 with any reason", async () => {
    vi.mocked(api.skill).mockRejectedValue(new ApiError(404, "something_else"));
    renderAt(detail.fqn);
    expect(await screen.findByRole("alert")).toHaveTextContent(/not found, or you do not have access/i);
  });

  it("shows the switch link to reviewers only", async () => {
    vi.mocked(api.skill).mockResolvedValue(detail);
    vi.mocked(api.me).mockResolvedValue({ user: "r@x.com", groups: [], is_reviewer: true, is_author: false });
    renderAt(detail.fqn);
    const link = await screen.findByRole("link", { name: /switch version/i });
    expect(link.getAttribute("href")).toBe("/skills/skill_hub.common.audit-xls/switch");
  });

  it("hides the switch link from non-reviewers", async () => {
    vi.mocked(api.skill).mockResolvedValue(detail);
    renderAt(detail.fqn);
    await screen.findByRole("heading", { name: "Audit steps" });
    expect(screen.queryByRole("link", { name: /switch version/i })).toBeNull();
  });

  it("says whether the rubric has been reviewed", async () => {
    vi.mocked(api.skill).mockResolvedValue({
      ...detail,
      rubric: {
        skill_name: "audit-xls", source_hash: "sha256:x", generated_by: "claude-opus", pass_score: 0.7,
        reviewed: false,
        criteria: [{ id: "c1", text: "Cites sources", type: "task_quality", weight: 3, source: "generated" }],
      },
    });
    renderAt(detail.fqn);
    expect(await screen.findByText(/not yet reviewed/i)).toBeTruthy();
    expect(screen.getByText("Cites sources")).toBeTruthy();
  });

  it("links to the matrix filtered to this skill", async () => {
    vi.mocked(api.skill).mockResolvedValue(detail);
    renderAt(detail.fqn);
    const link = await screen.findByRole("link", { name: /evaluation matrix/i });
    expect(link.getAttribute("href")).toBe("/matrix?skill=audit-xls&dept=common");
  });

  it("shows version history with an eval run column and the live version", async () => {
    vi.mocked(api.skill).mockResolvedValue(detail);
    vi.mocked(api.versions).mockResolvedValue([
      row({ action: "published", version: 1, ts: "2026-10-01T00:00:00Z" }),
      row({ action: "published", version: 2, ts: "2026-10-02T00:00:00Z", eval_run_id: "run-42" }),
      row({ action: "switched", from_version: null, to_version: 1, ts: "2026-10-03T00:00:00Z" }),
    ]);
    renderAt(detail.fqn);
    const table = await screen.findByRole("table", { name: /versions and actions/i });
    expect(within(table).getByRole("columnheader", { name: "Eval run" })).toBeTruthy();
    expect(within(table).getByText("run-42")).toBeTruthy();
    expect(screen.getByText("Live version: 1")).toBeTruthy();
  });

  it("shows an error with retry when only the version history fails", async () => {
    vi.mocked(api.skill).mockResolvedValue(detail);
    vi.mocked(api.versions).mockRejectedValue(new ApiError(500, "boom"));
    renderAt(detail.fqn);
    expect(await screen.findByRole("button", { name: /try again/i })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Audit steps" })).toBeTruthy();
  });
});

describe("SkillDetail summary header", () => {
  it("shows the department, the live version and a verdict chip per model from the skill itself", async () => {
    vi.mocked(api.skill).mockResolvedValue({
      ...detail, live_version: 3, verdicts: { "claude-sonnet": "pass", "gemini-flash": "warn" },
    });
    renderAt(detail.fqn);
    expect(await screen.findByText("Live version 3")).toBeTruthy();
    const chips = screen.getByRole("list", { name: /latest verdicts/i });
    expect(within(chips).getByText("Pass")).toBeTruthy();
    expect(within(chips).getByText("Warn")).toBeTruthy();
    expect(screen.getByText("common", { selector: ".tag" })).toBeTruthy();
    expect(api.skills).not.toHaveBeenCalled(); // no catalog listing just for the header
  });

  it("says when no version is live and there are no results yet", async () => {
    vi.mocked(api.skill).mockResolvedValue(detail);
    renderAt(detail.fqn);
    expect(await screen.findByText("No live version")).toBeTruthy();
    const verdicts = screen.getByRole("region", { name: /latest verdicts on the live version/i });
    expect(within(verdicts).getByText("No results yet")).toBeTruthy();
    expect(within(verdicts).queryByRole("list")).toBeNull();
  });

  it.each([
    ["denied", "You can't read eval results for this department"],
    ["unavailable", "Eval results are temporarily unavailable"],
  ] as const)("explains %s eval access in place of verdicts", async (access, message) => {
    vi.mocked(api.skill).mockResolvedValue({ ...detail, eval_access: access });
    renderAt(detail.fqn);
    const verdicts = await screen.findByRole("region", { name: /latest verdicts on the live version/i });
    expect(within(verdicts).getByText(message)).toBeTruthy();
  });
});

describe("liveVersion", () => {
  it("ignores published rows and clears on retired", () => {
    expect(liveVersion([row({ action: "published", version: 2 })])).toBeNull();
    const sw = row({ action: "switched", to_version: 2, ts: "2026-10-02T00:00:00Z" });
    expect(liveVersion([sw])).toBe(2);
    expect(liveVersion([sw, row({ action: "retired", ts: "2026-10-03T00:00:00Z" })])).toBeNull();
  });

  it("takes the latest switched row, ignoring a later published row", () => {
    expect(liveVersion([
      row({ action: "switched", to_version: 1, ts: "2026-10-01T00:00:00Z" }),
      row({ action: "switched", to_version: 2, ts: "2026-10-02T00:00:00Z" }),
      row({ action: "published", version: 3, ts: "2026-10-03T00:00:00Z" }),
    ])).toBe(2);
  });

  it("keeps the API's time order, even where a string sort of the timestamps would differ", () => {
    // "…00:00:01.5Z" sorts after "…00:00:01Z" by time but before it as a string ("." < "Z").
    expect(liveVersion([
      row({ action: "switched", to_version: 1, ts: "2026-10-01T00:00:01Z" }),
      row({ action: "switched", to_version: 2, ts: "2026-10-01T00:00:01.5Z" }),
    ])).toBe(2);
  });

  it("lets a switch after a retire make a version live again", () => {
    expect(liveVersion([
      row({ action: "switched", to_version: 2, ts: "2026-10-01T00:00:00Z" }),
      row({ action: "retired", ts: "2026-10-02T00:00:00Z" }),
      row({ action: "switched", to_version: 1, ts: "2026-10-03T00:00:00Z" }),
    ])).toBe(1);
  });
});
