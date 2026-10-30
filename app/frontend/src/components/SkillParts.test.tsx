import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { RubricSection } from "./SkillParts";

describe("RubricSection", () => {
  it("explains what the rubric is for and how an answer passes", () => {
    render(
      <RubricSection access="ok" note="Read only." empty="No rubric published" rubric={{
        skill_name: "s", source_hash: "sha256:x", generated_by: "human", pass_score: 0.6, reviewed: true,
        criteria: [{ id: "c1", text: "Gives a table", type: "task_quality", weight: 3, source: "Output" }],
      }} />,
    );
    expect(screen.getByText(/criteria the judges use to score each answer in effectiveness mode/i)).toBeTruthy();
    expect(screen.getByText(/reaches 60% of the total/)).toBeTruthy();
    expect(screen.getByText(/Trigger mode does not use the rubric/)).toBeTruthy();
  });
});
