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
});
