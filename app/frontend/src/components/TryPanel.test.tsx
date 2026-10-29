import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { TryPanel } from "./TryPanel";
import { api, ApiError } from "../api/client";

vi.mock("../api/client", async (orig) => {
  const real = await orig<typeof import("../api/client")>();
  return { ...real, api: { ...real.api, config: vi.fn(), tryPrompt: vi.fn(), tryCandidate: vi.fn() } };
});

const RESULT = {
  model: "claude-sonnet",
  with_skill: { text: "<b>bold?</b> no, plain text", invoked: true },
  without_skill: { text: "plain" },
  trace_url: "https://ws.example.test/ml/experiments/e/traces?selectedEvaluationId=t",
};

beforeEach(() => {
  vi.mocked(api.config).mockResolvedValue({
    models: { "claude-sonnet": "frontier", "gemini-flash": "fast", "m3": "fast", "m4": "fast" },
    departments: ["common"], workspace_url: "https://ws.example.test",
  });
  vi.mocked(api.tryPrompt).mockReset();
  vi.mocked(api.tryPrompt).mockResolvedValue([RESULT]);
  vi.mocked(api.tryCandidate).mockReset();
  vi.mocked(api.tryCandidate).mockResolvedValue([RESULT]);
});

async function fill(model = "claude-sonnet") {
  fireEvent.change(await screen.findByLabelText(/test input/i), { target: { value: "audit this" } });
  fireEvent.click(await screen.findByRole("checkbox", { name: model }));
}

describe("TryPanel", () => {
  it("shows answers as plain text with the invoked flag and a trace link", async () => {
    const { container } = render(<TryPanel target={{ fqn: "skill_hub.common.audit-xls" }} evalAccess="ok" />);
    await fill();
    fireEvent.click(screen.getByRole("button", { name: /try skill/i }));
    expect(await screen.findByText(/skill invoked: yes/i)).toBeTruthy();
    expect(screen.getByText(/<b>bold\?<\/b> no, plain text/)).toBeTruthy();
    expect(container.querySelector("b")).toBeNull();
    expect(screen.getByRole("link", { name: /open trace/i }).getAttribute("href")).toBe(RESULT.trace_url);
    expect(api.tryPrompt).toHaveBeenCalledWith({
      fqn: "skill_hub.common.audit-xls", prompt: "audit this", models: ["claude-sonnet"],
    });
  });

  it("sends once on a double click and announces progress in a status region", async () => {
    let resolve!: (v: unknown) => void;
    vi.mocked(api.tryPrompt).mockReturnValue(new Promise((r) => { resolve = r; }) as never);
    render(<TryPanel target={{ fqn: "skill_hub.common.audit-xls" }} evalAccess="ok" />);
    await fill();
    const button = screen.getByRole("button", { name: /try skill/i });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(api.tryPrompt).toHaveBeenCalledTimes(1);
    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByRole("status").textContent).toMatch(/running/i);
    resolve([RESULT]);
    await screen.findByText(/skill invoked/i);
    expect((button as HTMLButtonElement).disabled).toBe(false);
  });

  it("limits the choice to three models", async () => {
    render(<TryPanel target={{ fqn: "skill_hub.common.audit-xls" }} evalAccess="ok" />);
    for (const m of ["claude-sonnet", "gemini-flash", "m3"]) {
      fireEvent.click(await screen.findByRole("checkbox", { name: m }));
    }
    expect((screen.getByRole("checkbox", { name: "m4" }) as HTMLInputElement).disabled).toBe(true);
  });

  it("shows a reason code when the run fails", async () => {
    vi.mocked(api.tryPrompt).mockRejectedValue(new ApiError(502, "try_failed"));
    render(<TryPanel target={{ fqn: "skill_hub.common.audit-xls" }} evalAccess="ok" />);
    await fill();
    fireEvent.click(screen.getByRole("button", { name: /try skill/i }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toMatch(/try_failed/));
  });

  it("tells the user who can see Try runs", async () => {
    render(<TryPanel target={{ fqn: "skill_hub.common.audit-xls" }} evalAccess="ok" />);
    expect(await screen.findByText(/visible to this department's eval readers/i)).toBeTruthy();
  });

  it("hides the trace link when the user cannot read evals", async () => {
    render(<TryPanel target={{ fqn: "skill_hub.common.audit-xls" }} evalAccess="denied" />);
    await fill();
    fireEvent.click(screen.getByRole("button", { name: /try skill/i }));
    await screen.findByText(/skill invoked: yes/i);
    expect(screen.queryByRole("link", { name: /open trace/i })).toBeNull();
  });
});

describe("TryPanel on a skill under review", () => {
  const target = { runKey: "pr5_c7ecd3e_a1", dept: "common", skill: "new-skill" };

  it("says the upload stands in for the live version and that nobody else can load it", async () => {
    render(<TryPanel target={target} evalAccess="ok" />);
    expect(await screen.findByRole("heading", { name: "Try this skill" })).toBeTruthy();
    expect(screen.getByText(/sees this upload in place of the live version/i)).toBeTruthy();
    expect(screen.getByText(/nobody else can load it/i)).toBeTruthy();
  });

  it("runs the candidate through its own path, not the live one", async () => {
    render(<TryPanel target={target} evalAccess="ok" />);
    await fill();
    fireEvent.click(screen.getByRole("button", { name: /try skill/i }));
    expect(await screen.findByText(/skill invoked: yes/i)).toBeTruthy();
    expect(api.tryCandidate).toHaveBeenCalledWith("pr5_c7ecd3e_a1", "common", "new-skill", {
      prompt: "audit this", models: ["claude-sonnet"],
    });
    expect(api.tryPrompt).not.toHaveBeenCalled();
  });

  it("shows the one-time setup message when the app cannot stage a candidate yet", async () => {
    vi.mocked(api.tryCandidate).mockRejectedValue(new ApiError(503, "try_setup_required"));
    render(<TryPanel target={target} evalAccess="ok" />);
    await fill();
    fireEvent.click(screen.getByRole("button", { name: /try skill/i }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toMatch(/CREATE SCHEMA/));
    expect(screen.getByRole("alert").textContent).toMatch(/try_setup_required/);
  });
});
