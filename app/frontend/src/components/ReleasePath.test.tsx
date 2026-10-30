import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { POLL_MS, RESTART_RELEASE, ReleasePath, STILL_CHECKING, releaseDone } from "./ReleasePath";
import { api, ApiError } from "../api/client";
import type { ReleaseStatus } from "../api/types";

vi.mock("../api/client", async (orig) => {
  const real = await orig<typeof import("../api/client")>();
  return { ...real, api: { ...real.api, releaseStatus: vi.fn(), mergePull: vi.fn() } };
});

const URL = "https://github.test/acme/skills/pull/5";
const OPEN: ReleaseStatus = {
  pr_number: 5, dept: "common", skill: "new-skill",
  opened: { state: "open", url: URL },
  gate: { state: "passed", url: `${URL}/checks` },
  merged: { merged: false, sha: null, at: null },
  release: { state: "not_started", url: null },
  live: { fqn: "skill_hub.common.new-skill", version: null, switched_after_merge: false, eval_access: "ok" },
};
const MERGED_WAITING: ReleaseStatus = {
  ...OPEN,
  opened: { state: "closed", url: URL },
  merged: { merged: true, sha: "9f8e7d6c5b4a", at: "2026-10-29T12:00:00Z" },
  release: { state: "waiting_approval", url: "https://github.test/acme/skills/actions/runs/9" },
};
const LIVE: ReleaseStatus = {
  ...MERGED_WAITING,
  release: { state: "succeeded", url: "https://github.test/acme/skills/actions/runs/9" },
  live: { fqn: "skill_hub.common.new-skill", version: 2, switched_after_merge: true, eval_access: "ok" },
};

beforeEach(() => {
  vi.mocked(api.releaseStatus).mockReset();
  vi.mocked(api.mergePull).mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

function renderPath(isReviewer: boolean) {
  return render(
    <MemoryRouter>
      <ReleasePath pr={5} dept="common" skill="new-skill" isReviewer={isReviewer} />
    </MemoryRouter>,
  );
}

describe("ReleasePath", () => {
  it("shows the five steps, each with its state in words and its link", async () => {
    vi.mocked(api.releaseStatus).mockResolvedValue(OPEN);
    renderPath(false);
    const steps = await screen.findAllByRole("listitem");
    expect(steps.map((li) => li.querySelector(".release-step-title")?.textContent)).toEqual([
      "1. Pull request opened", "2. Eval gate", "3. Merged", "4. Release approved", "5. Live",
    ]);
    expect(steps[0]).toHaveTextContent("Open");
    expect(steps[1]).toHaveTextContent("Passed");
    expect(steps[2]).toHaveTextContent("Waiting for a reviewer");
    expect(steps[3]).toHaveTextContent("Not started");
    expect(steps[4]).toHaveTextContent("Not live yet");
    expect(within(steps[0]).getByRole("link", { name: "View the pull request" })).toHaveAttribute("href", URL);
    expect(api.releaseStatus).toHaveBeenCalledWith(5, "common", "new-skill");
  });

  it("links to the waiting release run, so it can be approved in GitHub", async () => {
    vi.mocked(api.releaseStatus).mockResolvedValue(MERGED_WAITING);
    renderPath(false);
    const release = (await screen.findByText("Waiting for approval in GitHub")).closest("li") as HTMLElement;
    expect(within(release).getByRole("link", { name: "Approve in GitHub" })).toHaveAttribute(
      "href", "https://github.test/acme/skills/actions/runs/9",
    );
    expect(screen.getByText("Merged at 9f8e7d6")).toBeTruthy();
  });

  it("names the live version and links to the skill once a switch came after the merge", async () => {
    vi.mocked(api.releaseStatus).mockResolvedValue(LIVE);
    renderPath(false);
    const live = (await screen.findByText("Live as v2")).closest("li") as HTMLElement;
    expect(within(live).getByRole("link", { name: "Open the skill" })).toHaveAttribute(
      "href", "/skills/skill_hub.common.new-skill",
    );
    expect(releaseDone(LIVE)).toBe(true);
  });

  it("shows a reviewer the merge button only when the gate passed and the pull request is open", async () => {
    vi.mocked(api.releaseStatus).mockResolvedValue(OPEN);
    const { unmount } = renderPath(true);
    expect(await screen.findByRole("button", { name: "Merge pull request" })).toBeTruthy();
    unmount();

    vi.mocked(api.releaseStatus).mockResolvedValue({ ...OPEN, gate: { state: "pending", url: `${URL}/checks` } });
    const pending = renderPath(true);
    await screen.findByText("Running");
    expect(screen.queryByRole("button", { name: "Merge pull request" })).toBeNull();
    pending.unmount();

    vi.mocked(api.releaseStatus).mockResolvedValue(OPEN);
    renderPath(false);
    await screen.findByText("Passed");
    expect(screen.queryByRole("button", { name: "Merge pull request" })).toBeNull();
  });

  it("merges only after a confirmation, then reads the release again", async () => {
    vi.mocked(api.releaseStatus).mockResolvedValueOnce(OPEN).mockResolvedValueOnce(MERGED_WAITING);
    vi.mocked(api.mergePull).mockResolvedValue({ pr_number: 5, merged: true, sha: "9f8e7d6c5b4a" });
    renderPath(true);
    fireEvent.click(await screen.findByRole("button", { name: "Merge pull request" }));
    expect(api.mergePull).not.toHaveBeenCalled(); // the first click only asks
    const confirm = screen.getByRole("group", { name: /merge this pull request/i });
    expect(confirm).toHaveTextContent("Then the release waits for approval in the GitHub production environment.");
    fireEvent.click(within(confirm).getByRole("button", { name: "Merge" }));
    expect(await screen.findByText("Waiting for approval in GitHub")).toBeTruthy();
    expect(api.mergePull).toHaveBeenCalledWith(5);
    expect(api.releaseStatus).toHaveBeenCalledTimes(2);
  });

  it("explains a refused merge in words, and keeps the page", async () => {
    vi.mocked(api.releaseStatus).mockResolvedValue(OPEN);
    vi.mocked(api.mergePull).mockRejectedValue(new ApiError(409, "not_mergeable", []));
    renderPath(true);
    fireEvent.click(await screen.findByRole("button", { name: "Merge pull request" }));
    fireEvent.click(screen.getByRole("button", { name: "Merge" }));
    expect(await screen.findByText(/cannot be merged from the Skill Hub yet/i)).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Release path" })).toBeTruthy();
  });

  it("asks the reviewer to try again in a few seconds while GitHub works out whether it can merge", async () => {
    vi.mocked(api.releaseStatus).mockResolvedValue(OPEN);
    vi.mocked(api.mergePull)
      .mockRejectedValueOnce(new ApiError(409, "not_mergeable", [], ["mergeable_unknown"]))
      .mockResolvedValueOnce({ pr_number: 5, merged: true, sha: "9f8e7d6c5b4a" });
    renderPath(true);
    fireEvent.click(await screen.findByRole("button", { name: "Merge pull request" }));
    fireEvent.click(screen.getByRole("button", { name: "Merge" }));
    expect(await screen.findByText(STILL_CHECKING)).toBeTruthy();
    expect(screen.queryByText(/cannot be merged from the Skill Hub yet/i)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Merge" })); // the confirmation stays, to try again
    await act(async () => {});
    expect(api.mergePull).toHaveBeenCalledTimes(2);
  });

  it("keeps the general words when another rule does not hold as well", async () => {
    vi.mocked(api.releaseStatus).mockResolvedValue(OPEN);
    vi.mocked(api.mergePull).mockRejectedValue(
      new ApiError(409, "not_mergeable", [], ["check:platform", "mergeable_unknown"]),
    );
    renderPath(true);
    fireEvent.click(await screen.findByRole("button", { name: "Merge pull request" }));
    fireEvent.click(screen.getByRole("button", { name: "Merge" }));
    expect(await screen.findByText(/cannot be merged from the Skill Hub yet/i)).toBeTruthy();
    expect(screen.queryByText(STILL_CHECKING)).toBeNull();
  });

  it("says a cancelled gate check was cancelled, not that it failed", async () => {
    vi.mocked(api.releaseStatus).mockResolvedValue({ ...OPEN, gate: { state: "cancelled", url: `${URL}/checks` } });
    renderPath(true);
    const gate = (await screen.findByText(/the check was cancelled/i)).closest("li") as HTMLElement;
    expect(gate).toHaveTextContent("Re-run it in GitHub, or push again.");
    expect(gate).not.toHaveTextContent("Failed");
    expect(gate.className).not.toContain("release-step-failed");
    expect(screen.queryByRole("button", { name: "Merge pull request" })).toBeNull();
  });

  it("says a cancelled release was replaced, and how an operator starts it again", async () => {
    vi.mocked(api.releaseStatus).mockResolvedValue({
      ...MERGED_WAITING,
      release: { state: "cancelled", url: "https://github.test/acme/skills/actions/runs/9" },
    });
    renderPath(false);
    const release = (await screen.findByText(/a newer release or deploy replaced this run/i)).closest("li") as HTMLElement;
    expect(within(release).getByText(RESTART_RELEASE).tagName).toBe("CODE");
    expect(RESTART_RELEASE).toBe("gh workflow run skills-publish.yml --ref main");
    expect(release).not.toHaveTextContent("failed");
    expect(within(release).getByRole("link", { name: "Open the run in GitHub" })).toHaveAttribute(
      "href", "https://github.test/acme/skills/actions/runs/9",
    );
  });

  it("says so when GitHub is not set up for the Hub", async () => {
    vi.mocked(api.releaseStatus).mockRejectedValue(new ApiError(503, "github_not_configured", []));
    renderPath(true);
    expect(await screen.findByText(/pull requests are not set up/i)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Merge pull request" })).toBeNull();
  });

  it("reads the release again while a step is pending, and stops once it is done", async () => {
    vi.useFakeTimers();
    vi.mocked(api.releaseStatus).mockResolvedValueOnce(MERGED_WAITING).mockResolvedValue(LIVE);
    renderPath(false);
    await act(async () => {});
    expect(api.releaseStatus).toHaveBeenCalledTimes(1);
    await act(async () => { vi.advanceTimersByTime(POLL_MS); });
    expect(api.releaseStatus).toHaveBeenCalledTimes(2);
    await act(async () => { vi.advanceTimersByTime(POLL_MS); });
    expect(api.releaseStatus).toHaveBeenCalledTimes(2); // live: nothing is pending any more
  });
});
