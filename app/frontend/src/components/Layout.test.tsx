import { act, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Layout } from "./Layout";
import { api, ApiError } from "../api/client";

vi.mock("../api/client", async (orig) => {
  const real = await orig<typeof import("../api/client")>();
  return { ...real, api: { ...real.api, me: vi.fn() } };
});

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route element={<Layout />}>
          <Route path="*" element={<p>page</p>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.mocked(api.me).mockReset();
  vi.mocked(api.me).mockResolvedValue({ user: "ana@example.com", groups: [], is_reviewer: false, is_author: false });
});

describe("Layout", () => {
  it("keeps the skip link, the labelled main navigation and the main focus target", async () => {
    renderAt("/");
    expect(screen.getByRole("link", { name: /skip to content/i }).getAttribute("href")).toBe("#main");
    const main = screen.getByRole("main");
    expect(main).toHaveAttribute("id", "main");
    expect(main).toHaveAttribute("tabindex", "-1");
    expect(screen.getByRole("navigation", { name: "Main" })).toBeTruthy();
    await screen.findByText("ana@example.com");
  });

  it("shows the signed-in user without a reviewer badge to a non-reviewer", async () => {
    renderAt("/");
    expect(await screen.findByText("ana@example.com")).toBeTruthy();
    expect(screen.getByText(/signed in as/i)).toBeTruthy();
    expect(screen.queryByText("Reviewer")).toBeNull();
  });

  it("shows a Reviewer badge to a reviewer", async () => {
    vi.mocked(api.me).mockResolvedValue({ user: "rita@example.com", groups: ["reviewers"], is_reviewer: true, is_author: false });
    renderAt("/");
    expect(await screen.findByText("rita@example.com")).toBeTruthy();
    expect(screen.getByText("Reviewer")).toBeTruthy();
  });

  it("shows no user when the lookup fails", async () => {
    vi.mocked(api.me).mockRejectedValue(new ApiError(401, "no_user_token"));
    renderAt("/");
    await act(async () => {}); // let the refused lookup settle
    expect(api.me).toHaveBeenCalled();
    expect(screen.queryByText(/signed in as/i)).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it.each([
    ["/", "Catalog"],
    ["/skills/skill_hub.common.audit-xls", "Catalog"],
    ["/matrix", "Eval matrix"],
    ["/run", "Run eval"],
  ])("marks the active section for %s", async (path, name) => {
    renderAt(path);
    const nav = screen.getByRole("navigation", { name: "Main" });
    for (const link of nav.querySelectorAll("a")) {
      expect(link.classList.contains("active")).toBe(link.textContent === name);
    }
    await screen.findByText("ana@example.com");
  });
});
