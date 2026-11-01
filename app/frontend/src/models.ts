import type { CatalogModel, ProfileKind } from "./api/types";

// The groups of eval targets, in the order the Models page and Run eval show them.
export const KIND_ORDER: ProfileKind[] = ["hub_yaml", "ug_model", "external_model", "external_agent"];
export const KIND_LABELS: Record<ProfileKind, string> = {
  hub_yaml: "Release gate", ug_model: "On Unity Gateway", external_model: "External models",
  external_agent: "External agents",
};
// The badge next to a model's name in the Eval matrix.
export const KIND_BADGES: Record<ProfileKind, string> = {
  hub_yaml: "Release gate", ug_model: "Unity Gateway", external_model: "External model", external_agent: "External agent",
};

// A row without a kind is from before the model catalog: a hub.yaml profile.
export const kindOf = (kind: ProfileKind | null | undefined): ProfileKind => kind ?? "hub_yaml";

export function groupByKind<T extends { kind: ProfileKind }>(rows: T[]): [ProfileKind, T[]][] {
  return KIND_ORDER
    .map((k): [ProfileKind, T[]] => [k, rows.filter((r) => r.kind === k)])
    .filter(([, members]) => members.length > 0);
}

// The external agents a reviewer can register, by harness.
export const HARNESS_LABELS: Record<string, string> = {
  "claude-code": "Claude Code", agentcore: "Amazon Bedrock AgentCore", reference: "reference",
};

// What a model points at: its serving endpoint, or its Bedrock model and region. An external agent also shows its
// pinned CLI version or its runtime.
export function targetOf(m: Pick<CatalogModel, "endpoint" | "model_id" | "region"> & Partial<Pick<CatalogModel, "kind" | "agent">>) {
  const model = m.model_id ? `${m.model_id} (${m.region ?? "no region"})` : m.endpoint ?? "-";
  if (m.kind !== "external_agent") return model;
  const version = m.agent?.cli_version;
  const runtime = m.agent?.runtime_arn?.split("/").pop();
  return `${model} · ${version ? `CLI ${version}` : runtime ?? "no settings"}`;
}
