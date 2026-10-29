import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { AppConfig, EvalAccess, TryModelResult } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { ErrorState } from "./ErrorState";
import { Loading } from "./Loading";

const MAX_MODELS = 3;
const MAX_PROMPT = 4000;
const CANDIDATE_INTRO = "Runs as you. The agent sees this upload in place of the live version, next to the live skills you can use. Nobody else can load it. Pick up to 3 models. Answers are shown as plain text.";

// A live skill, or a skill under review (a candidate) that is staged for the run only.
export type TryTarget = { fqn: string } | { runKey: string; dept: string; skill: string };

// Answers are model output: they are shown as React text only, never as HTML or Markdown.
export function TryPanel({ target, evalAccess }: { target: TryTarget; evalAccess: EvalAccess }) {
  const config = useAsync<AppConfig>(() => api.config(), []);
  const [prompt, setPrompt] = useState("");
  const [models, setModels] = useState<string[]>([]);
  const [results, setResults] = useState<TryModelResult[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const inFlight = useRef(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);

  async function run() {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    setResults(null);
    try {
      const out = "fqn" in target
        ? await api.tryPrompt({ fqn: target.fqn, prompt, models })
        : await api.tryCandidate(target.runKey, target.dept, target.skill, { prompt, models });
      if (mounted.current) setResults(out);
    } catch (err) {
      if (mounted.current) setError(err);
    } finally {
      inFlight.current = false;
      if (mounted.current) setBusy(false);
    }
  }

  const toggle = (name: string) =>
    setModels((cur) => (cur.includes(name) ? cur.filter((x) => x !== name) : [...cur, name]));

  if (config.loading) return <Loading label="Loading models" />;
  if (config.error) return <ErrorState error={config.error} onRetry={config.reload} />;

  return (
    <section aria-labelledby="try-heading">
      <h2 id="try-heading">Try this skill</h2>
      <p className="muted">
        {"fqn" in target
          ? `Runs as you, so you only get the skills you can use. Pick up to ${MAX_MODELS} models. Answers are shown as plain text.`
          : CANDIDATE_INTRO}
      </p>
      <div className="field">
        <label htmlFor="try-prompt">Test input</label>
        <textarea id="try-prompt" rows={3} maxLength={MAX_PROMPT} value={prompt}
          onChange={(e) => setPrompt(e.target.value)} />
        <p className="hint">Try runs are recorded and visible to this department's eval readers.</p>
      </div>
      <fieldset className="try-models">
        <legend className="field-label">Models</legend>
        <div className="choice-grid">
          {Object.entries(config.data?.models ?? {}).map(([name, cls]) => (
            <label key={name} className="choice">
              <input type="checkbox" aria-label={name} checked={models.includes(name)}
                disabled={!models.includes(name) && models.length >= MAX_MODELS}
                onChange={() => toggle(name)} />
              <span className="choice-text"><strong>{name}</strong> <span className="choice-sub">({cls})</span></span>
            </label>
          ))}
        </div>
      </fieldset>
      <div className="form-actions">
        <button type="button" className="btn-primary" disabled={busy || !prompt.trim() || models.length === 0} onClick={run}>
          Try skill
        </button>
        <p role="status" aria-live="polite" className="status-line">{busy ? "Running the agent. This can take a minute." : ""}</p>
      </div>
      {error ? <ErrorState error={error} /> : null}
      {results?.map((r) => (
        <article key={r.model} aria-label={`Result for ${r.model}`} className="try-result">
          <h3>{r.model}</h3>
          <p>Skill invoked: {r.with_skill.invoked ? "yes" : "no"}</p>
          <div className="compare">
            <div><h4>With skill</h4><pre className="plain">{r.with_skill.text}</pre></div>
            <div><h4>Without skill</h4><pre className="plain">{r.without_skill.text}</pre></div>
          </div>
          {r.trace_url && evalAccess === "ok" && <p><a href={r.trace_url} target="_blank" rel="noreferrer">Open trace</a></p>}
        </article>
      ))}
    </section>
  );
}
