import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { AppConfig, Me, ReadableDepartments } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { ErrorState } from "../components/ErrorState";
import { Loading } from "../components/Loading";

type Row = { action: string; everyone: string; authors: string; reviewers: string };

// Who can do what. Keep in step with the checks in app/backend and with the Roles section of docs/onboarding.md.
// A row with empty author and reviewer cells is one cell across all three columns: it is not a Skill Hub role.
const ROWS: Row[] = [
  { action: "Browse live skills", everyone: "Own departments", authors: "Own departments", reviewers: "All departments" },
  { action: "Try a live skill", everyone: "Yes", authors: "Yes", reviewers: "Yes" },
  { action: "Run an eval on live skills", everyone: "Yes", authors: "Yes", reviewers: "Yes" },
  { action: "See skills under review", everyone: "No", authors: "Own departments", reviewers: "All departments" },
  { action: "Upload a draft", everyone: "No", authors: "Yes", reviewers: "Yes" },
  { action: "Try a skill under review", everyone: "No", authors: "Own departments", reviewers: "All departments" },
  { action: "Run an eval on a candidate or draft", everyone: "No", authors: "Yes", reviewers: "Yes" },
  { action: "Switch the live version", everyone: "No", authors: "No", reviewers: "Yes" },
  { action: "Add or edit a skill", everyone: "No", authors: "Through a pull request", reviewers: "Through a pull request" },
  { action: "Approve a release", everyone: "In GitHub: the required reviewers of the production environment", authors: "", reviewers: "" },
  { action: "Publish a version", everyone: "No one: CI publishes after approval", authors: "", reviewers: "" },
];

const STEPS = [
  "Change the skill in a pull request. Edit skills/<department>/<skill>/ in the Git repository.",
  "The pull request's evaluation runs by itself. A failed trigger eval blocks the merge.",
  "Authors and reviewers can follow the result as a candidate in the Skill Hub.",
  "After the merge, the required reviewers of the GitHub production environment approve the release.",
  "CI then publishes the new version and makes it live.",
];

const label = (key: string) => key.replace(/_/g, " ");

function YourAccess({ me, readable, departments }: {
  me: Me; readable: ReturnType<typeof useAsync<ReadableDepartments>>; departments: string[];
}) {
  const open = new Set(readable.data?.departments ?? []);
  return (
    <section aria-labelledby="your-access" className="panel">
      <h2 id="your-access">Your access</h2>
      <p className="muted">Signed in as {me.user}.</p>
      <p className="access-roles">
        <span className="badge badge-accent">Reader</span>
        {me.is_author && <span className="badge badge-accent">Author</span>}
        {me.is_reviewer && <span className="badge badge-accent">Reviewer</span>}
      </p>
      <h3>Departments you can read live skills in</h3>
      {readable.loading ? <Loading label="Checking your departments" />
        : readable.error ? <ErrorState error={readable.error} onRetry={readable.reload} />
        : open.size === 0 ? <p className="muted">None. See how to get access below.</p>
        : (
          <ul className="access-depts" aria-label="Departments you can read">
            {departments.filter((d) => open.has(d)).map((d) => <li key={d} className="tag">{label(d)}</li>)}
          </ul>
        )}
    </section>
  );
}

function Matrix({ me }: { me: Me }) {
  const mine = { everyone: true, authors: me.is_author, reviewers: me.is_reviewer };
  const cols = [
    { key: "everyone", title: "Reader" },
    { key: "authors", title: "Author" },
    { key: "reviewers", title: "Reviewer" },
  ] as const;
  return (
    <section aria-labelledby="who-can" className="panel">
      <h2 id="who-can">Who can do what</h2>
      <p className="muted">
        A reader is anyone signed in who can read a department's skills. Each role adds to the one before it. Your own
        columns are marked.
      </p>
      <div className="table-scroll" role="region" aria-label="Who can do what" tabIndex={0}>
        <table className="access-table">
          <caption className="visually-hidden">Who can do what</caption>
          <thead>
            <tr>
              <th scope="col">Action</th>
              {cols.map((c) => (
                <th key={c.key} scope="col" className={mine[c.key] ? "access-mine" : undefined}>
                  {c.title}{mine[c.key] && <span className="badge badge-accent"> You</span>}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {ROWS.map((r) => (
              <tr key={r.action}>
                <th scope="row">{r.action}</th>
                {r.authors === "" ? (
                  <td colSpan={3} className="access-span">{r.everyone}</td>
                ) : cols.map((c) => (
                  <td key={c.key} className={mine[c.key] ? "access-mine" : undefined}>{r[c.key]}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function HowToGet({ config }: { config: AppConfig }) {
  const { roles, department_access: access = {} } = config;
  return (
    <section aria-labelledby="get-access" className="panel">
      <h2 id="get-access">How to get access</h2>
      <p className="muted">Access comes from workspace groups. Ask a workspace admin to add you to the group.</p>
      <ul className="access-groups">
        {roles && <li><code>{roles.authors}</code> makes you an author.</li>}
        {roles && <li><code>{roles.reviewers}</code> makes you a reviewer.</li>}
        {config.departments.map((d) => {
          const { readers, quarantine } = access[d] ?? { readers: [], quarantine: false };
          return (
            <li key={d}>
              {readers.map((g, i) => <span key={g}>{i > 0 && " or "}<code>{g}</code></span>)}
              {" "}lets you read live skills in {label(d)}.
              {quarantine && " This department holds imported skills and is for reviewers only."}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function Changing() {
  return (
    <section aria-labelledby="changing" className="panel">
      <h2 id="changing">Changing a skill</h2>
      <p className="muted">Nobody publishes a version from the app, and skills are not edited in it. A change goes through a pull request.</p>
      <ol className="access-steps">{STEPS.map((s) => <li key={s}>{s}</li>)}</ol>
      <p className="hint">Want to try a skill first? <Link to="/drafts/new">Upload a draft</Link>. It is never released.</p>
    </section>
  );
}

export default function Access() {
  const me = useAsync<Me>(() => api.me(), []);
  const config = useAsync<AppConfig>(() => api.config(), []);
  const readable = useAsync<ReadableDepartments>(() => api.readableDepartments(), []);
  if (me.loading || config.loading) return <Loading label="Loading your access" />;
  if (me.error) return <ErrorState error={me.error} onRetry={me.reload} />;
  if (config.error) return <ErrorState error={config.error} onRetry={config.reload} />;
  if (!me.data || !config.data) return null;
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Access</h1>
          <p className="lede">Who can upload, edit and review skills, and how to get the access you need.</p>
        </div>
      </div>
      <YourAccess me={me.data} readable={readable} departments={config.data.departments} />
      <Matrix me={me.data} />
      <HowToGet config={config.data} />
      <Changing />
    </>
  );
}
