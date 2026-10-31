export type StationState = "done" | "skipped" | "waiting" | "current" | "stopped" | "todo";
export interface Station { key: string; kind: "phase" | "gate"; state: StationState }

export interface PendingItem {
  interrupt_id: string;
  type: "gate" | "handback" | "escalation";
  gate?: string;
  phase?: string;
  lane?: string;
  lanes?: string[];
  feature_id?: string;
  product_id?: string;
  title?: string;
  approvers: string[];
  can_decide: boolean;
  allowed?: string[];
  artifact?: { id: string; kind: string } | null;
  preview?: string;
  score?: number | null;
  loops?: number | null;
  cost_usd?: number;
  budget_usd?: number;
  risk_tier?: string | null;
  pull_requests?: string[];
  reason?: string;
  findings?: string[];
  constitution_version?: string;
}

export interface FeatureSummary {
  feature_id: string;
  product_id: string;
  title: string;
  created_by?: string;
  status: string;
  running: boolean;
  error?: string | null;
  rail: Station[];
  risk_tier?: string | null;
  lanes_touched?: string[];
  cost_usd?: number;
  budget_usd?: number;
  pending: PendingItem[];
  updated_at?: string | null;
}

export interface GateDecision {
  gate: string; phase: string; lane?: string; decision: string; approver: string;
  feedback: string; active_minutes: number; at: string;
}

export interface FeatureEvent { at: string; node: string; kind: string; detail?: string; [k: string]: unknown }

export interface Feature extends FeatureSummary {
  description: string;
  constitution_version: string;
  artifacts: Record<string, string>;
  pull_requests: Record<string, { repo: string; number: number; url: string; state: string; merged_by?: string; review_score?: number; task_id?: string }>;
  change_set: { id?: string; merge_order?: string[]; published?: string[] };
  release: { components?: string[]; packages?: string[]; production?: boolean; promoted_by?: string };
  gate_decisions: GateDecision[];
  scores: Record<string, number>;
  loop_count: Record<string, number>;
  human_active_minutes: number;
  usage: { calls: number; cache_hit_rate: number; est_cost_usd: number; cost_by_phase: Record<string, number> };
  events: FeatureEvent[];
}

export interface Me {
  login: string; name: string; avatar_url: string; orgs: string[]; teams: string[];
  lanes: { id: string; name: string }[];
}

export interface Draft {
  product_id: string; title: string; problem: string; users: string; success_metric: string;
  related_capability: string;
}
export interface IntakeSession {
  id: string; owner: string; lanes: string[]; draft: Draft; ready: boolean; missing: string[];
  feature_id: string; created_at: string; messages: { role: "user" | "agent"; text: string; at: string }[];
}

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method,
    credentials: "same-origin",
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const j = await res.json();
      msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch { /* not JSON */ }
    throw new ApiError(res.status, msg);
  }
  return res.status === 204 ? (undefined as T) : res.json();
}

export const api = {
  authConfig: () => request<{ mode: "github" | "dev"; teams?: string[] }>("GET", "/auth/config"),
  devLogin: (login: string, teams: string[], orgs: string[]) => request<Me>("POST", "/auth/dev-login", { login, teams, orgs }),
  logout: () => request<{ ok: boolean }>("POST", "/auth/logout", {}),
  me: () => request<Me>("GET", "/me"),
  features: () => request<FeatureSummary[]>("GET", "/features"),
  products: () => request<Record<string, { name?: string; kind: string; ok: boolean }>>("GET", "/products"),
  feature: (id: string) => request<Feature>("GET", `/features/${encodeURIComponent(id)}`),
  artifact: (id: string, kind: string) =>
    request<{ id: string; content: string; version: number; constitution_version: string }>(
      "GET", `/features/${encodeURIComponent(id)}/artifacts/${kind}`),
  inbox: (mine: boolean) => request<PendingItem[]>("GET", `/inbox${mine ? "?mine=true" : ""}`),
  decide: (featureId: string, interruptId: string, body: Record<string, unknown>) =>
    request<{ accepted: boolean }>("POST", `/features/${encodeURIComponent(featureId)}/decisions/${interruptId}`, body),
  intakeStart: () => request<IntakeSession>("POST", "/intake", {}),
  intakeList: () => request<IntakeSession[]>("GET", "/intake"),
  intake: (id: string) => request<IntakeSession>("GET", `/intake/${id}`),
  intakeSay: (id: string, text: string) => request<IntakeSession>("POST", `/intake/${id}/messages`, { text }),
  intakeDraft: (id: string, changes: Partial<Draft>) => request<IntakeSession>("PATCH", `/intake/${id}/draft`, changes),
  intakeSubmit: (id: string) => request<IntakeSession>("POST", `/intake/${id}/submit`, {}),
};
