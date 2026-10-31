import { useEffect, useRef, useState, type ChangeEvent, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { ApiError, api } from "../api/client";
import type { CatalogModel, Me, ModelsList, ProbeStatus, ServingEndpoint } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { ErrorState } from "../components/ErrorState";
import { Loading } from "../components/Loading";
import { KIND_LABELS, groupByKind, targetOf } from "../models";

const POLL_MS = 5000;
const MAX_POLLS = 60; // five minutes: a serverless Job takes about a minute to start
const STILL_RUNNING = "Still running. Start the test again later.";

type Kind = "ug_model" | "external_model";
type Draft = {
  name: string; kind: Kind; endpoint: string; model_id: string; region: string; model_class: "frontier" | "fast";
  price_in: string; price_out: string;
};
// "stale": the five minutes of polling are over. The result is recorded only by polling, so the test must be started again.
type Test = ProbeStatus | "starting" | "stale";

const blank = (region: string): Draft => ({
  name: "", kind: "ug_model", endpoint: "", model_id: "", region, model_class: "frontier", price_in: "", price_out: "",
});
const fromModel = (m: CatalogModel): Draft => ({
  name: m.name, kind: m.kind === "external_model" ? "external_model" : "ug_model", endpoint: m.endpoint ?? "",
  model_id: m.model_id ?? "", region: m.region ?? "", model_class: m.model_class,
  price_in: m.price_in == null ? "" : String(m.price_in), price_out: m.price_out == null ? "" : String(m.price_out),
});
const price = (v: string): number | null => (v.trim() === "" ? null : Number(v));

function LastTest({ model, test }: { model: CatalogModel; test?: Test }) {
  if (test === "starting") return <span>Starting the test…</span>;
  if (test === "stale") return <span>{STILL_RUNNING}</span>;
  if (test?.state === "running") return <span>{test.message ?? "Testing…"}</span>;
  if (test) return <span>{test.state === "passed" ? "Passed" : test.state === "failed" ? "Failed" : "Not recorded"}{test.message ? `: ${test.message}` : ""}</span>;
  if (!model.test) return <span className="muted">Not tested</span>;
  return <span>{model.test.status === "passed" ? "Passed" : "Failed"}{model.test.message ? `: ${model.test.message}` : ""}</span>;
}

function ModelTable({ title, rows, reviewer, tests, onTest, onToggle, onEdit }: {
  title: string; rows: CatalogModel[]; reviewer: boolean; tests: Record<string, Test>;
  onTest: (m: CatalogModel) => void; onToggle: (m: CatalogModel) => void; onEdit: (m: CatalogModel) => void;
}) {
  const id = `models-${title.toLowerCase().replace(/[^a-z]+/g, "-")}`;
  return (
    <section aria-labelledby={id} className="panel">
      <h2 id={id}>{title}</h2>
      <div className="table-scroll" role="region" aria-label={title} tabIndex={0}>
        <table className="matrix">
          <caption className="visually-hidden">{title}</caption>
          <thead>
            <tr>
              <th scope="col">Name</th><th scope="col">Route</th><th scope="col">Target</th><th scope="col">Class</th>
              <th scope="col">Harness</th><th scope="col">Status</th><th scope="col">Last test</th>
              {reviewer && <th scope="col">Actions</th>}
            </tr>
          </thead>
          <tbody>
            {rows.map((m) => {
              const local = tests[m.name];
              const testing = local === "starting" || (typeof local === "object" && local.state === "running");
              const passed = m.test?.status === "passed";
              return (
                <tr key={m.name}>
                  <th scope="row">{m.name}</th>
                  <td>{m.route ?? "-"}</td><td>{targetOf(m)}</td><td>{m.model_class}</td><td>{m.harness}</td>
                  <td>{m.release_gate ? "Release gate" : m.enabled ? "Enabled" : "Disabled"}</td>
                  <td>
                    {m.release_gate ? "-" : <LastTest model={m} test={tests[m.name]} />}
                    {m.kind === "ug_model" && !passed && (
                      <span className="cell-sub">Test it: the test checks that the eval service principal can query this endpoint.</span>
                    )}
                  </td>
                  {reviewer && (
                    <td className="actions">
                      {m.release_gate ? <span className="muted">Change it in hub.yaml</span> : (
                        <>
                          <button type="button" className="btn-sm" disabled={testing} onClick={() => onTest(m)}
                            aria-label={`Test ${m.name}`}>Test</button>{" "}
                          <button type="button" className="btn-sm" disabled={!m.enabled && !passed}
                            onClick={() => onToggle(m)} aria-label={`${m.enabled ? "Disable" : "Enable"} ${m.name}`}>
                            {m.enabled ? "Disable" : "Enable"}
                          </button>{" "}
                          <button type="button" className="btn-sm" onClick={() => onEdit(m)} aria-label={`Edit ${m.name}`}>
                            Edit
                          </button>
                        </>
                      )}
                    </td>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function ModelForm({ editing, region, onSaved, onCancel }: {
  editing: CatalogModel | null; region: string; onSaved: () => void; onCancel: () => void;
}) {
  const [draft, setDraft] = useState<Draft>(editing ? fromModel(editing) : blank(region));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const loaded = useAsync<ServingEndpoint[]>(
    () => (draft.kind === "ug_model" ? api.servingEndpoints() : Promise.resolve([])), [draft.kind],
  );
  // The row's current endpoint stays an option when the list lacks it, so the form can still be saved.
  const endpoints = {
    ...loaded,
    data: draft.endpoint && loaded.data && !loaded.data.some((ep) => ep.name === draft.endpoint)
      ? [{ name: draft.endpoint, task: "" }, ...loaded.data] : loaded.data,
  };
  const set = (key: keyof Draft) => (e: ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setDraft({ ...draft, [key]: e.target.value });

  async function save(e: FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(null);
    const target = draft.kind === "ug_model"
      ? { endpoint: draft.endpoint }
      : { model_id: draft.model_id.trim(), region: draft.region.trim() };
    const rest = { model_class: draft.model_class, price_in: price(draft.price_in), price_out: price(draft.price_out) };
    try {
      if (editing) await api.updateModel(editing.name, { ...target, ...rest });
      else await api.addModel({ name: draft.name.trim(), kind: draft.kind, ...target, ...rest });
      onSaved();
    } catch (err) {
      setError(err);
    } finally {
      setSaving(false);
    }
  }

  const problems = error instanceof ApiError ? error.errors : [];
  return (
    <form className="panel" aria-labelledby="model-form" onSubmit={save}>
      <h2 id="model-form">{editing ? `Edit ${editing.name}` : "Add a model"}</h2>
      <p className="notice">A model outside Databricks receives the skill text and the test cases of every evaluation that runs on it.</p>
      {editing && <p className="hint">Changing where it points disables it until it passes a new test.</p>}
      {!editing && (
        <fieldset className="form-group">
          <legend>Where it runs</legend>
          <label className="field-inline"><input type="radio" name="kind" value="ug_model" checked={draft.kind === "ug_model"}
            onChange={() => setDraft({ ...draft, kind: "ug_model" })} /> On Unity Gateway</label>
          <label className="field-inline"><input type="radio" name="kind" value="external_model"
            checked={draft.kind === "external_model"} onChange={() => setDraft({ ...draft, kind: "external_model" })} />
            {" "}External model (Amazon Bedrock)</label>
        </fieldset>
      )}
      {!editing && (
        <div className="field">
          <label htmlFor="model-name">Name</label>
          <input id="model-name" value={draft.name} onChange={set("name")} required maxLength={40} />
          <span className="hint">Lowercase letters, digits and hyphens. It names the model in results.</span>
        </div>
      )}
      {draft.kind === "ug_model" ? (
        <div className="field">
          <label htmlFor="model-endpoint">Serving endpoint</label>
          {endpoints.loading ? <Loading label="Loading serving endpoints" />
            : endpoints.error ? <ErrorState error={endpoints.error} onRetry={endpoints.reload} />
            : (
              <select id="model-endpoint" value={draft.endpoint} onChange={set("endpoint")} required>
                <option value="">Pick an endpoint</option>
                {(endpoints.data ?? []).map((ep) => <option key={ep.name} value={ep.name}>{ep.name}</option>)}
              </select>
            )}
        </div>
      ) : (
        <>
          <div className="field">
            <label htmlFor="model-id">Bedrock model ID</label>
            <input id="model-id" value={draft.model_id} onChange={set("model_id")} required />
            <span className="hint">A model ID such as amazon.nova-lite-v1:0, or an inference profile ID such as us.anthropic.claude-sonnet-5-5.</span>
          </div>
          <div className="field">
            <label htmlFor="model-region">Region</label>
            <input id="model-region" value={draft.region} onChange={set("region")} required />
          </div>
        </>
      )}
      <div className="field">
        <label htmlFor="model-class">Class</label>
        <select id="model-class" value={draft.model_class} onChange={set("model_class")}>
          <option value="frontier">frontier</option><option value="fast">fast</option>
        </select>
      </div>
      <div className="field">
        <label htmlFor="model-price-in">Price per million input tokens, USD (optional)</label>
        <input id="model-price-in" type="number" min="0" step="any" value={draft.price_in} onChange={set("price_in")} />
      </div>
      <div className="field">
        <label htmlFor="model-price-out">Price per million output tokens, USD (optional)</label>
        <input id="model-price-out" type="number" min="0" step="any" value={draft.price_out} onChange={set("price_out")} />
      </div>
      <div className="form-actions">
        <button type="submit" className="btn-primary" disabled={saving}>{editing ? "Save changes" : "Add model"}</button>
        <button type="button" onClick={onCancel}>Cancel</button>
      </div>
      {error ? <ErrorState error={error} /> : null}
      {problems.length > 0 && <ul className="error-list">{problems.map((p) => <li key={p}>{p}</li>)}</ul>}
    </form>
  );
}

export default function Models() {
  const me = useAsync<Me>(() => api.me(), []);
  const list = useAsync<ModelsList>(() => api.models(), []);
  const [form, setForm] = useState<{ editing: CatalogModel | null } | null>(null);
  const [tests, setTests] = useState<Record<string, Test>>({});
  const [actionError, setActionError] = useState<unknown>(null);
  const mounted = useRef(true);
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; timers.current.forEach(clearTimeout); };
  }, []);

  const reviewer = me.data?.is_reviewer === true;
  const forget = (name: string) => setTests(({ [name]: _gone, ...rest }) => rest);
  // Once the list is read again it holds each finished test, so the local result is dropped. Only "stale" stays.
  useEffect(() => {
    setTests((t) => {
      const kept = Object.entries(t).filter(([, v]) => typeof v === "string" || (v.state !== "passed" && v.state !== "failed"));
      return kept.length === Object.keys(t).length ? t : Object.fromEntries(kept);
    });
  }, [list.data]);

  // Polls every 5 s until the test has finished. A finished test is recorded by the API, so the list is read again.
  function poll(name: string, jobRunId: number, left = MAX_POLLS) {
    api.modelTest(name, jobRunId).then((s) => {
      if (!mounted.current) return;
      if (s.state === "running" && left <= 1) {
        setTests((t) => ({ ...t, [name]: "stale" }));
        return;
      }
      setTests((t) => ({ ...t, [name]: s }));
      if (s.state === "running") timers.current.push(setTimeout(() => poll(name, jobRunId, left - 1), POLL_MS));
      else list.reload();
    }, (err) => {
      if (!mounted.current) return;
      setActionError(err);
      forget(name);
    });
  }

  async function test(m: CatalogModel) {
    setActionError(null);
    setTests((t) => ({ ...t, [m.name]: "starting" }));
    try {
      const { job_run_id } = await api.testModel(m.name);
      if (mounted.current) poll(m.name, job_run_id);
    } catch (err) {
      if (!mounted.current) return;
      setActionError(err);
      forget(m.name);
    }
  }

  async function toggle(m: CatalogModel) {
    setActionError(null);
    try {
      await api.updateModel(m.name, { enabled: !m.enabled });
      if (mounted.current) list.reload();
    } catch (err) {
      if (mounted.current) setActionError(err);
    }
  }

  if (list.loading && !list.data) return <Loading label="Loading models" />;
  if (list.error) return <ErrorState error={list.error} onRetry={list.reload} />;
  const data = list.data!;
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Models</h1>
          <p className="lede">
            The models an evaluation can run on. Only the release gate models, set in hub.yaml, gate a release.
          </p>
        </div>
        {reviewer && !form && (
          <button type="button" className="btn-primary" onClick={() => setForm({ editing: null })}>Add a model</button>
        )}
      </div>
      {me.data && !reviewer && (
        <p className="hint">Reviewers add, test, enable and disable models. <Link to="/access">Who can do what</Link></p>
      )}
      {!data.catalog_available && (
        <p role="note" className="notice">
          The model catalog cannot be read, so only the release gate models are listed. An administrator may need to
          run the skillhub-bootstrap Job.
        </p>
      )}
      {form && (
        <ModelForm key={form.editing?.name ?? "new"} editing={form.editing} region={data.bedrock_region}
          onCancel={() => setForm(null)} onSaved={() => { if (form.editing) forget(form.editing.name); setForm(null); list.reload(); }} />
      )}
      {actionError ? <ErrorState error={actionError} /> : null}
      {groupByKind(data.models).map(([kind, rows]) => (
        <ModelTable key={kind} title={KIND_LABELS[kind]} rows={rows} reviewer={reviewer} tests={tests}
          onTest={test} onToggle={toggle} onEdit={(m) => setForm({ editing: m })} />
      ))}
    </>
  );
}
