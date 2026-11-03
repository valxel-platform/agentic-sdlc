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
  design_review?: DesignReview | null;
  related_features?: SimilarItem[];
  kind?: "feature" | "initiative" | "release";
  initiative?: InitiativeDraft;
  roadmap_fit?: RoadmapFit | null;
  model?: "launch" | "train";
  features?: string[];
  dropped?: string[];
  evidence?: string;
  target_date?: string;
  summary?: string;
  not_duplicate_reason?: string;
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
  release: { components?: string[]; packages?: string[]; production?: boolean; promoted_by?: string;
             flag?: string; flag_on?: boolean | null; release_id?: string };
  initiative_id?: string;
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

export interface AdrRef { id: string; title: string; status: string; lane?: string }
export interface ContractChange { repo: string; current: string; next: string; breaking: boolean; note: string }
export interface DesignReview {
  findings: string[];
  builds_on: AdrRef[];
  supersedes: { adr: string; supersedes: string; title: string }[];
  contracts: ContractChange[];
  related: AdrRef[];
  in_flight: { feature: string; title: string; repos: string[]; contracts?: { repo: string; next: string }[] }[];
  record: { repo?: string | null; sha?: string | null };
  errors: Record<string, string>;
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

export interface Candidate { title: string; description: string; started?: boolean }
export interface InitiativeDraft {
  goal: string; new_goal: { title: string; measure: string } | null; measure: string; horizon: string;
  budget_usd: number; lanes: string[]; release_model: "launch" | "train"; candidates: Candidate[];
}
export interface RoadmapFit {
  unplanned?: boolean;
  initiative?: { id: string; title: string; goal: string; horizon: string; release_model: string };
  budget_usd?: number; spent_usd?: number; remaining_usd?: number; over?: boolean;
}
export interface FeatureRow {
  feature_id: string; title: string; status: "released" | "in_progress" | "stopped" | string; stage: string;
  initiative_id: string; cost_usd: number; flag_on: boolean | null;
}
export interface ThreadBase {
  feature_id: string; kind: "initiative" | "release"; product_id: string; title: string; created_by: string;
  status: string; running: boolean; error: string | null; pending: PendingItem[]; events: FeatureEvent[];
  cost_usd: number; record_pr: { url?: string; error?: string };
}
export interface InitiativeView extends ThreadBase {
  request: string; content: string; draft: InitiativeDraft; findings: string[]; features: FeatureRow[];
  candidates: Candidate[]; budget_usd: number; spent_usd: number; progress: { released: number; total: number };
}
export interface ReleaseView extends ThreadBase {
  model: "launch" | "train"; target_date: string; initiative: string; notes: string; evidence: string;
  approved_by: string; dropped: string[]; features: FeatureRow[];
}
export interface RoadmapInitiative {
  id: string; title: string; goal: string; horizon: string; status: string; budget_usd: number; lanes: string[];
  release_model: string; record: string; spent_usd: number; features: FeatureRow[];
  progress: { released: number; total: number; planned: number };
}
export interface Roadmap {
  product_id: string; record: { repo: string; sha: string; error: string };
  goals: { id: string; title: string; measure: string; period: string }[];
  initiatives: RoadmapInitiative[];
  releases: { id: string; title: string; model: string; status: string; target_date: string; features: string[]; record: string }[];
  unplanned: FeatureRow[];
}

export interface SimilarItem {
  kind: "feature" | "capability"; id: string; title: string; status: string; stage: string;
  score: number; duplicate: boolean;
}
export interface Draft {
  product_id: string; title: string; problem: string; users: string; success_metric: string;
  related_capability: string; similar: SimilarItem[]; not_duplicate_reason: string;
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
      msg = typeof j.detail === "string" ? j.detail : j.detail?.message ?? JSON.stringify(j.detail);
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
  initiative: (id: string) => request<InitiativeView>("GET", `/features/${encodeURIComponent(id)}`),
  release: (id: string) => request<ReleaseView>("GET", `/features/${encodeURIComponent(id)}`),
  roadmap: (productId: string) => request<Roadmap>("GET", `/roadmap/${encodeURIComponent(productId)}`),
  createInitiative: (body: { product_id: string; title: string; request: string; measure?: string; horizon?: string }) =>
    request<{ feature_id: string }>("POST", "/initiatives", body),
  startCandidate: (id: string, index: number, not_duplicate_reason = "") =>
    request<{ feature_id: string }>("POST", `/initiatives/${encodeURIComponent(id)}/features`, { index, not_duplicate_reason }),
  createRelease: (body: { product_id: string; title: string; model: string; features: string[]; target_date?: string; initiative?: string }) =>
    request<{ feature_id: string }>("POST", "/releases", body),
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
