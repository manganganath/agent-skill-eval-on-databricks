import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { VerdictChip } from "./VerdictChip";

describe("VerdictChip", () => {
  it("names the verdict in words, with a decorative icon and its meaning on hover", () => {
    render(<VerdictChip verdict="advisory" />);
    const chip = screen.getByText("Advisory").closest(".chip") as HTMLElement;
    expect(chip.getAttribute("title")).toMatch(/^Advisory: Too few reviewed test cases.*never blocks a release\.$/);
    const icon = chip.querySelector("svg");
    expect(icon?.getAttribute("aria-hidden")).toBe("true");
    expect(chip.textContent).toBe("Advisory"); // no glyph letters in the text
  });

  it("shows No result without a verdict", () => {
    render(<VerdictChip verdict={undefined} />);
    expect(screen.getByText("No result")).toBeTruthy();
  });
});
