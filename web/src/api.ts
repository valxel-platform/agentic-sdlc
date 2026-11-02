export type StationState = "done" | "skipped" | "waiting" | "current" | "stopped" | "todo";
export interface Station { key: string; kind: "phase" | "gate"; state: StationState }

export interface PendingItem {
  interrupt_id: string;
  type: "gate" | "handback" | "escalation" | "external";
  gate?: string;
  wait?: string;
  label?: string;
  stage?: string;
  max_waiver_days?: number;
  blocking?: SecurityFinding[];
  quality?: { suites: QualitySuite[]; waivers: Waiver[] } | null;
  new_repos?: RepoPlan[];
  targets?: Record<string, unknown>[];
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
  diagrams: Diagram[];
  delivery: Record<string, WaitResult>;
  deployments: Record<string, { repo: string; component: string; env: string; release?: string; pipeline?: string[] }>;
  quality: Record<string, { stage: string; status: string; suites: QualitySuite[]; at: string }>;
  waivers: Waiver[];
  new_repos: RepoPlan[];
  runs: Record<string, { repo: string; component: string; run_id: string; sha: string; url?: string; state?: string }>;
  work_tracking: WorkTracking | null;
}

export interface RepoPlan {
  repo: string; role: string; purpose: string; template?: string; visibility?: string;
  ruleset?: { required_approvals?: number; required_checks?: string[] };
  teams?: Record<string, string>; security?: Record<string, boolean>;
  // after provisioning
  status?: string; url?: string; governance_pr?: string | null; steps?: { step: string; ok: boolean; error?: string; detail?: string }[];
  required_checks?: string[]; required_approvals?: number;
}

export interface SecurityFinding {
  suite: string; tool: string; rule: string; severity: string; title: string; repo: string; url: string; fingerprint: string;
}

export interface QualitySuite {
  stage?: string; suite: string; kind: "test" | "security"; repo: string; state: string;
  passed?: number | null; failed?: number | null; coverage?: number | null;
  findings?: SecurityFinding[] | number; waived?: string[] | number; reasons?: string[]; required?: boolean;
}

export interface Waiver {
  fingerprint: string; suite: string; severity: string; title: string; repo: string; approver: string; reason: string; expires: string;
}

export interface WaitResult {
  status: "success" | "failure"; wait: string; reason?: string; at: string;
  results: { repo?: string; component?: string; state: string; message?: string; url?: string; env?: string;
             checks?: { name: string; state: string; url: string }[] }[];
}

export interface Diagram {
  provider: string; title: string; version: number; url: string; edit_url?: string;
  file?: string; file_url?: string; stub?: boolean; at: string;
}

export interface WorkItem {
  item: string; kind: "epic" | "story" | "task"; key: string; url: string; summary: string;
  status: "todo" | "in_progress" | "done" | "cancelled";
}

export interface WorkTracking { provider: string; project: string; items: WorkItem[]; error: string | null }

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
  syncWork: (id: string) => request<unknown>("POST", `/features/${encodeURIComponent(id)}/sync`, {}),
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
