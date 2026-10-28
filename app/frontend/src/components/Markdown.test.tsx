import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Markdown } from "./Markdown";

describe("Markdown", () => {
  it("does not execute HTML in SKILL.md", () => {
    const { container } = render(<Markdown source={'# Title\n\n<script>window.__pwned = 1</script>\n\nbody'} />);
    expect(container.querySelector("script")).toBeNull();
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
    expect(container.querySelector("h1")?.textContent).toBe("Title");
  });

  it("does not render remote images", () => {
    const { container } = render(<Markdown source={"![x](https://tracker.example.test/p.png)"} />);
    expect(container.querySelector("img")).toBeNull();
  });

  it("opens skill text links in a new tab with no opener or referrer", () => {
    const { container } = render(<Markdown source={"[docs](https://docs.example.test/x)"} />);
    const a = container.querySelector("a");
    expect(a?.getAttribute("href")).toBe("https://docs.example.test/x");
    expect(a?.getAttribute("target")).toBe("_blank");
    expect(a?.getAttribute("rel")).toBe("noopener noreferrer nofollow");
    expect(a?.textContent).toBe("docs");
  });

  it("does not keep a javascript: link target", () => {
    const { container } = render(<Markdown source={"[x](javascript:alert(1))"} />);
    const hrefs = [...container.querySelectorAll("a")].map((a) => a.getAttribute("href") ?? "");
    expect(hrefs.some((h) => h.toLowerCase().startsWith("javascript:"))).toBe(false);
  });
});
