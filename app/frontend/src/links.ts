// MLflow UI paths. They follow the workspace's MLflow UI; check them when the MLflow version changes.
const base = (workspaceUrl: string) => workspaceUrl.replace(/\/+$/, "");

export function mlflowRunUrl(workspaceUrl: string, experimentId: string, runId: string): string {
  return `${base(workspaceUrl)}/ml/experiments/${encodeURIComponent(experimentId)}/runs/${encodeURIComponent(runId)}`;
}

export function traceUrl(workspaceUrl: string, experimentId: string, traceId: string): string {
  return `${base(workspaceUrl)}/ml/experiments/${encodeURIComponent(experimentId)}/traces?selectedEvaluationId=${encodeURIComponent(traceId)}`;
}
