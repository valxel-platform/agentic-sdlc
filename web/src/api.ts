export type StationState = "done" | "skipped" | "waiting" | "current" | "stopped" | "todo";
export interface Station { key: string; kind: "phase" | "gate"; state: StationState }

/** AWS services a design asks for (VX-19): what G4 approves and G6 binds in production. */
export interface AwsResource {
  name: string; type: string; service: string; stateful: boolean;
  access: Record<string, string[]>; parameters: Record<string, unknown>;
  sizes: Record<string, string>; monthly_usd: Record<string, number>;
  alarms?: { name: string; severity: string; says: string }[];
}
export interface AwsResources {
  resources: AwsResource[]; monthly_usd: Record<string, number>;
  region: string; accounts: Record<string, string>; note: string;
}

export interface PendingItem {
  interrupt_id: string;
  // At the product gate: the architecture's AWS resources priced per environment.
  monthly_usd?: Record<string, number>;
  type: "gate" | "handback" | "escalation" | "external";
  gate?: string;
  wait?: string;
  label?: string;
  stage?: string;
  max_waiver_days?: number;
  blocking?: SecurityFinding[];
  quality?: { suites: QualitySuite[]; waivers: Waiver[] } | null;
  new_repos?: RepoPlan[];
  aws_resources?: AwsResources | null;
  // At G6: the traceability matrix. When it blocks, approving needs an override and its reason.
  traceability?: ReleaseTraceability | null;
  // At a shared lane's G6: each release candidate, and whether its workflow published it.
  packages?: ReleaseCandidate[] | null;
  // At G6: each agent repo's evals by stage, against the last release of the same agent.
  agent_evals?: AgentEvals[] | null;
  // At G5 and G6: the coder and reviewer sessions, each with a link to its trace.
  agent_sessions?: AgentSession[] | null;
  // Why the last decision was refused (asked again).
  refused?: string;
  // At G6: CloudWatch alarms firing before production on what the feature touches.
  alarms_firing?: { env: string; resource: string; alarm: string; severity: string; says: string;
                    since: string; url: string; title: string }[];
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
  severity?: string;
  actions?: IncidentAction[];
  signals?: Signal[];
  firing?: number;
  suspects?: Suspect[];
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
  /** G5 and G6: the performance test each repo of the change needed, and why. */
  performance?: PerformanceDecision[] | null;
  // G6: the feature's product events, counted in staging (outcome loop)
  instrumentation?: InstrumentationCheck | null;
  /** People's earlier corrections to this phase in this feature, oldest first. */
  history?: Correction[];
  /** How many times this phase was sent back. */
  revisions?: number;
  /** Sent back often enough that taking it over is suggested. */
  stuck?: boolean;
  /** Hand-backs and escalations: the document to edit (the agents' last draft). */
  content?: string;
  /** What the agents assumed, changed, traded off and left open (open questions first). */
  agent_notes?: AgentNote[];
  /** The workflow profile: feature or express. */
  profile?: Profile;
  /** Express at G4: the spec it approves too (there is no G2). */
  spec_preview?: string;
}

export type Profile = "feature" | "express";

export const PROFILE_LABEL: Record<Profile, string> = {
  feature: "Feature", express: "Express",
};

export const PROFILE_HELP: Record<Profile, string> = {
  feature: "Every phase and gate: discovery, spec, UX design and architecture, then build and release.",
  express: "A small, low-risk change: no discovery or UX design, and the spec is approved with the architecture at G4.",
};

export interface AgentNote {
  kind: "open" | "changed" | "assumed" | "traded off";
  text: string;
  phase?: string;
  task?: string;
  repo?: string;
}

export interface TracePr { key: string; repo: string; number: number | null; url: string; task: string; merged: boolean }

export interface TraceCase {
  id: string; req: string; kind: string; what: string; ref: string; via: string;
  status: "passed" | "failed" | "written" | "missing" | "pending" | "manual" | "unknown";
}

export interface TraceRequirement {
  id: string; type: string; text: string; goals: string[]; design: Record<string, string>; deferred: string | null;
  tasks: string[]; cases: TraceCase[]; test: string; prs: TracePr[]; repos: string[];
  acceptance: Record<string, string>; deployed: Record<string, string>;
}

export interface ReleaseTraceability {
  complete: boolean; gaps: string[]; deferred: { id: string; reason: string }[];
  drift: string[]; drift_error: string; drift_blocks: boolean; blocks: boolean; override_approvers: string[];
}

export interface Trace {
  feature_id: string;
  title: string;
  profile: Profile;
  metrics: { id: string; text: string; goals: string[] }[];
  goals: { id: string; text: string; metrics: string[]; requirements: string[] }[];
  criteria: TraceRequirement[];
  nfrs: TraceRequirement[];
  deferred: { id: string; reason: string }[];
  complete: boolean;
  decisions: { id: string; text: string }[];
  repos: { repo: string; role: string; tasks: string[]; prs: TracePr[]; deployed: { env: string; release: string } | null }[];
  release: { flag: string; percent: number | null; production: boolean; merged: boolean };
  incidents: { id: string; title: string; status: string; severity: string; touch: string }[];
  gaps: string[];
}

export interface PerformanceDecision {
  repo: string; role: string; level: "none" | "smoke" | "full"; reasons: string[]; soak: boolean;
}

export interface Correction {
  gate: string; decision: string; approver: string; reason: string; feedback: string; at: string;
}

export interface Lesson {
  id: string; phase: string; text: string; check: boolean; sources: string[]; pr?: string; feature_id?: string;
  why?: "remember" | "takeover" | "repeat";
}

export interface LessonsOverview {
  product_id: string; approved: Lesson[]; proposed: Lesson[]; corrections: number;
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

export interface EvalCounts { suite: string; passed: number; failed: number; rate: number | null; state: string }
export interface AgentEvals {
  repo: string; stages: Record<string, EvalCounts>; previous: Record<string, EvalCounts>;
  previous_feature: string; change: Record<string, number>;
}

export interface AgentSession {
  trace_id: string; url: string; at: string; phase: string; role: string; runtime: string; model: string;
  task: string; attempt: string; calls: number; duration_s: number; tokens: number; cost_usd: number;
  // Installed skills the session loaded by itself, beyond the ones its phase gave it.
  extra_skills?: string[];
}

export interface Feature extends FeatureSummary {
  attachments?: Attachment[];
  description: string;
  agent_sessions?: AgentSession[];
  // "canary": the feature's agents run with the candidate config.
  config_variant?: string;
  profile?: Profile;
  track?: string;
  constitution_version: string;
  artifacts: Record<string, string>;
  pull_requests: Record<string, { repo: string; number: number; url: string; state: string; merged_by?: string; review_score?: number; task_id?: string }>;
  change_set: { id?: string; merge_order?: string[]; published?: string[] };
  release: { components?: string[]; packages?: string[]; production?: boolean; promoted_by?: string;
             flag?: string; flag_on?: boolean | null; release_id?: string; flag_percent?: number;
             rollout_plan?: { steps: number[]; step: number; bake_minutes: number; stopped?: boolean } };
  initiative_id?: string;
  flag?: string;
  gate_decisions: GateDecision[];
  scores: Record<string, number>;
  loop_count: Record<string, number>;
  human_active_minutes: number;
  // AWS cost G4 approved, and what the latest review after release found (null: no AWS services).
  run_cost?: { approved_monthly_usd: Record<string, number>; projected_monthly_usd: Record<string, number> | null;
               reviewed_at: string | null; findings: string[]; incidents: string[] } | null;
  usage: { calls: number; cache_hit_rate: number; est_cost_usd: number; cost_by_phase: Record<string, number> };
  events: FeatureEvent[];
  diagrams: Diagram[];
  delivery: Record<string, WaitResult>;
  deployments: Record<string, { repo: string; component: string; env: string; release?: string; pipeline?: string[]; kind?: string }>;
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

export interface IncidentAction {
  id: string; type: "flag_off" | "rollback" | "hotfix" | "intent_change" | "monitor"; text: string;
  feature_id?: string; component?: string; repo?: string; change?: string; scope?: string; intent?: string;
  ok?: boolean; result?: string; by?: string; at?: string;
}
export interface Signal {
  source: string; status: "firing" | "resolved"; severity: string; title: string;
  detail: string; fingerprint: string; url: string; at: string; component: string;
  // Why it joined this incident (same trace, call graph or release), when it came from elsewhere.
  labels?: Record<string, string>;
}
export interface Suspect { feature_id: string; title: string; released_at: string; flag: string; flag_on: boolean | null }
export interface IncidentView extends Omit<ThreadBase, "kind"> {
  kind: "incident"; severity: string; component: string; opened_at: string; triage: string;
  actions: IncidentAction[]; taken: IncidentAction[]; links: Record<string, string>; postmortem: string;
  signals: Signal[]; firing: number; suspects: Suspect[];
  root?: { component: string; why: string };
  paging?: PageSend[];
  analysis?: Analysis;
  escalated?: boolean;
}
/** OpenChoreo's agent's report on an incident: root causes (SRE agent) or right-sizing (FinOps agent). */
export interface Analysis {
  kind?: "rca" | "cost"; status?: "pending" | "completed" | "failed" | "timed_out" | "skipped"; why?: string; report_id?: string;
  report?: {
    summary?: string;
    result?: {
      type: string; explanation?: string;
      root_causes?: { summary: string; confidence: "high" | "medium" | "low"; analysis?: string }[];
      excluded_causes?: { description: string; rationale?: string }[];
      recommendations?: { recommended_actions?: { description: string }[] };
    };
    overprovisioning?: { analysis?: string; recommendation?: { cpu_request: string; cpu_limit: string; memory_request: string;
                         memory_limit: string; estimated_savings: number; currency: string; rationale: string } };
  } | null;
}
/** What happened to each PagerDuty send for an incident (ops/paging.py). */
export interface PageSend {
  at: string; action: "heads_up" | "urgent" | "acknowledge" | "resolve"; ok: boolean; detail: string; dedup_key: string;
}
export interface IncidentRow {
  feature_id: string; product_id: string; title: string; severity: string; status: string; component: string;
  opened_at: string; firing: number; links: Record<string, string>;
}

export interface PilotFeature {
  feature_id: string; title: string; status: string; cost_usd: number; shadow_cost_usd: number;
  human_minutes: number; agent_minutes: number; human_share: number | null; first_time_approvals: number; gates: number;
  loops: Record<string, number>; ci_retries: number; escalations: number;
}
interface Agreement { compared: number; agreed: number }
export interface CriticAccuracy {
  passed_then_approved: number; passed_then_revised: number; failed_then_fixed: number; failed_then_vouched: number;
  false_pass_rate: number | null; false_fail_rate: number | null; missed_because: Record<string, number>;
}
export interface ConfigScore {
  features: number; done: number; first_time_approval_rate: number | null; loops_per_feature: number;
  critic_false_pass_rate: number | null; pre_merge_fixes: number; post_merge_fixes: number; incidents: number;
  traceability_overrides: number; cost_per_feature_usd: number; human_share: number | null;
}
export interface PilotReport {
  product_id: string; count: number; done: number; features: PilotFeature[];
  critic_accuracy: Record<string, CriticAccuracy>;
  by_config: Record<string, ConfigScore>;
  estimated_minutes: { pr_review: number; pr_comment: number };
  totals: {
    cost_usd: number; shadow_cost_usd: number; human_minutes: number; agent_minutes: number; human_share: number | null;
    target_human_share: number; first_time_approval_rate: number | null; escalations: number; ci_retries: number;
    revise_reasons: Record<string, number>;
    shadow_auto_approval: { gates: number; eligible: number; agreed: number; would_have_missed: number; minutes_saved: number };
    shadow_critic: Agreement; shadow_reviewer: Agreement; old_score_agreed: Agreement;
  };
}
export interface UnmatchedSignal {
  source: string; title: string; component?: string; product_id?: string; severity?: string; env?: string;
  status?: string; fingerprint: string; url?: string; labels?: Record<string, string>; reason: string; received_at: string;
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
/** Product inception (vrixel_sdlc.inception): a vision to a brief, an architecture and a roadmap. */
export interface InceptionReport {
  doc: string; status: "passed" | "escalated"; attempts: number; findings: string[];
  checklist: { id: string; must: boolean; met: boolean; evidence: string; why: string }[];
  cost_usd: number; monthly_usd: Record<string, number>; data_class: string;
}
export interface InceptionSetupStep { step: string; ok: boolean; url?: string; repo?: string; error?: string;
  why?: string; line?: string; skipped?: boolean; teams?: string[]; result?: string }
export interface InceptionView extends Omit<ThreadBase, "kind"> {
  kind: "inception";
  vision: { name: string; org: string; vision: string; goals: string[]; users: string[]; constraints: string[];
            budget_usd: number; product_id: string; existing: boolean };
  brief: string; architecture: string; roadmap: string;
  reports: Partial<Record<"brief" | "architecture" | "roadmap", InceptionReport>>;
  setup: { product_id?: string; governance_repo?: string; steps?: InceptionSetupStep[]; ok?: boolean };
  council: string[];
}
export interface InceptionSummary {
  id: string; title: string; org: string; existing: boolean; product_id: string; status: string;
  created_by: string; cost_usd: number; running: boolean;
}
export interface InceptionIn {
  name: string; org: string; vision: string; goals: string[]; users: string[]; constraints: string[];
  budget_usd: number; product_id: string;
}

export interface RoadmapInitiative {
  id: string; title: string; goal: string; horizon: string; status: string; budget_usd: number; lanes: string[];
  release_model: string; record: string; spent_usd: number; features: FeatureRow[];
  progress: { released: number; total: number; planned: number };
  outcome?: { goal_id: string; status: OutcomeStatus; why?: string; progress?: number; expected?: number | null } | null;
}
export interface GoalMetric {
  goal_id: string; source: "sql" | "prometheus" | "posthog" | "manual"; unit: string; direction: "up" | "down";
  baseline: number | null; target: number | null; by: string;
}
export type OutcomeStatus = "on_track" | "flat" | "off_track" | "no_data";
export interface GoalOutcome {
  metric: GoalMetric; latest: number | null; status: OutcomeStatus; why?: string; progress?: number; expected?: number | null;
  trend: { end: string; value: number | null; status: string; note: string }[];
}
export interface ReadingRow { start: string; end: string; status: string; value: number | null; n: number | null; note: string }
export interface Readout {
  feature_id: string; goal_id: string; window_days: number; released_at: string; source: string; unit: string;
  direction: string; before: ReadingRow; after: ReadingRow; change: number | null; toward: boolean | null; caveat: string;
}
export interface WantedNumber { goal_id: string; feature_id: string; window_days: number; start: string; end: string; unit: string }
export interface FeatureOutcome {
  feature_id: string; product_id: string; goals: string[]; metrics: Record<string, GoalMetric>; released_at: string;
  instrumentation: InstrumentationCheck | null; readouts: Readout[];
  upcoming: { goal_id: string; window_days: number; due: string }[];
}
export interface Roadmap {
  product_id: string; record: { repo: string; sha: string; error: string };
  goals: { id: string; title: string; measure: string; period: string; outcome?: GoalOutcome | null }[];
  outcomes?: { wanted: WantedNumber[]; windows: number[]; readouts: Record<string, Readout[]>; min_cell?: number };
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
  // The intent changes what people see: the experience round applies, and its brief.
  ui: boolean; experience: Partial<Record<ExperienceKey, string>>;
}
export type ExperienceKey = "where" | "main_task" | "context" | "look";
export const EXPERIENCE_DELEGATED = "Left to the design agent";
/** A file attached to an intent: kept as the strictest data class until the intent is submitted. */
export interface Attachment {
  id: string; name: string; kind: "image" | "pdf" | "text"; size: number; read_by_agents: boolean;
  mime?: string; uploaded_at?: string;
}
export const ATTACH_ACCEPT = ".png,.jpg,.jpeg,.gif,.webp,.pdf,.txt,.md,.csv,.json";
export interface IntakeSession {
  id: string; owner: string; lanes: string[]; draft: Draft; ready: boolean; missing: string[]; attachments: Attachment[];
  feature_id: string; created_at: string;
  messages: { role: "user" | "agent"; text: string; at: string; attachment?: string; options?: string[] }[];
}

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method,
    credentials: "same-origin",
    headers: body !== undefined ? { "Content-Type": "application/json" } : { "X-Vrixel-Client": "web" },
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

export interface CostCategory { category: string; label: string; usd: number; basis: string[]; provisional: boolean }
export interface FeatureCost {
  feature_id: string; product_id: string; title: string; status: string;
  build: { total_usd: number; by_category: CostCategory[]; provisional: boolean; llm_budget_usd?: number | null };
  run_30d: { total_usd: number; by_category: CostCategory[] };
}
export interface ProductCost {
  product_id: string; month: string; provisional: boolean;
  totals: { run_usd: number; build_usd: number; total_usd: number; build_to_date_usd: number };
  run: { by_category: CostCategory[]; by_env: Record<string, number> };
  build: { by_category: CostCategory[] };
  features: { feature_id: string; title: string; status: string; build_to_date_usd: number; build_month_usd: number;
              run_month_usd: number; approved_monthly_usd?: number | null }[];
}

export interface ChangeRecord {
  id: string; at: string; product_id: string; env: string; kind: "release" | "flag" | "rollback" | "policy" | "external";
  type: "standard" | "emergency" | "automatic"; action: string; summary: string;
  targets: { kind: string; name: string; release?: string; on?: boolean; percent?: number }[];
  feature_id: string; release_id: string; incident_id: string; requested_by: string; approved_by: string[];
  approvals: { gate: string; lane: string; approver: string; at: string }[];
  evidence: { pull_requests?: string[]; quality?: Record<string, Record<string, unknown>>; waivers?: unknown[]; triage?: string };
  outcome: "done" | "failed"; detail: string; caused_incidents: string[]; hash: string; prev_hash: string;
  receipt?: { status: "queued" | "closed" | "open" | "failed" | "not_sent"; number?: string; instance?: string; detail?: string } | null;
}
export interface LookbackMonth {
  month: string; incidents: number; by_severity: Record<string, number>; components: Record<string, number>;
  fixed_by_tool: number; escalated: number; open: number; changes: Record<string, number>; failed_changes: number;
  releases: number; releases_causing_incidents: number; change_failure_rate: number | null;
  recover_minutes_median: number | null; recover_minutes_max: number | null;
}
export interface Lookback {
  product_id: string; months: LookbackMonth[];
  total: { incidents: number; fixed_by_tool: number; escalated: number; releases: number; releases_causing_incidents: number;
           failed_changes: number; change_failure_rate: number | null };
  causes: { change_id: string; summary: string; at: string; incidents: string[] }[];
}
export interface InventoryItem {
  kind: "component" | "resource" | "store_app"; name: string; type?: string; env: string; state: string; release: string;
  size?: string; feature_id: string; repo?: string; cost_30d_usd?: number; planned_monthly_usd?: number | null;
  outputs?: { name: string; value: string }[]; secrets?: { output: string; name: string; key: string }[];
  ids: { openchoreo?: string; binding?: string; kubectl?: string; aws_account?: string; aws_region?: string; arn?: string; stores?: string };
}
export interface EnvSummary {
  components: number; resources: number; store_apps: number; not_ready?: number; cost_30d_usd: number; planned_monthly_usd: number;
}
export interface Inventory {
  product_id: string; at: string; project: string; namespace: string; pipeline: string[]; items: InventoryItem[];
  summary: { per_env: Record<string, EnvSummary>; production: EnvSummary; lower: EnvSummary };
  unmanaged: { env: string; account: string; arn: string; resource_tag: string; feature: string }[];
  aws: string; problems: string[];
}

export const api = {
  changes: (productId: string, since?: string) =>
    request<ChangeRecord[]>("GET", `/changes?product_id=${encodeURIComponent(productId)}${since ? `&since=${since}` : ""}`),
  verifyChanges: () => request<{ ok: boolean; records: number; broken_at: string | null }>("GET", "/changes/verify"),
  lookback: (productId: string, months = 6) =>
    request<Lookback>("GET", `/reports/lookback?product_id=${encodeURIComponent(productId)}&months=${months}`),
  inventory: (productId: string) => request<Inventory>("GET", `/inventory/${encodeURIComponent(productId)}`),
  featureCost: (id: string) => request<FeatureCost>("GET", `/features/${encodeURIComponent(id)}/cost`),
  productCost: (productId: string, month?: string) =>
    request<ProductCost>("GET", `/costs/${encodeURIComponent(productId)}${month ? `?month=${month}` : ""}`),
  authConfig: () => request<{ mode: "github" | "dev"; teams?: string[] }>("GET", "/auth/config"),
  devLogin: (login: string, teams: string[], orgs: string[]) => request<Me>("POST", "/auth/dev-login", { login, teams, orgs }),
  logout: () => request<{ ok: boolean }>("POST", "/auth/logout", {}),
  me: () => request<Me>("GET", "/me"),
  features: () => request<FeatureSummary[]>("GET", "/features"),
  products: () => request<Record<string, { name?: string; kind: string; ok: boolean; data_class?: string; profiles?: Profile[];
                                            default_profile?: Profile }>>("GET", "/products"),
  syncWork: (id: string) => request<unknown>("POST", `/features/${encodeURIComponent(id)}/sync`, {}),
  feature: (id: string) => request<Feature>("GET", `/features/${encodeURIComponent(id)}`),
  initiative: (id: string) => request<InitiativeView>("GET", `/features/${encodeURIComponent(id)}`),
  inception: (id: string) => request<InceptionView>("GET", `/features/${encodeURIComponent(id)}`),
  inceptions: () => request<InceptionSummary[]>("GET", "/inceptions"),
  createInception: (body: InceptionIn) => request<{ feature_id: string }>("POST", "/inceptions", body),
  release: (id: string) => request<ReleaseView>("GET", `/features/${encodeURIComponent(id)}`),
  incident: (id: string) => request<IncidentView>("GET", `/features/${encodeURIComponent(id)}`),
  mergeIncident: (id: string, into: string, reason: string) =>
    request<{ merged: string; into: string }>("POST", `/incidents/${encodeURIComponent(id)}/merge`, { into, reason }),
  splitSignal: (id: string, fingerprint: string) =>
    request<{ new_incident: string | null }>("POST", `/incidents/${encodeURIComponent(id)}/split`, { fingerprint }),
  incidents: () => request<IncidentRow[]>("GET", "/incidents"),
  unmatchedSignals: () => request<{ count: number; signals: UnmatchedSignal[] }>("GET", "/signals/unmatched"),
  reportProblem: (body: { product_id: string; title: string; description: string; severity: string; component?: string }) =>
    request<{ feature_id: string }>("POST", "/incidents", body),
  roadmap: (productId: string) => request<Roadmap>("GET", `/roadmap/${encodeURIComponent(productId)}`),
  recordReading: (productId: string, goalId: string, body: { value: number; start: string; end: string; n?: number; k?: number }) =>
    request<{ reading: ReadingRow; readouts: Readout[] }>("POST",
      `/products/${encodeURIComponent(productId)}/goals/${encodeURIComponent(goalId)}/readings`, body),
  featureOutcome: (id: string) => request<FeatureOutcome>("GET", `/features/${encodeURIComponent(id)}/outcome`),
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
  intakeDraft: (id: string, changes: Partial<Omit<Draft, "experience">> & { experience?: Partial<Record<ExperienceKey, string>> }) => request<IntakeSession>("PATCH", `/intake/${id}/draft`, changes),
  intakeAttach: async (id: string, file: File): Promise<IntakeSession> => {
    const res = await fetch(`/api/intake/${id}/attachments?name=${encodeURIComponent(file.name)}`, {
      method: "POST", credentials: "same-origin", body: file,
      headers: { "Content-Type": "application/octet-stream", "X-Vrixel-Client": "web" },
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new ApiError(res.status, typeof data.detail === "string" ? data.detail : `Couldn't attach ${file.name}`);
    return data as IntakeSession;
  },
  intakeDetach: (id: string, aid: string) => request<IntakeSession>("DELETE", `/intake/${id}/attachments/${aid}`),
  intakeSubmit: (id: string, pilot = false, profile = "") =>
    request<IntakeSession>("POST", `/intake/${id}/submit`, { pilot, profile }),
  trace: (id: string) => request<Trace>("GET", `/features/${encodeURIComponent(id)}/trace`),
  traceCsvUrl: (id: string) => `/api/features/${encodeURIComponent(id)}/trace.csv`,
  lessons: (productId: string) => request<LessonsOverview>("GET", `/products/${encodeURIComponent(productId)}/lessons`),
  pilotReport: (productId: string) => request<PilotReport>("GET", `/reports/pilot?product_id=${encodeURIComponent(productId)}`),
};

export interface ReleaseCandidate {
  repo: string;
  version: string;
  rc: string;
  status: "published" | "missing" | "unknown";
  package: { type: string; name: string } | null;
}

export interface InstrumentationCheck {
  checked: boolean;
  environment: string;
  fired: Record<string, number>;
  missing: string[];
  events: { event: string; goals: string[]; repo: string }[];
  why?: string;
}
