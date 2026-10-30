import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, ApiError } from "../api/client";
import type { AppConfig, DraftIn, Me } from "../api/types";
import { candidatePath, canSeeCandidates } from "../candidates";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { Loading } from "../components/Loading";
import { useAsync } from "../hooks/useAsync";

type Source = "zip" | "folder";

// The file's bytes as base64. The zip is sent as it is; the server unpacks and checks it.
function toBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error);
    reader.onload = () => {
      const bytes = new Uint8Array(reader.result as ArrayBuffer);
      let binary = "";
      for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
      resolve(btoa(binary));
    };
    reader.readAsArrayBuffer(file);
  });
}

export default function UploadDraft() {
  const me = useAsync<Me>(() => api.me(), []); // shared with the header: one /api/me request per page load
  const config = useAsync<AppConfig>(() => api.config(), []);
  const navigate = useNavigate();
  const [dept, setDept] = useState("");
  const [source, setSource] = useState<Source>("zip");
  const [archive, setArchive] = useState<File | null>(null);
  const [folder, setFolder] = useState<File[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const errorRef = useRef<HTMLDivElement>(null);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => { if (error) errorRef.current?.focus(); }, [error]); // the problems are read out and in view

  if (me.loading || config.loading) return <Loading label="Loading" />;
  if (me.error) return <ErrorState error={me.error} onRetry={me.reload} />;
  if (config.error) return <ErrorState error={config.error} onRetry={config.reload} />;
  if (!canSeeCandidates(me.data)) {
    return (
      <EmptyState title="Upload a draft">
        <p>Only skill authors and reviewers can upload drafts. Ask your team lead to add you to the authors group. <Link to="/access">Who can do what</Link></p>
      </EmptyState>
    );
  }

  const chosen = source === "zip" ? archive !== null : folder.length > 0;
  const ready = dept !== "" && chosen && !submitting;
  const missing = [
    ...(dept === "" ? ["choose a department"] : []),
    ...(chosen ? [] : [source === "zip" ? "choose a zip file" : "choose the skill folder"]),
  ];

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!ready) return;
    setSubmitting(true);
    setError(null);
    try {
      const body: DraftIn = source === "zip" && archive
        ? { dept, archive: await toBase64(archive) }
        : {
          dept,
          files: await Promise.all(
            folder.map(async (f) => ({ path: f.webkitRelativePath || f.name, content: await toBase64(f) })),
          ),
        };
      const out = await api.uploadDraft(body);
      navigate(candidatePath({ run_key: out.run_key, dept: out.dept, name: out.name }));
    } catch (err) {
      if (mounted.current) setError(err);
    } finally {
      if (mounted.current) setSubmitting(false);
    }
  }

  const problems = error instanceof ApiError ? error.errors : [];

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Upload a draft</h1>
          <p className="lede">
            Evaluate a skill in the Skill Hub before you open a pull request. A draft is never released, and it is
            removed 7 days after upload. To release a skill, open a pull request.
          </p>
        </div>
      </div>
      <form onSubmit={submit} noValidate>
        <fieldset className="form-group">
          <legend>Skill</legend>
          <div className="upload-fields">
            <div className="field">
              <label htmlFor="draft-dept">Department</label>
              <select id="draft-dept" value={dept} onChange={(e) => setDept(e.target.value)}>
                <option value="">Choose a department</option>
                {(config.data?.departments ?? []).map((d) => <option key={d} value={d}>{d}</option>)}
              </select>
            </div>
            <fieldset className="field upload-source">
              <legend className="field-label">What to upload</legend>
              <label className="field-inline">
                <input type="radio" name="draft-source" checked={source === "zip"} onChange={() => setSource("zip")} />
                A zip file
              </label>
              <label className="field-inline">
                <input type="radio" name="draft-source" checked={source === "folder"} onChange={() => setSource("folder")} />
                The skill folder
              </label>
            </fieldset>
            {source === "zip" ? (
              <div className="field">
                <label htmlFor="draft-zip">Zip of the skill folder</label>
                {/* A key per picker, so React never reuses the folder picker's element for the zip picker. */}
                <input key="zip" id="draft-zip" type="file"
                  accept=".zip,application/zip,application/x-zip-compressed" aria-describedby="draft-needs"
                  onChange={(e) => setArchive(e.target.files?.[0] ?? null)} />
                {archive && <p className="hint">{archive.name}</p>}
              </div>
            ) : (
              <div className="field">
                <label htmlFor="draft-folder">Skill folder</label>
                {/* A folder picker: React has no prop for it, so the attribute is set when the element mounts. */}
                <input key="folder" id="draft-folder" type="file" multiple aria-describedby="draft-needs"
                  ref={(el) => el?.setAttribute("webkitdirectory", "")}
                  onChange={(e) => setFolder(Array.from(e.target.files ?? []))} />
                {folder.length > 0 && <p className="hint">{folder.length} files chosen</p>}
              </div>
            )}
          </div>
          <div id="draft-needs" className="upload-needs">
            <p className="field-label">The upload needs</p>
            <ul>
              <li><code>SKILL.md</code> with <code>name</code> and <code>description</code> in its frontmatter.</li>
              <li><code>evals/evals.json</code> with the test cases.</li>
              <li>Optional: <code>evals/rubric.json</code>. Without it, the draft runs in trigger mode only.</li>
              <li>Optional: a <code>fixtures/</code> folder, used instead of the default fixtures for this draft.</li>
              <li>At most 200 files and 4 MB once unpacked.</li>
            </ul>
          </div>
        </fieldset>
        <div className="form-actions">
          <button type="submit" className="btn-primary" disabled={!ready} aria-describedby="draft-missing">Upload draft</button>
          <p role="status" aria-live="polite" className="status-line">{submitting ? "Checking and uploading the draft" : ""}</p>
        </div>
        <div>
          {/* Why the button is off, so a disabled button is never a dead end. */}
          {!ready && !submitting && missing.length > 0 && (
            <p id="draft-missing" className="hint upload-missing">To upload: {missing.join(" and ")}.</p>
          )}
        </div>
      </form>
      {error ? (
        problems.length > 0 ? (
          <div role="alert" className="error" tabIndex={-1} ref={errorRef}>
            <p>The draft was not uploaded</p>
            <ul className="error-list">{problems.map((p) => <li key={p}>{p}</li>)}</ul>
            <p className="muted">Fix these in the skill folder, then upload it again.</p>
          </div>
        ) : (
          <div tabIndex={-1} ref={errorRef}><ErrorState error={error} /></div>
        )
      ) : null}
    </>
  );
}
