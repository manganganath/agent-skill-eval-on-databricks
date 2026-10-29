import { describe, expect, it, vi, afterEach } from "vitest";
import { api, ApiError } from "./client";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("api client", () => {
  it("maps a 403 reason from the error body", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: { reason: "skill_not_readable" } }), { status: 403 }),
    );
    const err = await api.skill("skill_hub.common.a").catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(403);
    expect(err.reason).toBe("skill_not_readable");
  });

  it("falls back to http_<status> when the body is not JSON", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("oops", { status: 502 }));
    const err = await api.me().catch((e) => e);
    expect(err.reason).toBe("http_502");
  });

  it("encodes the FQN in the path", async () => {
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({}), { status: 200 }),
    );
    await api.skill("skill_hub.common.a b");
    expect(spy.mock.calls[0][0]).toBe("/api/skills/skill_hub.common.a%20b");
  });

  it("wraps an unparseable OK response in ApiError bad_response", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("<html>", { status: 200 }));
    const err = await api.me().catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.reason).toBe("bad_response");
  });

  it("reads the denied-departments and truncation headers for matrix and cases", async () => {
    const headers = { "X-Eval-Denied-Departments": "equity_research,external", "X-Result-Truncated": "true" };
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response("[]", { status: 200, headers }));
    for (const call of [() => api.matrix({}), () => api.cases("r1")]) {
      expect(await call()).toEqual({ rows: [], deniedDepartments: ["equity_research", "external"], truncated: true });
    }
  });

  it("shares one /me request between callers, and asks again after a failure", async () => {
    vi.resetModules(); // a fresh client, so no earlier test's answer is kept
    const fresh = (await import("./client")).api;
    const fetchMe = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response("oops", { status: 502 }))
      .mockImplementation(async () =>
        new Response(JSON.stringify({ user: "ana@example.com", groups: [], is_reviewer: false, is_author: false }), { status: 200 }));
    const [a, b] = await Promise.allSettled([fresh.me(), fresh.me()]);
    expect(a.status).toBe("rejected");
    expect(b.status).toBe("rejected");
    expect(fetchMe).toHaveBeenCalledTimes(1);
    const [c, d] = await Promise.all([fresh.me(), fresh.me()]);
    expect(c.user).toBe("ana@example.com");
    expect(d).toBe(c);
    expect(await fresh.me()).toBe(c);
    expect(fetchMe).toHaveBeenCalledTimes(2);
    expect(fetchMe.mock.calls.every(([url]) => url === "/api/me")).toBe(true);
  });

  it("reports no denied departments when the header is absent", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("[]", { status: 200 }));
    expect(await api.matrix({})).toEqual({ rows: [], deniedDepartments: [], truncated: false });
  });

  it("reads the dashboard body and the denied departments from the header", async () => {
    const body = { summary: { skills: 0 }, verdicts: [], tokens: null };
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(body), { status: 200, headers: { "X-Eval-Denied-Departments": "external, equity_research" } }),
    );
    const d = await api.dashboard();
    expect(d.deniedDepartments).toEqual(["external", "equity_research"]);
    expect(d.tokens).toBeNull();
  });
});
