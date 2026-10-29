import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ApiError } from "../api/client";
import { ErrorState } from "./ErrorState";

describe("ErrorState", () => {
  it("explains a refused skill in plain words and announces it", () => {
    render(<ErrorState error={new ApiError(403, "skill_not_readable")} />);
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toMatch(/do not have access/i);
  });

  it("shows a retry button when onRetry is given", () => {
    render(<ErrorState error={new Error("network")} onRetry={() => {}} />);
    expect(screen.getByRole("button", { name: /try again/i })).toBeTruthy();
  });

  it("says the job is not set up when the eval or switch Job is missing", () => {
    render(<ErrorState error={new ApiError(404, "job_not_found")} />);
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toMatch(/not set up/i);
    expect(alert.textContent).not.toMatch(/something went wrong/i);
  });

  it("does not blame page loading for an unknown reason, since actions use it too", () => {
    render(<ErrorState error={new ApiError(500, "http_500")} />);
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toMatch(/something went wrong/i);
    expect(alert.textContent).not.toMatch(/loading this page/i);
  });
});
