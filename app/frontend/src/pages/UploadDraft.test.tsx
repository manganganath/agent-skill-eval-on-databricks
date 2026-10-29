import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import UploadDraft from "./UploadDraft";
import { api, ApiError } from "../api/client";
import type { Me } from "../api/types";

vi.mock("../api/client", async (orig) => {
  const real = await orig<typeof import("../api/client")>();
  return { ...real, api: { ...real.api, me: vi.fn(), config: vi.fn(), uploadDraft: vi.fn() } };
});

const author: Me = { user: "ana@example.com", groups: ["skillhub-authors"], is_reviewer: false, is_author: true };
const reader: Me = { user: "rae@example.com", groups: [], is_reviewer: false, is_author: false };
const KEY = "draft_6720a1b2c3d4e5f6a7b8c9d0";

beforeEach(() => {
  vi.mocked(api.me).mockResolvedValue(author);
  vi.mocked(api.config).mockResolvedValue({
    workspace_url: "https://ws.example.test", models: {}, departments: ["common", "equity_research"],
  });
  vi.mocked(api.uploadDraft).mockReset();
  vi.mocked(api.uploadDraft).mockResolvedValue({ run_key: KEY, dept: "common", name: "new-skill", has_rubric: true });
});

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/drafts/new"]}>
      <Routes>
        <Route path="/drafts/new" element={<UploadDraft />} />
        <Route path="/candidates/:runKey/:dept/:skill" element={<p>draft page</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

const zip = () => new File([new Uint8Array([80, 75, 3, 4])], "new-skill.zip", { type: "application/zip" });

function folderFile(path: string, text: string) {
  const f = new File([text], path.split("/").pop() ?? path);
  Object.defineProperty(f, "webkitRelativePath", { value: path });
  return f;
}

describe("UploadDraft", () => {
  it("tells a reader that only authors and reviewers upload drafts", async () => {
    vi.mocked(api.me).mockResolvedValue(reader);
    renderPage();
    expect(await screen.findByText(/only skill authors and reviewers can upload drafts/i)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /upload draft/i })).toBeNull();
  });

  it("keeps Upload disabled until a department and a zip are chosen", async () => {
    renderPage();
    const button = await screen.findByRole("button", { name: /upload draft/i });
    expect(button).toBeDisabled();
    fireEvent.change(screen.getByRole("combobox", { name: /department/i }), { target: { value: "common" } });
    expect(button).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/zip of the skill folder/i), { target: { files: [zip()] } });
    expect(button).toBeEnabled();
  });

  it("uploads the zip as it is and opens the draft's page", async () => {
    renderPage();
    fireEvent.change(await screen.findByRole("combobox", { name: /department/i }), { target: { value: "common" } });
    fireEvent.change(screen.getByLabelText(/zip of the skill folder/i), { target: { files: [zip()] } });
    fireEvent.click(screen.getByRole("button", { name: /upload draft/i }));
    expect(await screen.findByText("draft page")).toBeTruthy();
    expect(api.uploadDraft).toHaveBeenCalledWith({ dept: "common", archive: "UEsDBA==" });
  });

  it("uploads a picked folder with each file's path", async () => {
    renderPage();
    fireEvent.change(await screen.findByRole("combobox", { name: /department/i }), { target: { value: "common" } });
    fireEvent.click(screen.getByRole("radio", { name: /skill folder/i }));
    const files = [folderFile("new-skill/SKILL.md", "hi"), folderFile("new-skill/evals/evals.json", "{}")];
    fireEvent.change(screen.getByLabelText(/^skill folder$/i), { target: { files } });
    fireEvent.click(screen.getByRole("button", { name: /upload draft/i }));
    await waitFor(() => expect(api.uploadDraft).toHaveBeenCalled());
    expect(api.uploadDraft).toHaveBeenCalledWith({
      dept: "common",
      files: [
        { path: "new-skill/SKILL.md", content: "aGk=" },
        { path: "new-skill/evals/evals.json", content: "e30=" },
      ],
    });
  });

  it("lists every validation error and keeps the form", async () => {
    vi.mocked(api.uploadDraft).mockRejectedValue(
      new ApiError(422, "draft_invalid", ["SKILL.md is missing", "evals/evals.json is missing"]),
    );
    renderPage();
    fireEvent.change(await screen.findByRole("combobox", { name: /department/i }), { target: { value: "common" } });
    fireEvent.change(screen.getByLabelText(/zip of the skill folder/i), { target: { files: [zip()] } });
    fireEvent.click(screen.getByRole("button", { name: /upload draft/i }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The draft was not uploaded");
    expect(alert).toHaveTextContent("SKILL.md is missing");
    expect(alert).toHaveTextContent("evals/evals.json is missing");
    expect(alert).toHaveFocus();
    expect(screen.getByRole("button", { name: /upload draft/i })).toBeEnabled();
  });

  it("explains a refusal without a list, such as a draft that is too large", async () => {
    vi.mocked(api.uploadDraft).mockRejectedValue(new ApiError(413, "draft_too_large"));
    renderPage();
    fireEvent.change(await screen.findByRole("combobox", { name: /department/i }), { target: { value: "common" } });
    fireEvent.change(screen.getByLabelText(/zip of the skill folder/i), { target: { files: [zip()] } });
    fireEvent.click(screen.getByRole("button", { name: /upload draft/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/too large/i);
  });
});
