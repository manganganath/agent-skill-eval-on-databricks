import { act, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Releases, { GITHUB_NOT_SET_UP, scanText, waitedFor } from "./Releases";
import { POLL_MS } from "../components/ReleasePath";
import { api, ApiError } from "../api/client";
import type { Me, PendingRelease } from "../api/types";

vi.mock("../api/client", async (orig) => {
  const real = await orig<typeof import("../api/client")>();
  return { ...real, api: { ...real.api, me: vi.fn(), pendingReleases: vi.fn() } };
});

const reviewer: Me = { user: "rita@example.com", groups: ["skillhub-reviewers"], is_reviewer: true, is_author: false };
const author: Me = { user: "ana@example.com", groups: ["skillhub-authors"], is_reviewer: false, is_author: true };
const RUN = "https://github.test/acme/skills/actions/runs/9";

const WAITING: PendingRelease = {
  run_url: RUN, run_number: 9, sha: "M5abcdef0", started_at: "2026-10-29T12:01:00Z",
  waiting_since: "2026-10-29T13:30:00Z",
  pull: { number: 5, title: "Add new-skill (common) from the Skill Hub", author: "ana", merged_by: "rita",
    url: "https://github.test/acme/skills/pull/5" },
  details: "ok", scan: "passed", skills_source: "release", eval_basis: "release", earlier_changes: false,
  run_key: "pr5_c7ecd3e_a1",
  skills: [
    { dept: "common", skill: "new-skill", action: "publish", verdicts: { "claude-sonnet": "pass", "claude-haiku": "warn" }, eval_access: "ok",
      eval_source: "pull_request" },
    { dept: "equity_research", skill: "earnings-note", action: "publish", verdicts: { "claude-sonnet": "fail" }, eval_access: "ok",
      eval_source: "merge" },
    { dept: "common", skill: "old-skill", action: "publish", verdicts: {}, eval_access: "denied", eval_source: null },
  ],
};
const DIRECT_PUSH: PendingRelease = {
  ...WAITING, run_url: "https://github.test/acme/skills/actions/runs/10", run_number: 10, sha: "D1aaaaaaa",
  pull: null, run_key: null, skills: [],
};

beforeEach(() => {
  vi.mocked(api.me).mockReset();
  vi.mocked(api.pendingReleases).mockReset();
  vi.mocked(api.me).mockResolvedValue(reviewer);
});

afterEach(() => {
  vi.useRealTimers();
});

const open = () => render(<MemoryRouter><Releases /></MemoryRouter>);

describe("Releases", () => {
  it("lists a waiting release with its pull request, skills, verdicts, scan and the way to GitHub", async () => {
    vi.mocked(api.pendingReleases).mockResolvedValue([WAITING]);
    open();
    expect(await screen.findByRole("heading", { name: "Pull request #5: Add new-skill (common) from the Skill Hub" })).toBeTruthy();
    expect(screen.getByText(/Opened by ana\. Merged by rita\./)).toBeTruthy();
    expect(screen.getByRole("link", { name: "View the pull request" })).toHaveAttribute("href", "https://github.test/acme/skills/pull/5");
    const review = screen.getByRole("link", { name: /Review in GitHub/ });
    expect(review).toHaveAttribute("href", RUN);
    expect(review).toHaveAttribute("target", "_blank");
    expect(screen.getByText(/Approve or reject there with Review deployments/)).toBeTruthy();

    const rows = within(screen.getByRole("table")).getAllByRole("row").slice(1);
    const first = within(rows[0]);
    expect(first.getByRole("link", { name: "common/new-skill" })).toHaveAttribute("href", "/candidates/pr5_c7ecd3e_a1/common/new-skill");
    const chips = within(first.getByRole("list", { name: "Verdicts of new-skill" }));
    expect(chips.getByText("claude-sonnet").parentElement).toHaveTextContent("Pass");
    expect(chips.getByText("claude-haiku").parentElement).toHaveTextContent("Warn");
    expect(first.getByText("Pull request eval, reused by the release")).toBeTruthy();
    const second = within(rows[1]);
    expect(second.getByText("fail", { exact: false })).toBeTruthy();
    expect(second.getByText("Merge eval: the merged content was evaluated again")).toBeTruthy();
    const third = within(rows[2]);
    expect(third.getByText("You can't read eval results for this department")).toBeTruthy();
    expect(third.getByText("No eval found")).toBeTruthy();
    // The scan is shown once for the release, with where the findings are.
    expect(screen.getAllByText(/Security scan\./)).toHaveLength(1);
    expect(screen.getByText(/No critical findings\. Any high findings are listed in the run summary in GitHub/)).toBeTruthy();
  });

  it("does not link a skill to a candidate page when the pull request's upload is gone", async () => {
    vi.mocked(api.pendingReleases).mockResolvedValue([{ ...WAITING, run_key: null }]);
    open();
    await screen.findByRole("table");
    expect(within(screen.getByRole("table")).queryByRole("link")).toBeNull();
    expect(screen.getByText("common/new-skill")).toBeTruthy();
  });

  it("never offers to approve or reject in the Hub", async () => {
    vi.mocked(api.pendingReleases).mockResolvedValue([WAITING]);
    open();
    await screen.findByRole("link", { name: /Review in GitHub/ });
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByText(/never approves or rejects a release/)).toBeTruthy();
  });

  it("lists a release whose commit has no pull request, with what is known", async () => {
    vi.mocked(api.pendingReleases).mockResolvedValue([DIRECT_PUSH]);
    open();
    expect(await screen.findByRole("heading", { name: "Commit D1aaaaa" })).toBeTruthy();
    expect(screen.getByText("No pull request: this commit was pushed to main directly.")).toBeTruthy();
    expect(screen.getByRole("link", { name: /Review in GitHub/ })).toHaveAttribute("href", DIRECT_PUSH.run_url);
  });

  it("says when GitHub could not be read for one run, and still lists the others", async () => {
    vi.mocked(api.pendingReleases).mockResolvedValue([{ ...DIRECT_PUSH, details: "unavailable" }, WAITING]);
    open();
    expect(await screen.findByText(/GitHub could not be read for this release/)).toBeTruthy();
    expect(screen.queryByText(/pushed to main directly/)).toBeNull();
    expect(screen.getAllByRole("link", { name: /Review in GitHub/ })).toHaveLength(2);
    expect(screen.getByRole("heading", { name: /Pull request #5/ })).toBeTruthy();
  });

  it("shows an empty state when nothing is waiting", async () => {
    vi.mocked(api.pendingReleases).mockResolvedValue([]);
    open();
    expect(await screen.findByRole("heading", { name: "No release is waiting for approval" })).toBeTruthy();
  });

  it("says GitHub is not set up on 503 github_not_configured", async () => {
    vi.mocked(api.pendingReleases).mockRejectedValue(new ApiError(503, "github_not_configured"));
    open();
    expect(await screen.findByText(GITHUB_NOT_SET_UP)).toBeTruthy();
    expect(screen.getByText("github_not_configured")).toBeTruthy();
  });

  it("shows a failed read with a retry", async () => {
    vi.mocked(api.pendingReleases).mockRejectedValueOnce(new ApiError(502, "github_failed"));
    vi.mocked(api.pendingReleases).mockResolvedValue([]);
    open();
    expect(await screen.findByText(/could not reach GitHub/)).toBeTruthy();
    await act(async () => { screen.getByRole("button", { name: "Try again" }).click(); });
    expect(await screen.findByRole("heading", { name: "No release is waiting for approval" })).toBeTruthy();
  });

  it("does not ask the API for anyone but a reviewer", async () => {
    vi.mocked(api.me).mockResolvedValue(author);
    open();
    expect(await screen.findByText(/Only skill reviewers can see the releases/)).toBeTruthy();
    expect(api.pendingReleases).not.toHaveBeenCalled();
  });

  it("shows a retirement as a retirement, not as a missing eval", async () => {
    const retiring = { dept: "common", skill: "gone", action: "retire", verdicts: {}, eval_access: "ok", eval_source: null } as const;
    vi.mocked(api.pendingReleases).mockResolvedValue([{ ...WAITING, skills: [retiring] }]);
    open();
    const row = within(await screen.findByRole("table")).getAllByRole("row")[1];
    expect(within(row).getByText(/Retired: the skill is removed/)).toBeTruthy();
    expect(within(row).queryByText("No eval found")).toBeNull();
    expect(within(row).queryByText("No results")).toBeNull();
  });

  it("says when the release carries merges before the shown pull request", async () => {
    vi.mocked(api.pendingReleases).mockResolvedValue([{ ...WAITING, earlier_changes: true }]);
    open();
    expect(await screen.findByText(/also carries changes from earlier merges/)).toBeTruthy();
  });

  it("says nothing about provenance when the release's own plan and eval are shown", async () => {
    vi.mocked(api.pendingReleases).mockResolvedValue([WAITING]);
    open();
    await screen.findByRole("table");
    expect(screen.queryByText(/predates the release plan/)).toBeNull();
    expect(screen.queryByText(/could not be confirmed/)).toBeNull();
  });

  it.each([
    ["pull_request", /from the merged pull request; this run predates the release plan/],
    ["unknown", /release plan could not be read or trusted/],
  ] as const)("marks skills from the pull request when the plan is %s", async (skills_source, text) => {
    vi.mocked(api.pendingReleases).mockResolvedValue([{ ...WAITING, skills_source }]);
    open();
    expect(await screen.findByText(text)).toBeTruthy();
  });

  it.each([
    ["inferred", /eval shown is inferred/],
    ["unknown", /eval could not be confirmed/],
  ] as const)("marks an eval that is %s", async (eval_basis, text) => {
    vi.mocked(api.pendingReleases).mockResolvedValue([{ ...WAITING, eval_basis }]);
    open();
    expect(await screen.findByText(text)).toBeTruthy();
  });

  it("reads again on the cadence of the release path", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.mocked(api.pendingReleases).mockResolvedValueOnce([WAITING]).mockResolvedValue([]);
    open();
    await screen.findByRole("heading", { name: /Pull request #5/ });
    expect(api.pendingReleases).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_MS); });
    expect(api.pendingReleases).toHaveBeenCalledTimes(2);
    expect(await screen.findByRole("heading", { name: "No release is waiting for approval" })).toBeTruthy();
  });

  describe("while the tab is hidden", () => {
    const setVisibility = (state: "hidden" | "visible") => {
      Object.defineProperty(document, "visibilityState", { configurable: true, get: () => state });
      act(() => { document.dispatchEvent(new Event("visibilitychange")); });
    };
    afterEach(() => { setVisibility("visible"); });

    it("stops reading, and reads once when the tab shows again", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      vi.mocked(api.pendingReleases).mockImplementation(async () => [WAITING]); // a new list per read, as JSON gives
      open();
      await screen.findByRole("heading", { name: /Pull request #5/ });
      expect(api.pendingReleases).toHaveBeenCalledTimes(1);
      setVisibility("hidden");
      await act(async () => { await vi.advanceTimersByTimeAsync(POLL_MS * 4); });
      expect(api.pendingReleases).toHaveBeenCalledTimes(1);
      setVisibility("visible");
      await act(async () => { await vi.advanceTimersByTimeAsync(10); });
      expect(api.pendingReleases).toHaveBeenCalledTimes(2);
      await act(async () => { await vi.advanceTimersByTimeAsync(POLL_MS); });
      expect(api.pendingReleases).toHaveBeenCalledTimes(3); // polling goes on
    });

    it("reads once when it opens in a hidden tab, and then waits", async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      setVisibility("hidden");
      vi.mocked(api.pendingReleases).mockResolvedValue([]);
      open();
      await screen.findByRole("heading", { name: "No release is waiting for approval" });
      await act(async () => { await vi.advanceTimersByTimeAsync(POLL_MS * 2); });
      expect(api.pendingReleases).toHaveBeenCalledTimes(1);
    });
  });
});

describe("waitedFor", () => {
  const since = "2026-10-29T13:30:00Z";
  const at = (minutes: number) => Date.parse(since) + minutes * 60_000;
  it.each([
    [0, "under a minute"], [5, "5 min"], [60, "1 h 0 min"], [192, "3 h 12 min"], [1440 + 4 * 60, "1 d 4 h"],
  ])("after %i minutes it says %s", (minutes, text) => {
    expect(waitedFor(since, at(minutes))).toBe(text);
  });
  it("says unknown without a time", () => {
    expect(waitedFor(null, Date.now())).toBe("unknown");
  });
});

describe("scanText", () => {
  it.each([
    ["passed", /No critical findings/], ["failed", /critical findings, or the scan did not run/],
    ["not_run", /skipped/], ["unknown", /not known yet/],
  ] as const)("says in words what %s means and points to the run summary", (state, text) => {
    expect(scanText(state)).toMatch(text);
    if (state !== "not_run") expect(scanText(state)).toMatch(/run summary in GitHub/);
  });
});
