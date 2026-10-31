import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import type {
  AppConfig, CandidateKind, CandidateSummary, EvalRunIn, EvalRunStatus, JournalRow, Me, Mode, ModelsList, SkillSummary,
} from "../api/types";
import { MODES } from "../api/types";
import { KIND_LABELS, groupByKind, targetOf } from "../models";
import { canSeeCandidates, isDraftKey, prNumberOf } from "../candidates";
import { useAsync } from "../hooks/useAsync";
import { CandidateBadge } from "../components/CandidateBadge";
import { ErrorState } from "../components/ErrorState";
import { Loading } from "../components/Loading";

const MAX_POLLS = 40;
const POLL_MS = 5000;
const TERMINAL = ["TERMINATED", "SKIPPED", "INTERNAL_ERROR"];
const MODE_HINTS: Record<Mode, string> = {
  trigger: "Checks that the agent picks the skill when it should, and only then.",
  effectiveness: "Compares answers with and without the skill against the rubric and the cases' assertions.",
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

// A candidate choice: one skill of one upload (a pull request's, or a draft). The run key names the upload's folder.
type Choice = {
  run_key: string; kind: CandidateKind; pr_number: number | null; dept: string; name: string; has_rubric: boolean | null;
  rubric_optional?: boolean | null;
};
const choiceId = (c: Choice) => `${c.run_key}|${c.dept}/${c.name}`;
const choiceLabel = (c: Choice) => (c.kind === "draft" ? "draft" : `candidate in PR #${c.pr_number}`);

// The candidate a candidate page linked to (?candidate=<run key>&skill=<dept>/<name>), when the link is well formed.
function linked(params: URLSearchParams): Choice | null {
  const runKey = params.get("candidate") ?? "";
  const [dept, name, ...rest] = (params.get("skill") ?? "").split("/");
  if (!dept || !name || rest.length > 0) return null;
  const pr = prNumberOf(runKey);
  if (pr !== null) return { run_key: runKey, kind: "pr", pr_number: pr, dept, name, has_rubric: null };
  return isDraftKey(runKey) ? { run_key: runKey, kind: "draft", pr_number: null, dept, name, has_rubric: null } : null;
}

export default function RunEval() {
  const [params] = useSearchParams();
  const skills = useAsync<SkillSummary[]>(() => api.skills(), []);
  const config = useAsync<AppConfig>(() => api.config(), []);
  // The enabled model catalog rows, after the hub.yaml models. Without the catalog the hub.yaml models still run.
  const catalog = useAsync<ModelsList>(() => api.models(), []);
  // One /api/me request per page load, shared with the header. Candidates are asked for only by authors and reviewers.
  const me = useAsync<Me>(() => api.me(), []);
  const reviewing = canSeeCandidates(me.data);
  const cands = useAsync<CandidateSummary[]>(
    () => (reviewing ? api.candidates() : Promise.resolve([])), [reviewing],
  );
  const [picked, setPicked] = useState<string[]>([]);
  const [pickedCandidates, setPickedCandidates] = useState<string[]>([]);
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

  // The listed candidates, plus the one a candidate page linked to if the list does not have it (an earlier upload).
  const fromLink = reviewing ? linked(params) : null;
  const listed: Choice[] = reviewing ? cands.data ?? [] : [];
  const choices = fromLink && !listed.some((c) => choiceId(c) === choiceId(fromLink)) ? [fromLink, ...listed] : listed;
  // One run evaluates live skills, or skills of one candidate upload: the Job reads one skills folder per run.
  const candidateKey = pickedCandidates.length > 0 ? pickedCandidates[0].split("|")[0] : null;
  // A draft without a rubric can run trigger mode only: the effectiveness judges score against the rubric. A draft
  // whose effectiveness cases are all trigger cases with assertions is the exception: they are judged on those.
  const triggerOnly = choices.some((c) => pickedCandidates.includes(choiceId(c)) && c.kind === "draft" &&
    c.has_rubric === false && c.rubric_optional !== true);
  const runModes = triggerOnly ? modes.filter((m) => m !== "effectiveness") : modes;

  // The linked candidate is picked once, when the candidates have loaded.
  const preselected = useRef(false);
  useEffect(() => {
    if (preselected.current || !fromLink || cands.loading || !reviewing) return;
    preselected.current = true;
    setPickedCandidates([choiceId(fromLink)]);
  }, [fromLink, cands.loading, reviewing]);

  const active = run !== null && !gaveUp && !pollError &&
    !(status && (status.result_state !== null || TERMINAL.includes(status.state)));
  const chosen = picked.length > 0 || pickedCandidates.length > 0;
  const ready = chosen && models.length > 0 && runModes.length > 0 && !submitting && !active;

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
    const body: EvalRunIn = candidateKey !== null
      ? {
        candidate: { run_key: candidateKey, skills: pickedCandidates.map((id) => id.split("|")[1]) },
        models, modes: runModes,
      }
      : { skills: picked, models, modes, ...(Object.keys(pinned).length > 0 ? { versions: pinned } : {}) };
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

  // Models on Unity Gateway, then external models. External agents are not offered in this release.
  const catalogGroups = groupByKind((catalog.data?.models ?? []).filter(
    (m) => !m.release_gate && m.enabled && (m.kind === "ug_model" || m.kind === "external_model"),
  ));
  const liveOf = (fqn: string) => (skills.data ?? []).find((s) => s.fqn === fqn)?.live_version ?? null;
  const liveChoices = (
    <>
      {(skills.data ?? []).length === 0 && (
        <p className="muted">
          No skills to evaluate. Only published skills that you can read are listed here. Ask your team lead for access.
        </p>
      )}
      {(skills.data ?? []).length > 0 && (
        <div className="choice-grid scroll">
          {(skills.data ?? []).map((s) => (
            <label key={s.fqn} className="choice"><input type="checkbox" checked={picked.includes(s.fqn)}
              disabled={pickedCandidates.length > 0}
              onChange={() => toggle(picked, s.fqn, setPicked)} aria-label={`${s.dept}/${s.name}`} />
              <span className="choice-text"><strong>{s.name}</strong><span className="choice-sub">{s.dept}</span></span>
            </label>
          ))}
        </div>
      )}
    </>
  );

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Run an evaluation</h1>
          <p className="lede">
            Each skill runs its live version unless you pick another registered version.
            {reviewing && " A candidate runs the content of its pull request."}
          </p>
        </div>
      </div>
      {me.data && !reviewing && (
        <p className="hint">
          Authors and reviewers can also evaluate skills under review. <Link to="/access">Who can do what</Link>
        </p>
      )}
      <fieldset className="form-group">
        <legend>Skills</legend>
        {reviewing && (
          <p className="hint">One run evaluates live skills, or candidates from one pull request upload.</p>
        )}
        {reviewing ? (
          <div role="group" aria-labelledby="live-skills" className="choice-section">
            <h3 id="live-skills" className="choice-section-title">Live skills</h3>
            {liveChoices}
          </div>
        ) : liveChoices}
        {reviewing && (
          <div role="group" aria-labelledby="candidate-skills" className="choice-section">
            <h3 id="candidate-skills" className="choice-section-title">Candidates under review</h3>
            {cands.loading && <Loading label="Loading candidates" />}
            {cands.error ? <ErrorState error={cands.error} onRetry={cands.reload} /> : null}
            {!cands.loading && !cands.error && choices.length === 0 && (
              <p className="muted">No skills are under review.</p>
            )}
            {choices.length > 0 && (
              <div className="choice-grid scroll">
                {choices.map((c) => {
                  const id = choiceId(c);
                  const blocked = picked.length > 0 || (candidateKey !== null && candidateKey !== c.run_key);
                  return (
                    <label key={id} className="choice choice-candidate">
                      <input type="checkbox" checked={pickedCandidates.includes(id)} disabled={blocked}
                        onChange={() => toggle(pickedCandidates, id, setPickedCandidates)}
                        aria-label={`${c.dept}/${c.name}, ${choiceLabel(c)}`} />
                      <span className="choice-text">
                        <strong>{c.name}</strong><span className="choice-sub">{c.dept}</span>
                        <CandidateBadge kind={c.kind} pr={c.pr_number} />
                      </span>
                    </label>
                  );
                })}
              </div>
            )}
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
        <div role="group" aria-labelledby="models-hub_yaml" className="choice-section">
          <h3 id="models-hub_yaml" className="choice-section-title">{KIND_LABELS.hub_yaml}</h3>
          <div className="choice-grid">
            {Object.entries(config.data?.models ?? {}).map(([name, cls]) => (
              <label key={name} className="choice"><input type="checkbox" checked={models.includes(name)}
                onChange={() => toggle(models, name, setModels)} aria-label={name} />
                <span className="choice-text"><strong>{name}</strong><span className="choice-sub">{cls}</span></span>
              </label>
            ))}
          </div>
        </div>
        {catalogGroups.map(([kind, rows]) => (
          <div key={kind} role="group" aria-labelledby={`models-${kind}`} className="choice-section">
            <h3 id={`models-${kind}`} className="choice-section-title">{KIND_LABELS[kind]}</h3>
            <div className="choice-grid">
              {rows.map((m) => (
                <label key={m.name} className="choice"><input type="checkbox" checked={models.includes(m.name)}
                  onChange={() => toggle(models, m.name, setModels)} aria-label={m.name} />
                  <span className="choice-text">
                    <strong>{m.name}</strong><span className="choice-sub">{m.model_class} · {targetOf(m)}</span>
                  </span>
                </label>
              ))}
            </div>
          </div>
        ))}
        {catalog.loading && <Loading label="Loading the model catalog" />}
        {catalog.error ? (
          <ErrorState error={catalog.error} onRetry={catalog.reload}
            message="The model catalog could not be loaded. The release gate models can still run." />
        ) : null}
      </fieldset>
      <fieldset className="form-group">
        <legend>Modes</legend>
        {triggerOnly && <p className="hint">A draft without a rubric runs in trigger mode only.</p>}
        <div className="choice-grid">
          {MODES.map((m) => (
            <label key={m} className="choice"><input type="checkbox" checked={runModes.includes(m)}
              disabled={triggerOnly && m === "effectiveness"}
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
