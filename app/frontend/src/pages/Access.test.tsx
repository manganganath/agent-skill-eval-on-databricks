import { render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Access from "./Access";
import { api, ApiError } from "../api/client";
import type { Me } from "../api/types";

vi.mock("../api/client", async (orig) => {
  const real = await orig<typeof import("../api/client")>();
  return { ...real, api: { ...real.api, me: vi.fn(), config: vi.fn(), readableDepartments: vi.fn() } };
});

const reader: Me = { user: "rae@example.com", groups: [], is_reviewer: false, is_author: false };
const author: Me = { user: "ana@example.com", groups: ["skillhub-authors"], is_reviewer: false, is_author: true };

beforeEach(() => {
  vi.mocked(api.config).mockResolvedValue({
    workspace_url: "https://ws.example.test", models: {}, departments: ["common", "equity_research", "external"],
    roles: { authors: "skillhub-authors", reviewers: "skillhub-reviewers" },
    department_access: {
      common: { readers: ["skillhub-users"], quarantine: false },
      equity_research: { readers: ["dept-equity-research"], quarantine: false },
      external: { readers: ["skillhub-reviewers"], quarantine: true },
    },
  });
  vi.mocked(api.readableDepartments).mockResolvedValue({ departments: ["common"] });
});

const open = () => render(<MemoryRouter><Access /></MemoryRouter>);

describe("Access", () => {
  it("shows a reader their role, departments and the groups to ask for", async () => {
    vi.mocked(api.me).mockResolvedValue(reader);
    open();
    const mine = await screen.findByRole("list", { name: "Departments you can read" });
    expect(within(mine).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["common"]);
    expect(screen.queryByText("Author", { selector: ".badge" })).toBeNull();
    expect(screen.getByText("skillhub-authors")).toBeTruthy();
    expect(screen.getByText("dept-equity-research")).toBeTruthy();
    expect(screen.getByText(/ask a workspace admin/i)).toBeTruthy();
  });

  it("marks the author's columns in the table", async () => {
    vi.mocked(api.me).mockResolvedValue(author);
    open();
    const table = await screen.findByRole("table", { name: "Who can do what" });
    const heads = within(table).getAllByRole("columnheader").map((h) => h.textContent);
    expect(heads).toEqual(["Action", "Reader You", "Author You", "Reviewer"]);
    expect(within(table).getByRole("row", { name: /upload a draft/i }).textContent).toContain("Yes");
  });

  it("still shows the page when the department check fails", async () => {
    vi.mocked(api.me).mockResolvedValue(reader);
    vi.mocked(api.readableDepartments).mockRejectedValue(new ApiError(403, "authz_check_failed"));
    open();
    await waitFor(() => expect(screen.getByRole("heading", { name: "Changing a skill" })).toBeTruthy());
    expect(screen.getByRole("heading", { name: "Who can do what" })).toBeTruthy();
  });
});
