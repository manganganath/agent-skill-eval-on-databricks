import type {
  AppConfig, CaseRow, EvalRunIn, EvalRunOut, EvalRunStatus, JournalRow, MatrixRow, Me, SkillDetail,
  SkillSummary, SwitchOut, TryIn, TryModelResult,
} from "./types";

export class ApiError extends Error {
  constructor(public readonly status: number, public readonly reason: string) {
    super(reason);
    this.name = "ApiError";
  }
}

/** A results list plus the partial-read signals the API sends in headers. */
export interface Listed<T> {
  rows: T[];
  deniedDepartments: string[];
  truncated: boolean;
}

async function send(path: string, init?: RequestInit): Promise<Response> {
  const res = await fetch(`/api${path}`, {
    credentials: "same-origin",
    ...init,
    headers: {
      accept: "application/json",
      ...(init?.body ? { "content-type": "application/json" } : {}),
      ...(init?.headers as Record<string, string> | undefined),
    },
  });
  if (!res.ok) {
    let reason = `http_${res.status}`;
    try {
      const body = await res.json();
      if (body?.detail && typeof body.detail.reason === "string") reason = body.detail.reason;
    } catch {
      // not JSON: keep the http_<status> reason
    }
    throw new ApiError(res.status, reason);
  }
  return res;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await send(path, init);
  try {
    return (await res.json()) as T;
  } catch {
    throw new ApiError(res.status, "bad_response");
  }
}

async function requestList<T>(path: string): Promise<Listed<T>> {
  const res = await send(path);
  let rows: T[];
  try {
    rows = (await res.json()) as T[];
  } catch {
    throw new ApiError(res.status, "bad_response");
  }
  const denied = res.headers.get("X-Eval-Denied-Departments") ?? "";
  return {
    rows,
    deniedDepartments: denied.split(",").map((d) => d.trim()).filter(Boolean),
    truncated: res.headers.get("X-Result-Truncated") === "true",
  };
}

// The signed-in user, asked for once per page load: the header and the pages share one /api/me request. A failed
// request is not kept, so the next caller (a page's Try again, say) asks again.
let mePromise: Promise<Me> | null = null;
function me(): Promise<Me> {
  if (!mePromise) {
    const pending = request<Me>("/me");
    mePromise = pending;
    pending.catch(() => { if (mePromise === pending) mePromise = null; });
  }
  return mePromise;
}

const enc = encodeURIComponent;
const qs = (params: Record<string, string | undefined>) => {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v) p.set(k, v);
  const s = p.toString();
  return s ? `?${s}` : "";
};

export const api = {
  me,
  config: () => request<AppConfig>("/config"),
  skills: (q?: string) => request<SkillSummary[]>(`/skills${qs({ q })}`),
  skill: (fqn: string) => request<SkillDetail>(`/skills/${enc(fqn)}`),
  versions: (fqn: string) => request<JournalRow[]>(`/skills/${enc(fqn)}/versions`),
  matrix: (params: { skill?: string; dept?: string; fqn?: string }) => requestList<MatrixRow>(`/results/matrix${qs(params)}`),
  cases: (runId: string) => requestList<CaseRow>(`/results/cases${qs({ run_id: runId })}`),
  startEval: (body: EvalRunIn) =>
    request<EvalRunOut>("/eval-runs", { method: "POST", body: JSON.stringify(body) }),
  evalRun: (jobRunId: number) => request<EvalRunStatus>(`/eval-runs/${jobRunId}`),
  switchVersion: (fqn: string, toVersion: number) =>
    request<SwitchOut>(`/skills/${enc(fqn)}/switch`, { method: "POST", body: JSON.stringify({ to_version: toVersion }) }),
  tryPrompt: (body: TryIn) =>
    request<TryModelResult[]>("/try", { method: "POST", body: JSON.stringify(body) }),
};
