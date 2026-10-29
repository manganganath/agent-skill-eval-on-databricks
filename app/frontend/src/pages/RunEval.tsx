import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { AppConfig, EvalRunStatus, JournalRow, Mode, SkillSummary } from "../api/types";
import { MODES } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { ErrorState } from "../components/ErrorState";
import { Loading } from "../components/Loading";

const MAX_POLLS = 40;
const POLL_MS = 5000;
const TERMINAL = ["TERMINATED", "SKIPPED", "INTERNAL_ERROR"];
const MODE_HINTS: Record<Mode, string> = {
  trigger: "Checks that the agent picks the skill when it should, and only then.",
  effectiveness: "Compares answers with and without the skill against the rubric.",
};

// One picker per selected skill. It lists the published versions from the skill's history. "Live" sends no pin.
function VersionPicker({ fqn, name, liveVersion, value, onChange }: {
  fqn: string; name: string; liveVersion: number | null; value: number | undefined;
  onChange: (v: number | undefined) => void;
}) {
  const versions = useAsync<JournalRow[]>(() => api.versions(fqn), [fqn]);
  if (versions.loading) return <Loading label={`Loading versions of ${name}`} />;
  if (versions.error) return <ErrorState error={versions.error} onRetry={versions.reload} />;
  const published = (versions.data ?? [])
    .filter((r) => r.action === "published" && r.version != null)
    .map((r) => r.version!);
  return (
    <label className="field-inline">
      <span className="field-label">Version of {name}</span>
      <select aria-label={`Version of ${name}`} value={value ?? ""}
        onChange={(e) => onChange(e.target.value ? Number(e.target.value) : undefined)}>
        <option value="">{liveVersion == null ? "Live" : `Live (v${liveVersion})`}</option>
        {published.map((v) => <option key={v} value={v}>v{v}</option>)}
      </select>
    </label>
  );
}

export default function RunEval() {
  const skills = useAsync<SkillSummary[]>(() => api.skills(), []);
  const config = useAsync<AppConfig>(() => api.config(), []);
  const [picked, setPicked] = useState<string[]>([]);
  const [models, setModels] = useState<string[]>([]);
  const [modes, setModes] = useState<Mode[]>([]);
  const [pins, setPins] = useState<Record<string, number>>({});
  const [run, setRun] = useState<{ key: string; jobRunId: number } | null>(null);
  const [status, setStatus] = useState<EvalRunStatus | null>(null);
  const [gaveUp, setGaveUp] = useState(false);
  const [submitError, setSubmitError] = useState<unknown>(null);
  const [pollError, setPollError] = useState<unknown>(null);

  const [submitting, setSubmitting] = useState(false);
  const mounted = useRef(true);
  const inFlight = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);

  const active = run !== null && !gaveUp && !pollError &&
    !(status && (status.result_state !== null || TERMINAL.includes(status.state)));
  const ready = picked.length > 0 && models.length > 0 && modes.length > 0 && !submitting && !active;

  // Polls every 5 s. Stops on a result state, on unmount (the timer is cleared) or after MAX_POLLS.
  useEffect(() => {
    if (!run) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let polls = 0;
    const tick = async () => {
      try {
        const s = await api.evalRun(run.jobRunId);
        if (cancelled) return;
        setStatus(s);
        const done = s.result_state !== null || TERMINAL.includes(s.state);
        if (done) return;
        if (++polls >= MAX_POLLS) { setGaveUp(true); return; }
        timer = setTimeout(tick, POLL_MS);
      } catch (err) {
        if (!cancelled) setPollError(err);
      }
    };
    tick();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, [run]);

  const toggle = <T extends string>(list: T[], value: T, set: (v: T[]) => void) =>
    set(list.includes(value) ? list.filter((x) => x !== value) : [...list, value]);

  const setPin = (fqn: string, version: number | undefined) =>
    setPins((current) => {
      const next = { ...current };
      if (version === undefined) delete next[fqn];
      else next[fqn] = version;
      return next;
    });

  async function submit() {
    if (inFlight.current || active) return;
    inFlight.current = true;
    setSubmitting(true);
    setSubmitError(null);
    setPollError(null);
    setGaveUp(false);
    setStatus(null);
    setRun(null);
    // Only pins for skills that are still picked are sent. With no pins the body has no versions field.
    const pinned = Object.fromEntries(Object.entries(pins).filter(([fqn]) => picked.includes(fqn)));
    const body = {
      skills: picked, models, modes,
      ...(Object.keys(pinned).length > 0 ? { versions: pinned } : {}),
    };
    try {
      const out = await api.startEval(body);
      if (mounted.current) setRun({ key: out.run_key, jobRunId: out.job_run_id });
    } catch (err) {
      if (mounted.current) setSubmitError(err);
    } finally {
      inFlight.current = false;
      if (mounted.current) setSubmitting(false);
    }
  }

  if (skills.loading || config.loading) return <Loading label="Loading options" />;
  if (skills.error) return <ErrorState error={skills.error} onRetry={skills.reload} />;
  if (config.error) return <ErrorState error={config.error} onRetry={config.reload} />;

  const liveOf = (fqn: string) => (skills.data ?? []).find((s) => s.fqn === fqn)?.live_version ?? null;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Run an evaluation</h1>
          <p className="lede">Each skill runs its live version unless you pick another registered version.</p>
        </div>
      </div>
      <fieldset className="form-group">
        <legend>Skills</legend>
        {(skills.data ?? []).length === 0 && (
          <p className="muted">
            No skills to evaluate. Only published skills that you can read are listed here. Ask your team lead for access.
          </p>
        )}
        {(skills.data ?? []).length > 0 && (
          <div className="choice-grid scroll">
            {(skills.data ?? []).map((s) => (
              <label key={s.fqn} className="choice"><input type="checkbox" checked={picked.includes(s.fqn)}
                onChange={() => toggle(picked, s.fqn, setPicked)} aria-label={`${s.dept}/${s.name}`} />
                <span className="choice-text"><strong>{s.name}</strong><span className="choice-sub">{s.dept}</span></span>
              </label>
            ))}
          </div>
        )}
      </fieldset>
      {picked.length > 0 && (
        <fieldset className="form-group">
          <legend>Versions</legend>
          <div className="version-list">
            {picked.map((fqn) => (
              <VersionPicker key={fqn} fqn={fqn} name={fqn.split(".").pop() ?? fqn} liveVersion={liveOf(fqn)}
                value={pins[fqn]} onChange={(v) => setPin(fqn, v)} />
            ))}
          </div>
        </fieldset>
      )}
      <fieldset className="form-group">
        <legend>Models</legend>
        <div className="choice-grid">
          {Object.entries(config.data?.models ?? {}).map(([name, cls]) => (
            <label key={name} className="choice"><input type="checkbox" checked={models.includes(name)}
              onChange={() => toggle(models, name, setModels)} aria-label={name} />
              <span className="choice-text"><strong>{name}</strong><span className="choice-sub">{cls}</span></span>
            </label>
          ))}
        </div>
      </fieldset>
      <fieldset className="form-group">
        <legend>Modes</legend>
        <div className="choice-grid">
          {MODES.map((m) => (
            <label key={m} className="choice"><input type="checkbox" checked={modes.includes(m)}
              onChange={() => toggle(modes, m, setModes)} aria-label={m} />
              <span className="choice-text"><strong>{m}</strong><span className="choice-sub">{MODE_HINTS[m]}</span></span>
            </label>
          ))}
        </div>
      </fieldset>
      <div className="form-actions">
        <button type="button" className="btn-primary" disabled={!ready} onClick={submit}>Start evaluation</button>
        <p role="status" aria-live="polite" className="status-line">
          {run && <>Run {run.key} (job {run.jobRunId}): {status ? `${status.state}${status.result_state ? ` · ${status.result_state}` : ""}` : "starting"}</>}
          {gaveUp && " Status checks stopped. Reload the page to check again."}
        </p>
      </div>
      {submitError ? <ErrorState error={submitError} /> : null}
      {pollError ? <ErrorState error={pollError} /> : null}
    </>
  );
}
