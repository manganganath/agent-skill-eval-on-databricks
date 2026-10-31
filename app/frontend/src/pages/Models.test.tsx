import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Models from "./Models";
import { api, ApiError } from "../api/client";
import type { CatalogModel, Me, ModelsList } from "../api/types";

vi.mock("../api/client", async (orig) => {
  const real = await orig<typeof import("../api/client")>();
  return {
    ...real,
    api: {
      ...real.api, me: vi.fn(), models: vi.fn(), addModel: vi.fn(), updateModel: vi.fn(), testModel: vi.fn(),
      modelTest: vi.fn(), servingEndpoints: vi.fn(),
    },
  };
});

const reader: Me = { user: "rae@example.com", groups: [], is_reviewer: false, is_author: false };
const reviewer: Me = { user: "rita@example.com", groups: ["skillhub-reviewers"], is_reviewer: true, is_author: false };
const row = (over: Partial<CatalogModel>): CatalogModel => ({
  name: "x", kind: "ug_model", route: "serving", endpoint: null, model_id: null, region: null, model_class: "fast",
  harness: "reference", enabled: false, release_gate: false, price_in: null, price_out: null, tested_at: null,
  test: null, updated_by: null, updated_at: null, ...over,
});
const GPT = row({ name: "gpt", kind: "hub_yaml", endpoint: "databricks-gpt-5-5", model_class: "frontier", enabled: true, release_gate: true });
const LLAMA = row({ name: "llama", endpoint: "databricks-llama-4-maverick" });
const NOVA = row({ name: "nova-lite", kind: "external_model", route: "bedrock", model_id: "amazon.nova-lite-v1:0", region: "us-east-1" });
const list = (models: CatalogModel[], catalog_available = true): ModelsList =>
  ({ models, catalog_available, bedrock_region: "us-east-1" });

beforeEach(() => {
  vi.mocked(api.me).mockReset().mockResolvedValue(reviewer);
  vi.mocked(api.models).mockReset().mockResolvedValue(list([GPT, LLAMA, NOVA]));
  vi.mocked(api.addModel).mockReset().mockResolvedValue(NOVA);
  vi.mocked(api.updateModel).mockReset().mockResolvedValue(NOVA);
  vi.mocked(api.testModel).mockReset().mockResolvedValue({ job_run_id: 4242 });
  vi.mocked(api.modelTest).mockReset();
  vi.mocked(api.servingEndpoints).mockReset().mockResolvedValue([{ name: "databricks-llama-4-maverick", task: "llm/v1/chat" }]);
});

const open = () => render(<MemoryRouter><Models /></MemoryRouter>);

describe("Models", () => {
  it("shows a reader the models grouped by kind, with no actions", async () => {
    vi.mocked(api.me).mockResolvedValue(reader);
    open();
    const gate = await screen.findByRole("table", { name: "Release gate" });
    expect(within(gate).getByText("gpt")).toBeTruthy();
    expect(screen.getByRole("table", { name: "On Unity Gateway" })).toBeTruthy();
    const external = screen.getByRole("table", { name: "External models" });
    expect(within(external).getByText("amazon.nova-lite-v1:0 (us-east-1)")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /add a model/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /test nova-lite/i })).toBeNull();
    expect(screen.getByRole("link", { name: /who can do what/i })).toBeTruthy();
  });

  it("gives a reviewer no actions on a release gate model", async () => {
    open();
    const gate = await screen.findByRole("table", { name: "Release gate" });
    expect(within(gate).getByText("Change it in hub.yaml")).toBeTruthy();
    expect(within(gate).queryByRole("button")).toBeNull();
  });

  it("adds a Bedrock model with the default region", async () => {
    open();
    fireEvent.click(await screen.findByRole("button", { name: /add a model/i }));
    fireEvent.click(screen.getByRole("radio", { name: /external model/i }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "nova-lite" } });
    fireEvent.change(screen.getByLabelText("Bedrock model ID"), { target: { value: "amazon.nova-lite-v1:0" } });
    expect((screen.getByLabelText("Region") as HTMLInputElement).value).toBe("us-east-1");
    fireEvent.click(screen.getByRole("button", { name: "Add model" }));
    await waitFor(() => expect(api.addModel).toHaveBeenCalledWith({
      name: "nova-lite", kind: "external_model", model_id: "amazon.nova-lite-v1:0", region: "us-east-1",
      model_class: "frontier", price_in: null, price_out: null,
    }));
    await waitFor(() => expect(api.models).toHaveBeenCalledTimes(2)); // the list is read again
    expect(screen.queryByRole("heading", { name: "Add a model" })).toBeNull();
  });

  it("adds a model on Unity Gateway from the reviewer's serving endpoints", async () => {
    open();
    fireEvent.click(await screen.findByRole("button", { name: /add a model/i }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "llama" } });
    fireEvent.change(await screen.findByLabelText("Serving endpoint"), { target: { value: "databricks-llama-4-maverick" } });
    fireEvent.change(screen.getByLabelText("Price per million input tokens, USD (optional)"), { target: { value: "0.5" } });
    fireEvent.click(screen.getByRole("button", { name: "Add model" }));
    await waitFor(() => expect(api.addModel).toHaveBeenCalledWith({
      name: "llama", kind: "ug_model", endpoint: "databricks-llama-4-maverick", model_class: "frontier",
      price_in: 0.5, price_out: null,
    }));
  });

  it("lists the problems of a refused model", async () => {
    vi.mocked(api.addModel).mockRejectedValue(new ApiError(422, "model_invalid", ["region: give an AWS region such as us-east-1"]));
    open();
    fireEvent.click(await screen.findByRole("button", { name: /add a model/i }));
    fireEvent.click(screen.getByRole("radio", { name: /external model/i }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "nova-lite" } });
    fireEvent.change(screen.getByLabelText("Bedrock model ID"), { target: { value: "amazon.nova-lite-v1:0" } });
    fireEvent.click(screen.getByRole("button", { name: "Add model" }));
    expect(await screen.findByText("region: give an AWS region such as us-east-1")).toBeTruthy();
  });

  it("tests a model, polls until it finishes and shows the result", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      vi.mocked(api.modelTest)
        .mockResolvedValueOnce({ state: "running", message: null, tested_at: null })
        .mockResolvedValueOnce({ state: "passed", message: "answered in 812 ms", tested_at: "2026-10-31T09:00:00+00:00" });
      const recorded = { ...NOVA, test: { status: "passed" as const, message: "answered in 812 ms", job_run_id: 4242 } };
      vi.mocked(api.models).mockResolvedValueOnce(list([GPT, LLAMA, NOVA])).mockResolvedValue(list([GPT, LLAMA, recorded]));
      open();
      fireEvent.click(await screen.findByRole("button", { name: "Test nova-lite" }));
      expect(await screen.findByText("Testing…")).toBeTruthy();
      await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
      expect(await screen.findByText("Passed: answered in 812 ms")).toBeTruthy();
      expect(api.testModel).toHaveBeenCalledWith("nova-lite");
      expect(api.modelTest).toHaveBeenCalledWith("nova-lite", 4242);
      await waitFor(() => expect(api.models).toHaveBeenCalledTimes(2));
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows no Passed for a model whose target was edited after a passed test", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const recorded = { ...NOVA, test: { status: "passed" as const, message: "ok", job_run_id: 4242 } };
      vi.mocked(api.models)
        .mockResolvedValueOnce(list([GPT, LLAMA, NOVA]))
        .mockResolvedValueOnce(list([GPT, LLAMA, recorded]))
        .mockResolvedValue(list([GPT, LLAMA, NOVA])); // the edit cleared the recorded test
      vi.mocked(api.modelTest).mockResolvedValue({ state: "passed", message: "ok", tested_at: "2026-10-31T09:00:00+00:00" });
      open();
      fireEvent.click(await screen.findByRole("button", { name: "Test nova-lite" }));
      expect(await screen.findByText("Passed: ok")).toBeTruthy();
      fireEvent.click(screen.getByRole("button", { name: "Edit nova-lite" }));
      fireEvent.change(screen.getByLabelText("Bedrock model ID"), { target: { value: "amazon.nova-pro-v1:0" } });
      fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
      await waitFor(() => expect(api.updateModel).toHaveBeenCalled());
      await waitFor(() => expect(api.models).toHaveBeenCalledTimes(3));
      const external = screen.getByRole("table", { name: "External models" });
      await waitFor(() => expect(within(external).getByText("Not tested")).toBeTruthy());
      expect(screen.queryByText(/Passed/)).toBeNull();
      expect(screen.getByRole("button", { name: "Enable nova-lite" }).hasAttribute("disabled")).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it("stops counting a test as running after five minutes and lets it start again", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      vi.mocked(api.modelTest).mockResolvedValue({ state: "running", message: null, tested_at: null });
      open();
      const button = await screen.findByRole("button", { name: "Test nova-lite" });
      fireEvent.click(button);
      expect(await screen.findByText("Testing…")).toBeTruthy();
      await act(async () => { await vi.advanceTimersByTimeAsync(5000 * 61); });
      expect(await screen.findByText("Still running. Start the test again later.")).toBeTruthy();
      expect(screen.queryByText(/Reload the page/)).toBeNull();
      expect(screen.getByRole("button", { name: "Test nova-lite" }).hasAttribute("disabled")).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps the current endpoint of a row as an option when the endpoint list lacks it", async () => {
    const gone = row({ name: "old", endpoint: "databricks-retired" });
    vi.mocked(api.models).mockResolvedValue(list([GPT, gone]));
    open();
    fireEvent.click(await screen.findByRole("button", { name: "Edit old" }));
    const select = (await screen.findByLabelText("Serving endpoint")) as HTMLSelectElement;
    expect(Array.from(select.options).map((o) => o.value)).toContain("databricks-retired");
    expect(select.value).toBe("databricks-retired");
    fireEvent.change(screen.getByLabelText("Class"), { target: { value: "frontier" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(api.updateModel).toHaveBeenCalledWith("old", expect.objectContaining({
      endpoint: "databricks-retired", model_class: "frontier",
    })));
  });

  it("enables a model only after its last test passed", async () => {
    const tested = { ...NOVA, test: { status: "passed" as const, message: "ok", job_run_id: 7 } };
    vi.mocked(api.models).mockResolvedValue(list([GPT, LLAMA, tested]));
    open();
    expect((await screen.findByRole("button", { name: "Enable llama" })).hasAttribute("disabled")).toBe(true);
    const enable = screen.getByRole("button", { name: "Enable nova-lite" });
    expect(enable.hasAttribute("disabled")).toBe(false);
    fireEvent.click(enable);
    await waitFor(() => expect(api.updateModel).toHaveBeenCalledWith("nova-lite", { enabled: true }));
  });

  it("asks for a test of a model on Unity Gateway that has not passed one", async () => {
    open();
    const ug = await screen.findByRole("table", { name: "On Unity Gateway" });
    expect(within(ug).getByText(/checks that the eval service principal can query this endpoint/)).toBeTruthy();
  });

  it("says so when the catalog cannot be read", async () => {
    vi.mocked(api.models).mockResolvedValue(list([GPT], false));
    open();
    expect(await screen.findByText(/model catalog cannot be read/i)).toBeTruthy();
    expect(screen.queryByRole("table", { name: "External models" })).toBeNull();
  });
});
