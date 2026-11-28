import { traceHeaders } from "./telemetry";

export type StationState = "done" | "skipped" | "waiting" | "current" | "stopped" | "todo";
export interface GateConfidence {
  level: "high" | "medium" | "low" | "unknown";
  decisions: number;
  held: number;
  lower_bound: number;
  checks: string[];
  says: string;
}

/** Safe mode (valxel_sdlc.safe_mode): why every gate of this product goes to a person, read in full. */
export interface SafeMode {
  on: boolean;
  since: string;
  says: string;
  phases: string[];
  normal_checks: number;
  needed: number;
  fix?: { at: string; by: string; note: string } | null;
}

/** In safe mode, a second critic reads the same work; what it said beside the critic. */
export interface SecondCritic {
  model: string;
  agrees: boolean;
  passed?: boolean;
  says: string;
}

/** The tool's own health (valxel_sdlc.health): its agents on real work, against their baseline. */
export interface HealthMeasure {
  since: string;
  until: string;
  features: number;
  phases: Record<
    string,
    {
      runs: number;
      escalation_rate: number | null;
      stall_rate: number | null;
      stalls: number;
      loops_mean: number | null;
      loops_p90: number | null;
    }
  >;
  critic: Record<
    string,
    { passed: number; false_pass: number | null; failed: number; false_fail: number | null }
  >;
  gates: Record<string, { decided: number; first_time_approval: number | null }>;
  escalation_reasons: Record<string, number>;
  stalls: Record<string, number>;
  reopened: Record<string, number>;
  cost_by_phase_role: Record<string, number>;
  agent_minutes_by_phase_role: Record<string, number>;
  cost_per_feature: { features: number; mean: number | null; max: number | null };
}

export interface HealthAlert {
  key: string;
  rule: string;
  scope: string;
  value: number | null;
  baseline: number | null;
  says: string;
}

export interface SafeModeStatus extends SafeMode {
  hours: number;
  ready: boolean;
  causes: { source: string; rule: string; scope: string; says: string; at: string }[];
  checks: { at: string; source: string; normal: boolean | null; counted: boolean; says: string }[];
  approvals_to_review: { feature_id: string; gate: string; at: string }[];
}

export interface ToolHealth {
  at: string;
  product_id?: string;
  window_days?: number;
  baseline_days?: number;
  current: HealthMeasure;
  baseline: HealthMeasure;
  alerts: HealthAlert[];
  what_changed_summary?: string;
  /** One product: its safe mode or null. Across products: every product in safe mode. */
  safe_mode?: SafeModeStatus | Record<string, SafeModeStatus> | null;
  queue?: { queued: number; running: number; oldest_s: number } | null;
  dead_letters?: { count: number; recent: { at: string; thread_id: string; attempts: number }[] };
  drift?: { at: string; regressions: string[]; anchor_cases: number } | null;
}

export interface DesignIssue {
  task: string;
  by: "coder" | "reviewer";
  document: "spec" | "design" | "architecture";
  section: string;
  problem: string;
  evidence: string;
}

export interface Station {
  key: string;
  kind: "phase" | "gate";
  state: StationState;
}

/** AWS services a design asks for (VX-19): what G4 approves and G6 binds in production. */
export interface AwsResource {
  name: string;
  type: string;
  service: string;
  stateful: boolean;
  access: Record<string, string[]>;
  parameters: Record<string, unknown>;
  sizes: Record<string, string>;
  monthly_usd: Record<string, number>;
  alarms?: { name: string; severity: string; says: string }[];
}
export interface AwsResources {
  resources: AwsResource[];
  monthly_usd: Record<string, number>;
  region: string;
  accounts: Record<string, string>;
  note: string;
}

/** The top of a gate (valxel_sdlc.decisions). */
export interface DecisionBriefData {
  summary: { lines: string[]; by: "agent" | "document" };
  version?: number;
  changed?: { from_version: number; asked: string; lines: { op: " " | "-" | "+" | "…"; text: string }[] };
}
export interface LineComment {
  quote: string;
  text: string;
}
/** G3: the UX spec's flow as one clickable prototype (valxel_sdlc.prototype), or for older features
 * a mockup per screen (valxel_sdlc.mockups). */
export interface MockupSet {
  prototype?: boolean;
  screens: {
    id: string;
    name: string;
    app: string;
    phone: boolean;
    /** The screen as it is today in staging (valxel_sdlc.current_screens): which widths there are. */
    today?: { desktop: boolean; phone: boolean } | null;
    /** No such screen today: the feature adds it. */
    new?: boolean;
  }[];
  findings: string[];
  version?: number;
}
/** G6: each screen as built in staging beside its approved mockup (valxel_sdlc.screens). */
export interface ScreensAtRelease {
  scanned: boolean;
  prototype?: boolean;
  state: string;
  screens: {
    id: string;
    name: string;
    app: string;
    staging?: { route?: string; result?: string; desktop?: string; phone?: string };
    violations: { fingerprint?: string; rule: string; severity: string; title: string }[];
  }[];
}
/** G4: which repos the architect read, at which commit. */
export interface CodeRead {
  checked: boolean;
  repos?: Record<string, { sha?: string; skipped?: string }>;
}
/** G4: what a design's added load does to one component's approved maximum. */
export interface CapacityChange {
  repo: string;
  busy_from?: number;
  busy_to?: number;
  unit?: string;
  max_from?: number;
  max_to?: number;
  error?: string;
}
export interface CapacitySummary {
  tier: number;
  stages: Record<string, { monthly_usd: number; monthly_usd_max: number }>;
  fixed: { monthly_usd: number; unpriced: string[] };
  findings: string[];
}
/** A governance pull request an approval opened, mergeable from the tool. */
export interface RecordPr {
  key: string;
  label: string;
  url: string;
  merged: boolean;
  repo: string;
  number: number;
}
export interface PendingItem {
  // In the Decisions inbox: grouping, and whether it may be approved with others.
  product_name?: string;
  initiative_id?: string;
  low_risk?: boolean;
  low_risk_why?: string;
  // Seconds this person has had it open and in view; whether it may join a batch (read, or express).
  read_s?: number;
  batch_ready?: boolean;
  batch_why?: string;
  // G2: whether UX design runs. The fit check: what doesn't fit, and which document should change.
  ux_design?: { runs: boolean; why: string } | null;
  /** G1: how the feature ships (T-96): its current answer, and the releases it could join. */
  ship?: { current: Ship; releases: OpenRelease[] } | null;
  /** G4: a feature in a launch that won't have a flag waits like a train (T-97). */
  flag_warning?: string;
  /** Inception's product gate: the first initiative is the launch; its date and must-haves (T-100). */
  launch?: {
    initiative: string;
    title: string;
    features: string[];
    must: string[];
    target_date: string;
  } | null;
  /** The release gate: how many of its features are ready, and where the rest are (T-99). */
  readiness?: ReleaseReadiness | null;
  mockups?: MockupSet | null;
  /** G5: the change set's environment, to use the change as built before it merges. */
  try_it?: Preview | null;
  /** G5: each PR's independent review (T-144). */
  independent_reviews?: IndependentReview[] | null;
  /** G1: a rough sketch of the main screens the brief implies (valxel_sdlc.sketch). */
  sketch?: { version: number; findings: string[] } | null;
  screens?: ScreensAtRelease | null;
  code_read?: CodeRead | null;
  // G4: the approved maximums the design's added load raises.
  capacity_change?: CapacityChange[] | null;
  // The product gate: the capacity plan's cost per production stage, and the product's own fixed costs.
  capacity?: CapacitySummary | null;
  fit?: { to: "design" | "architecture"; text: string }[];
  interrupt_id: string;
  // At the product gate: the architecture's AWS resources priced per environment.
  monthly_usd?: Record<string, number>;
  // At an initiative review: the features less effort to remove than keep, and the default next review.
  retire_candidates?: { feature_id: string; title: string; flag: string; why: string }[];
  next_review?: string;
  goal_status?: OutcomeStatus;
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
  // Separation of duties: whose rewritten document this approval is for; they can't approve it.
  separation?: string;
  waiver_allowed?: boolean;
  rewritten_by?: string;
  // At G6: CloudWatch alarms firing before production on what the feature touches.
  alarms_firing?: {
    env: string;
    resource: string;
    alarm: string;
    severity: string;
    says: string;
    since: string;
    url: string;
    title: string;
  }[];
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
  needs_passkey?: boolean; // they'd decide through a workspace group after a passkey sign-in
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
  /** G1-G4: how far the product's own history predicts this approval (valxel_sdlc.confidence). */
  confidence?: GateConfidence | null;
  /** The product is in safe mode: no batch, no quick reads, read in full. */
  safe_mode?: SafeMode | null;
  second_critic?: SecondCritic | null;
  /** Build found the approved spec, design or architecture wrong; people can reopen it. */
  design_issues?: DesignIssue[];
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
  feature: "Feature",
  express: "Express",
};

export const PROFILE_HELP: Record<Profile, string> = {
  feature: "Every phase and gate: discovery, spec, UX design and architecture, then build and release.",
  express:
    "A small, low-risk change: no discovery or UX design, and the spec is approved with the architecture at G4.",
};

export interface AgentNote {
  kind: "open" | "changed" | "assumed" | "traded off";
  text: string;
  phase?: string;
  task?: string;
  repo?: string;
}

export interface TracePr {
  key: string;
  repo: string;
  number: number | null;
  url: string;
  task: string;
  merged: boolean;
}

export interface TraceCase {
  id: string;
  req: string;
  kind: string;
  what: string;
  ref: string;
  via: string;
  status: "passed" | "failed" | "written" | "missing" | "pending" | "manual" | "unknown";
}

export interface TraceRequirement {
  id: string;
  type: string;
  text: string;
  goals: string[];
  design: Record<string, string>;
  deferred: string | null;
  tasks: string[];
  cases: TraceCase[];
  test: string;
  prs: TracePr[];
  repos: string[];
  acceptance: Record<string, string>;
  deployed: Record<string, string>;
}

export interface ReleaseTraceability {
  complete: boolean;
  gaps: string[];
  deferred: { id: string; reason: string }[];
  drift: string[];
  drift_error: string;
  drift_blocks: boolean;
  blocks: boolean;
  override_approvers: string[];
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
  repos: {
    repo: string;
    role: string;
    tasks: string[];
    prs: TracePr[];
    deployed: { env: string; release: string } | null;
  }[];
  release: { flag: string; percent: number | null; production: boolean; merged: boolean };
  incidents: { id: string; title: string; status: string; severity: string; touch: string }[];
  gaps: string[];
}

export interface PerformanceDecision {
  repo: string;
  role: string;
  level: "none" | "smoke" | "full";
  reasons: string[];
  soak: boolean;
}

export interface Correction {
  gate: string;
  decision: string;
  approver: string;
  reason: string;
  feedback: string;
  at: string;
}

export interface Lesson {
  id: string;
  phase: string;
  text: string;
  check: boolean;
  sources: string[];
  pr?: string;
  feature_id?: string;
  why?: "remember" | "takeover" | "repeat";
}

export interface LessonsOverview {
  product_id: string;
  approved: Lesson[];
  proposed: Lesson[];
  corrections: number;
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
  gate: string;
  phase: string;
  lane?: string;
  decision: string;
  approver: string;
  feedback: string;
  active_minutes: number;
  at: string;
}

export interface FeatureEvent {
  at: string;
  node: string;
  kind: string;
  detail?: string;
  [k: string]: unknown;
}

export interface EvalCounts {
  suite: string;
  passed: number;
  failed: number;
  rate: number | null;
  state: string;
}
export interface AgentEvals {
  repo: string;
  role?: "agent" | "knowledge";
  stages: Record<string, EvalCounts>;
  previous: Record<string, EvalCounts>;
  previous_feature: string;
  change: Record<string, number>;
}

export interface AgentSession {
  trace_id: string;
  url: string;
  at: string;
  phase: string;
  role: string;
  runtime: string;
  model: string;
  task: string;
  attempt: string;
  calls: number;
  duration_s: number;
  tokens: number;
  cost_usd: number;
  // Installed skills the session loaded by itself, beyond the ones its phase gave it.
  extra_skills?: string[];
}

/** How long each step usually takes, and when it should be done (valxel_sdlc.progress). */
export interface PathStation extends Station {
  eta_minutes: number;
  from_history: boolean;
  mine?: boolean;
}
export interface FeaturePath {
  stations: PathStation[];
  expected_done: string | null;
  minutes_left: number | null;
}
export interface NotifyPrefs {
  email: string;
  needed: boolean;
  staging: boolean;
  watch: string[];
  channels: { email: boolean; chat: boolean };
  sent: { at: string; kind: string; ref: string; subject: string }[];
}
/** How a feature ships: with a release, or on its own (G1, changeable until G6). */
export interface Ship {
  release_id?: string;
  own?: boolean;
  chosen?: boolean;
  why?: string;
}

export interface OpenRelease {
  id: string;
  title: string;
  model: "launch" | "train";
  target_date: string;
  initiative: string;
}

/** Where a release's features are: ready, being checked, building, in design, not started, stopped. */
export interface ReleaseReadiness {
  release_id: string;
  model: "launch" | "train";
  target_date: string;
  status: string;
  counts: Record<"ready" | "checking" | "building" | "design" | "not_started" | "stopped", number>;
  total: number;
  ready: number;
  features: { feature_id: string; title: string; group: string; stage: string; must: boolean }[];
  not_started: { title: string; must: boolean }[];
  must: { total: number; ready: number; missing: string[] };
  says: string;
}

/** The change set's environment to try the change in, until it merges (valxel_sdlc.preview). */
export interface Preview {
  url?: string;
  env?: string;
  expires_at?: string;
  /** Why the link is gone (merged, stopped, or out of time). */
  gone?: string;
}

export interface Feature extends FeatureSummary {
  path?: FeaturePath;
  ship?: Ship;
  /** Open releases it could ship with; null once how it ships is settled (G6). */
  ship_releases?: OpenRelease[] | null;
  try_it?: Preview | null;
  watching?: boolean;
  attachments?: Attachment[];
  description: string;
  agent_sessions?: AgentSession[];
  // "canary": the feature's agents run with the candidate config.
  config_variant?: string;
  profile?: Profile;
  track?: string;
  constitution_version: string;
  artifacts: Record<string, string>;
  /** Each PR's independent review: an agent with none of the feature's context (T-144). */
  independent_reviews?: IndependentReview[];
  pull_requests: Record<
    string,
    {
      repo: string;
      number: number;
      url: string;
      state: string;
      merged_by?: string;
      review_score?: number;
      task_id?: string;
    }
  >;
  change_set: { id?: string; merge_order?: string[]; published?: string[] };
  release: {
    components?: string[];
    packages?: string[];
    production?: boolean;
    promoted_by?: string;
    flag?: string;
    flag_on?: boolean | null;
    release_id?: string;
    flag_percent?: number;
    rollout_plan?: { steps: number[]; step: number; bake_minutes: number; stopped?: boolean };
  };
  initiative_id?: string;
  flag?: string;
  gate_decisions: GateDecision[];
  scores: Record<string, number>;
  loop_count: Record<string, number>;
  human_active_minutes: number;
  // AWS cost G4 approved, and what the latest review after release found (null: no AWS services).
  run_cost?: {
    approved_monthly_usd: Record<string, number>;
    projected_monthly_usd: Record<string, number> | null;
    reviewed_at: string | null;
    findings: string[];
    incidents: string[];
  } | null;
  usage: {
    calls: number;
    cache_hit_rate: number;
    est_cost_usd: number;
    cost_by_phase: Record<string, number>;
  };
  events: FeatureEvent[];
  diagrams: Diagram[];
  delivery: Record<string, WaitResult>;
  deployments: Record<
    string,
    { repo: string; component: string; env: string; release?: string; pipeline?: string[]; kind?: string }
  >;
  quality: Record<string, { stage: string; status: string; suites: QualitySuite[]; at: string }>;
  waivers: Waiver[];
  new_repos: RepoPlan[];
  runs: Record<
    string,
    { repo: string; component: string; run_id: string; sha: string; url?: string; state?: string }
  >;
  work_tracking: WorkTracking | null;
}

export interface AdrRef {
  id: string;
  title: string;
  status: string;
  lane?: string;
}
export interface ContractChange {
  repo: string;
  current: string;
  next: string;
  breaking: boolean;
  note: string;
}
export interface DesignReview {
  findings: string[];
  builds_on: AdrRef[];
  supersedes: { adr: string; supersedes: string; title: string }[];
  contracts: ContractChange[];
  related: AdrRef[];
  in_flight: {
    feature: string;
    title: string;
    repos: string[];
    contracts?: { repo: string; next: string }[];
  }[];
  record: { repo?: string | null; sha?: string | null };
  errors: Record<string, string>;
}

export interface RepoPlan {
  repo: string;
  role: string;
  purpose: string;
  template?: string;
  visibility?: string;
  ruleset?: { required_approvals?: number; required_checks?: string[] };
  teams?: Record<string, string>;
  security?: Record<string, boolean>;
  // after provisioning
  status?: string;
  url?: string;
  governance_pr?: string | null;
  steps?: { step: string; ok: boolean; error?: string; detail?: string }[];
  required_checks?: string[];
  required_approvals?: number;
}

export interface SecurityFinding {
  suite: string;
  tool: string;
  rule: string;
  severity: string;
  title: string;
  repo: string;
  url: string;
  fingerprint: string;
}

export interface QualitySuite {
  stage?: string;
  suite: string;
  kind: "test" | "security";
  repo: string;
  state: string;
  passed?: number | null;
  failed?: number | null;
  coverage?: number | null;
  findings?: SecurityFinding[] | number;
  waived?: string[] | number;
  reasons?: string[];
  required?: boolean;
}

export interface Waiver {
  fingerprint: string;
  suite: string;
  severity: string;
  title: string;
  repo: string;
  approver: string;
  reason: string;
  expires: string;
}

export interface WaitResult {
  status: "success" | "failure";
  wait: string;
  reason?: string;
  at: string;
  results: {
    repo?: string;
    component?: string;
    state: string;
    message?: string;
    url?: string;
    env?: string;
    checks?: { name: string; state: string; url: string }[];
  }[];
}

export interface Diagram {
  provider: string;
  title: string;
  version: number;
  url: string;
  edit_url?: string;
  file?: string;
  file_url?: string;
  stub?: boolean;
  at: string;
}

export interface WorkItem {
  item: string;
  kind: "epic" | "story" | "task";
  key: string;
  url: string;
  summary: string;
  status: "todo" | "in_progress" | "done" | "cancelled";
}

export interface WorkTracking {
  provider: string;
  project: string;
  items: WorkItem[];
  error: string | null;
}

export interface Me {
  login: string;
  name: string;
  avatar_url: string;
  orgs: string[];
  teams: string[];
  lanes: { id: string; name: string }[];
  /** The API takes this app's spans at /otlp/v1/traces (forwarded to the trace store). */
  tracing?: boolean;
  /** The tool's owners (VALXEL_EVAL_APPROVERS): its health across products, drift, safe mode. */
  tool_owner?: boolean;
  email?: string;
  idp?: "github" | "cognito" | "dev";
  strong?: boolean; // two factors at sign-in (a passkey through Cognito)
  roles?: Record<string, Role>; // workspace -> role, read on every request
  groups?: string[]; // "<workspace>/<group>"
  products?: Record<string, string[]>; // workspace -> products the profile covers ([] for all)
}

export type Role = "owner" | "admin" | "approver" | "member" | "viewer";
export const ROLES: Role[] = ["owner", "admin", "approver", "member", "viewer"];
export const ROLE_TEXT: Record<Role, string> = {
  owner: "Everything an admin can, and makes or changes owners",
  admin: "Invites people, sets roles, groups and gate approvers",
  approver: "Decides the gates their groups approve, and starts features",
  member: "Starts features and follows them",
  viewer: "Sees the workspace's products",
};
export interface Workspace {
  id: string;
  name: string;
  plan?: string;
  role: Role;
}
export interface Plan {
  id: "self-hosted" | "lite" | "full";
  name: string;
  for_whom: string;
  price: string;
  card_when: string;
  sign_in: string;
  runs_on: string;
  monthly_usd: number | null;
  hosted: boolean;
}
export interface Plans {
  plans: Plan[];
  trial_days: number;
  install_guide: string;
}
export interface Billing {
  plan: Plan;
  provider: string;
  card_on_file: boolean;
  trial_ends: string;
  trial_days_left: number | null;
  card_needed: boolean;
  note: string;
}
export interface Member {
  id: string;
  name: string;
  email: string;
  github_login: string;
  role: Role;
  products: string[];
  status: "invited" | "active" | "suspended";
  invited_by?: string;
  created?: string;
  updated?: string;
}
export interface Group {
  slug: string;
  name: string;
  members: string[];
}
export interface WorkspaceView extends Workspace {
  members: Member[];
  groups: Group[];
  products: { id: string; gates: Record<string, string[]>; version: number }[];
}
export interface AuditEntry {
  at: string;
  actor: string;
  action: string;
  target?: string;
  detail?: Record<string, unknown>;
}
export interface Invited extends Member {
  invite_url: string;
  emailed: boolean;
}
export interface Connection {
  tool: string;
  account: string;
  status: "connected" | "needs_attention";
  connected_by: string;
  connected_at: number;
  last_ok: number;
  last_error: string;
  reach: {
    account_type?: string;
    selection?: "all" | "selected";
    repos?: string[];
    installation_id?: number;
    mode?: string;
    deploy_role?: string;
    agents_role?: string;
    sites?: { id?: string; url?: string; name?: string }[];
    org?: string;
    subdomain?: string;
    user?: string;
  };
}
export interface WorkspaceClouds {
  cloud: "aws" | "gcp" | "azure";
  name: string;
  noun: string;
  accounts?: { non_production: string; production: string };
  region?: string;
  planes: { non_production: string; production: string };
}

export interface CloudGuide {
  cloud: "aws" | "gcp" | "azure";
  name: string;
  noun: string;
  links: Record<string, string>;
  steps: string[];
}

export interface ToolRow {
  tool: string;
  name: string;
  asked_when: string;
  grants: string;
  revoke: string;
  available: boolean;
  status: "connected" | "needs_attention" | "not_connected";
  connections: Connection[];
}

export interface IncidentAction {
  id: string;
  type: "flag_off" | "rollback" | "hotfix" | "intent_change" | "monitor";
  text: string;
  feature_id?: string;
  component?: string;
  repo?: string;
  change?: string;
  scope?: string;
  intent?: string;
  ok?: boolean;
  result?: string;
  by?: string;
  at?: string;
}
export interface Signal {
  source: string;
  status: "firing" | "resolved";
  severity: string;
  title: string;
  detail: string;
  fingerprint: string;
  url: string;
  at: string;
  component: string;
  // Why it joined this incident (same trace, call graph or release), when it came from elsewhere.
  labels?: Record<string, string>;
}
export interface Suspect {
  feature_id: string;
  title: string;
  released_at: string;
  flag: string;
  flag_on: boolean | null;
}
export interface IncidentView extends Omit<ThreadBase, "kind"> {
  kind: "incident";
  severity: string;
  component: string;
  opened_at: string;
  triage: string;
  actions: IncidentAction[];
  taken: IncidentAction[];
  links: Record<string, string>;
  postmortem: string;
  signals: Signal[];
  firing: number;
  suspects: Suspect[];
  root?: { component: string; why: string };
  paging?: PageSend[];
  analysis?: Analysis;
  escalated?: boolean;
}
/** OpenChoreo's agent's report on an incident: root causes (SRE agent) or right-sizing (FinOps agent). */
export interface Analysis {
  kind?: "rca" | "cost";
  status?: "pending" | "completed" | "failed" | "timed_out" | "skipped";
  why?: string;
  report_id?: string;
  report?: {
    summary?: string;
    result?: {
      type: string;
      explanation?: string;
      root_causes?: { summary: string; confidence: "high" | "medium" | "low"; analysis?: string }[];
      excluded_causes?: { description: string; rationale?: string }[];
      recommendations?: { recommended_actions?: { description: string }[] };
    };
    overprovisioning?: {
      analysis?: string;
      recommendation?: {
        cpu_request: string;
        cpu_limit: string;
        memory_request: string;
        memory_limit: string;
        estimated_savings: number;
        currency: string;
        rationale: string;
      };
    };
  } | null;
}
/** What happened to each PagerDuty send for an incident (ops/paging.py). */
export interface PageSend {
  at: string;
  action: "heads_up" | "urgent" | "acknowledge" | "resolve";
  ok: boolean;
  detail: string;
  dedup_key: string;
}
export interface IncidentRow {
  feature_id: string;
  product_id: string;
  title: string;
  severity: string;
  status: string;
  component: string;
  opened_at: string;
  firing: number;
  links: Record<string, string>;
}

export interface PilotFeature {
  feature_id: string;
  title: string;
  status: string;
  cost_usd: number;
  shadow_cost_usd: number;
  human_minutes: number;
  agent_minutes: number;
  human_share: number | null;
  first_time_approvals: number;
  gates: number;
  loops: Record<string, number>;
  ci_retries: number;
  escalations: number;
}
interface Agreement {
  compared: number;
  agreed: number;
}
export interface CriticAccuracy {
  passed_then_approved: number;
  passed_then_revised: number;
  failed_then_fixed: number;
  failed_then_vouched: number;
  false_pass_rate: number | null;
  false_fail_rate: number | null;
  missed_because: Record<string, number>;
}
export interface ConfigScore {
  features: number;
  done: number;
  first_time_approval_rate: number | null;
  loops_per_feature: number;
  critic_false_pass_rate: number | null;
  pre_merge_fixes: number;
  post_merge_fixes: number;
  incidents: number;
  traceability_overrides: number;
  cost_per_feature_usd: number;
  human_share: number | null;
}
export interface PilotReport {
  product_id: string;
  count: number;
  done: number;
  features: PilotFeature[];
  critic_accuracy: Record<string, CriticAccuracy>;
  by_config: Record<string, ConfigScore>;
  estimated_minutes: { pr_review: number; pr_comment: number };
  totals: {
    cost_usd: number;
    shadow_cost_usd: number;
    human_minutes: number;
    agent_minutes: number;
    human_share: number | null;
    target_human_share: number;
    first_time_approval_rate: number | null;
    escalations: number;
    ci_retries: number;
    revise_reasons: Record<string, number>;
    shadow_auto_approval: {
      gates: number;
      eligible: number;
      agreed: number;
      would_have_missed: number;
      minutes_saved: number;
    };
    shadow_critic: Agreement;
    shadow_reviewer: Agreement;
    old_score_agreed: Agreement;
  };
}
export interface UnmatchedSignal {
  source: string;
  title: string;
  component?: string;
  product_id?: string;
  severity?: string;
  env?: string;
  status?: string;
  fingerprint: string;
  url?: string;
  labels?: Record<string, string>;
  reason: string;
  received_at: string;
}
/** One thing an agent did on a feature, as it did it (T-112). */
export interface AgentStep {
  phase: string;
  role: string;
  repo: string;
  step: string;
  at: string;
}
/** Starting an initiative's features: started now, queued one at a time, or waiting on what they need. */
export interface StartResult {
  started: string[];
  queued: string[];
  waiting?: { title: string; on: string[] }[];
}
export interface Candidate {
  title: string;
  description: string;
  started?: boolean;
  must?: boolean;
  /** Other candidates it builds on (G0), and those whose design isn't approved (G4) yet (T-106). */
  needs?: string[];
  waiting_on?: string[];
}
export interface InitiativeDraft {
  goal: string;
  new_goal: { title: string; measure: string } | null;
  measure: string;
  horizon: string;
  budget_usd: number;
  lanes: string[];
  release_model: "launch" | "train";
  candidates: Candidate[];
}
export interface RoadmapFit {
  unplanned?: boolean;
  initiative?: { id: string; title: string; goal: string; horizon: string; release_model: string };
  budget_usd?: number;
  spent_usd?: number;
  remaining_usd?: number;
  over?: boolean;
}
export interface FeatureRow {
  feature_id: string;
  title: string;
  status: "released" | "in_progress" | "stopped" | string;
  stage: string;
  initiative_id: string;
  cost_usd: number;
  flag_on: boolean | null;
  /** On a shipped release: the incident that turned this feature off in production (T-104). */
  turned_off_by?: string;
}
export interface ThreadBase {
  feature_id: string;
  kind: "initiative" | "release" | "review";
  product_id: string;
  title: string;
  created_by: string;
  status: string;
  running: boolean;
  error: string | null;
  pending: PendingItem[];
  events: FeatureEvent[];
  cost_usd: number;
  record_pr: { url?: string; error?: string };
}
export interface InitiativeView extends ThreadBase {
  request: string;
  content: string;
  draft: InitiativeDraft;
  findings: string[];
  features: FeatureRow[];
  candidates: Candidate[];
  budget_usd: number;
  spent_usd: number;
  progress: { released: number; total: number };
  research?: string;
}
export interface ReleaseView extends ThreadBase {
  model: "launch" | "train";
  target_date: string;
  initiative: string;
  notes: string;
  evidence: string;
  approved_by: string;
  dropped: string[];
  features: FeatureRow[];
  readiness?: ReleaseReadiness;
}
/** Product inception (valxel_sdlc.inception): a vision to a brief, an architecture and a roadmap. */
export interface InceptionReport {
  doc: string;
  status: "passed" | "escalated";
  attempts: number;
  findings: string[];
  checklist: { id: string; must: boolean; met: boolean; evidence: string; why: string }[];
  cost_usd: number;
  monthly_usd: Record<string, number>;
  data_class: string;
}
export interface InceptionSetupStep {
  step: string;
  ok: boolean;
  url?: string;
  repo?: string;
  error?: string;
  why?: string;
  line?: string;
  skipped?: boolean;
  teams?: string[];
  result?: string;
}
export interface InceptionView extends Omit<ThreadBase, "kind"> {
  kind: "inception";
  vision: {
    name: string;
    org: string;
    vision: string;
    goals: string[];
    users: string[];
    constraints: string[];
    budget_usd: number;
    product_id: string;
    existing: boolean;
  };
  brief: string;
  architecture: string;
  roadmap: string;
  reports: Partial<Record<"brief" | "architecture" | "roadmap" | "research", InceptionReport>>;
  research?: string;
  diagrams?: (Diagram & { diagram: "context" | "deployment" })[]; // published for the product gate
  setup: { product_id?: string; governance_repo?: string; steps?: InceptionSetupStep[]; ok?: boolean };
  council: string[];
}
export interface InceptionSummary {
  id: string;
  title: string;
  org: string;
  existing: boolean;
  product_id: string;
  status: string;
  created_by: string;
  cost_usd: number;
  running: boolean;
}
export interface InceptionIn {
  name: string;
  org: string;
  vision: string;
  goals: string[];
  users: string[];
  constraints: string[];
  budget_usd: number;
  product_id: string;
  research?: string;
}

export interface RoadmapInitiative {
  id: string;
  title: string;
  goal: string;
  horizon: string;
  status: string;
  budget_usd: number;
  lanes: string[];
  release_model: string;
  record: string;
  spent_usd: number;
  features: FeatureRow[];
  progress: { released: number; total: number; planned: number };
  candidates?: Candidate[];
  outcome?: {
    goal_id: string;
    status: OutcomeStatus;
    why?: string;
    progress?: number;
    expected?: number | null;
  } | null;
  review_by?: string;
  reviews?: { id: string; status: string; why: string; opened: string[] }[];
}
export interface ReviewView extends ThreadBase {
  initiative_id: string;
  why: string;
  preview: string;
  decision: { decision?: string; feedback?: string; approver?: string };
  opened: string[];
}
export interface GoalMetric {
  goal_id: string;
  source: "sql" | "prometheus" | "posthog" | "manual";
  unit: string;
  direction: "up" | "down";
  baseline: number | null;
  target: number | null;
  by: string;
}
export type OutcomeStatus = "on_track" | "flat" | "off_track" | "no_data";
export interface GoalOutcome {
  metric: GoalMetric;
  latest: number | null;
  status: OutcomeStatus;
  why?: string;
  progress?: number;
  expected?: number | null;
  trend: { end: string; value: number | null; status: string; note: string }[];
}
export interface ReadingRow {
  start: string;
  end: string;
  status: string;
  value: number | null;
  n: number | null;
  note: string;
}
export interface Readout {
  feature_id: string;
  goal_id: string;
  window_days: number;
  released_at: string;
  source: string;
  unit: string;
  direction: string;
  before: ReadingRow;
  after: ReadingRow;
  change: number | null;
  toward: boolean | null;
  caveat: string;
}
export interface WantedNumber {
  goal_id: string;
  feature_id: string;
  window_days: number;
  start: string;
  end: string;
  unit: string;
}
export interface FeatureOutcome {
  feature_id: string;
  product_id: string;
  goals: string[];
  metrics: Record<string, GoalMetric>;
  released_at: string;
  rollout: { percent: number; goal: string }[];
  instrumentation: InstrumentationCheck | null;
  readouts: Readout[];
  upcoming: { goal_id: string; window_days: number; due: string }[];
  /** The product's record of the outcome: outcome.md in its governance repo, once a readout is in. */
  record?: { repo: string; path: string; url: string } | null;
}
export interface Roadmap {
  product_id: string;
  record: { repo: string; sha: string; error: string };
  goals: { id: string; title: string; measure: string; period: string; outcome?: GoalOutcome | null }[];
  outcomes?: {
    wanted: WantedNumber[];
    windows: number[];
    readouts: Record<string, Readout[]>;
    min_cell?: number;
  };
  initiatives: RoadmapInitiative[];
  releases: {
    id: string;
    title: string;
    model: string;
    status: string;
    target_date: string;
    features: string[];
    record: string;
  }[];
  unplanned: FeatureRow[];
}

export interface SimilarItem {
  kind: "feature" | "capability";
  id: string;
  title: string;
  status: string;
  stage: string;
  score: number;
  duplicate: boolean;
}
export interface Draft {
  product_id: string;
  title: string;
  problem: string;
  users: string;
  success_metric: string;
  related_capability: string;
  similar: SimilarItem[];
  not_duplicate_reason: string;
  // The intent changes what people see: the experience round applies, and its brief.
  ui: boolean;
  experience: Partial<Record<ExperienceKey, string>>;
  // The front door: what size the request is, and what the bigger sizes need.
  size: "" | RequestSize;
  size_suggested: "" | RequestSize;
  size_why: string;
  horizon: string;
  research: "" | ResearchDepth;
  name: string;
  org: string;
  budget_usd: number;
  // A new product's cloud accounts (cloud/onboarding.py): "given" with both IDs and the region (its own),
  // "shared" (its workspace's clusters), or "later".
  cloud: "" | "given" | "later" | "shared";
  cloud_accounts: { non_production?: string; production?: string };
  region: string;
  placement?: "" | "shared" | "dedicated";
}
export type RequestSize = "feature" | "initiative" | "product";
export type ResearchDepth = "quick" | "thorough" | "own";
export const AGENT_PROPOSES = "Proposed by the agent";
export type ExperienceKey = "where" | "main_task" | "context" | "look";
export const EXPERIENCE_DELEGATED = "Left to the design agent";
/** A file attached to an intent: kept as the strictest data class until the intent is submitted. */
export interface Attachment {
  id: string;
  name: string;
  kind: "image" | "pdf" | "text";
  size: number;
  read_by_agents: boolean;
  mime?: string;
  uploaded_at?: string;
}
export const ATTACH_ACCEPT = ".png,.jpg,.jpeg,.gif,.webp,.pdf,.txt,.md,.csv,.json";
export interface IntakeSession {
  id: string;
  owner: string;
  workspace?: string;
  /** The installation's cloud (ADR 0010): every product runs on it. */
  cloud?: "aws" | "gcp" | "azure";
  lanes: string[];
  draft: Draft;
  ready: boolean;
  missing: string[];
  attachments: Attachment[];
  feature_id: string;
  created_at: string;
  starter: boolean;
  kind: "" | "feature" | "initiative" | "inception";
  awaiting: string;
  messages: { role: "user" | "agent"; text: string; at: string; attachment?: string; options?: string[] }[];
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> =
    body !== undefined ? { "Content-Type": "application/json" } : { "X-Valxel-Client": "web" };
  // Join the browser's trace when tracing is on; empty when it is off, so the
  // headers (and the existing tests) are unchanged without a live span.
  Object.assign(headers, traceHeaders());
  const res = await fetch(`/api${path}`, {
    method,
    credentials: "same-origin",
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const j = await res.json();
      msg = typeof j.detail === "string" ? j.detail : (j.detail?.message ?? JSON.stringify(j.detail));
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, msg);
  }
  return res.status === 204 ? (undefined as T) : res.json();
}

export interface CostCategory {
  category: string;
  label: string;
  usd: number;
  basis: string[];
  provisional: boolean;
}
export interface FeatureCost {
  feature_id: string;
  product_id: string;
  title: string;
  status: string;
  build: {
    total_usd: number;
    by_category: CostCategory[];
    provisional: boolean;
    llm_budget_usd?: number | null;
  };
  run_30d: { total_usd: number; by_category: CostCategory[] };
}
export interface ProductCost {
  product_id: string;
  month: string;
  provisional: boolean;
  totals: { run_usd: number; build_usd: number; total_usd: number; build_to_date_usd: number };
  run: { by_category: CostCategory[]; by_env: Record<string, number> };
  build: { by_category: CostCategory[] };
  features: {
    feature_id: string;
    title: string;
    status: string;
    build_to_date_usd: number;
    build_month_usd: number;
    run_month_usd: number;
    approved_monthly_usd?: number | null;
  }[];
}

export interface ChangeRecord {
  id: string;
  at: string;
  product_id: string;
  env: string;
  kind: "release" | "flag" | "rollback" | "policy" | "external";
  type: "standard" | "emergency" | "automatic";
  action: string;
  summary: string;
  targets: { kind: string; name: string; release?: string; on?: boolean; percent?: number }[];
  feature_id: string;
  release_id: string;
  incident_id: string;
  requested_by: string;
  approved_by: string[];
  approvals: { gate: string; lane: string; approver: string; at: string }[];
  evidence: {
    pull_requests?: string[];
    quality?: Record<string, Record<string, unknown>>;
    waivers?: unknown[];
    triage?: string;
  };
  outcome: "done" | "failed";
  detail: string;
  caused_incidents: string[];
  hash: string;
  prev_hash: string;
  receipt?: {
    status: "queued" | "closed" | "open" | "failed" | "not_sent";
    number?: string;
    instance?: string;
    detail?: string;
  } | null;
}
export interface LookbackMonth {
  month: string;
  incidents: number;
  by_severity: Record<string, number>;
  components: Record<string, number>;
  fixed_by_tool: number;
  escalated: number;
  open: number;
  changes: Record<string, number>;
  failed_changes: number;
  releases: number;
  releases_causing_incidents: number;
  change_failure_rate: number | null;
  recover_minutes_median: number | null;
  recover_minutes_max: number | null;
}
export interface Lookback {
  product_id: string;
  months: LookbackMonth[];
  total: {
    incidents: number;
    fixed_by_tool: number;
    escalated: number;
    releases: number;
    releases_causing_incidents: number;
    failed_changes: number;
    change_failure_rate: number | null;
  };
  causes: { change_id: string; summary: string; at: string; incidents: string[] }[];
}
export interface InventoryItem {
  kind: "component" | "resource" | "store_app";
  name: string;
  type?: string;
  env: string;
  state: string;
  release: string;
  size?: string;
  feature_id: string;
  repo?: string;
  cost_30d_usd?: number;
  planned_monthly_usd?: number | null;
  outputs?: { name: string; value: string }[];
  secrets?: { output: string; name: string; key: string }[];
  ids: {
    openchoreo?: string;
    binding?: string;
    kubectl?: string;
    aws_account?: string;
    aws_region?: string;
    arn?: string;
    stores?: string;
  };
}
export interface EnvSummary {
  components: number;
  resources: number;
  store_apps: number;
  not_ready?: number;
  cost_30d_usd: number;
  planned_monthly_usd: number;
}
export interface Inventory {
  product_id: string;
  at: string;
  project: string;
  namespace: string;
  pipeline: string[];
  items: InventoryItem[];
  summary: { per_env: Record<string, EnvSummary>; production: EnvSummary; lower: EnvSummary };
  unmanaged: { env: string; account: string; arn: string; resource_tag: string; feature: string }[];
  aws: string;
  problems: string[];
}

export const api = {
  changes: (productId: string, since?: string) =>
    request<ChangeRecord[]>(
      "GET",
      `/changes?product_id=${encodeURIComponent(productId)}${since ? `&since=${since}` : ""}`,
    ),
  verifyChanges: () =>
    request<{ ok: boolean; records: number; broken_at: string | null }>("GET", "/changes/verify"),
  lookback: (productId: string, months = 6) =>
    request<Lookback>(
      "GET",
      `/reports/lookback?product_id=${encodeURIComponent(productId)}&months=${months}`,
    ),
  inventory: (productId: string) => request<Inventory>("GET", `/inventory/${encodeURIComponent(productId)}`),
  featureCost: (id: string) => request<FeatureCost>("GET", `/features/${encodeURIComponent(id)}/cost`),
  productCost: (productId: string, month?: string) =>
    request<ProductCost>("GET", `/costs/${encodeURIComponent(productId)}${month ? `?month=${month}` : ""}`),
  authConfig: () =>
    request<{
      mode: "github" | "cognito" | "dev";
      teams?: string[];
      providers?: { id: string; label: string }[];
    }>("GET", "/auth/config"),
  devLogin: (login: string, teams: string[], orgs: string[], email = "") =>
    request<Me>("POST", "/auth/dev-login", { login, teams, orgs, email }),
  logout: () => request<{ ok: boolean }>("POST", "/auth/logout", {}),
  logoutEverywhere: () => request<{ ok: boolean }>("POST", "/auth/logout-everywhere", {}),
  me: () => request<Me>("GET", "/me"),
  features: () => request<FeatureSummary[]>("GET", "/features"),
  products: () =>
    request<
      Record<
        string,
        {
          name?: string;
          kind: string;
          ok: boolean;
          data_class?: string;
          profiles?: Profile[];
          default_profile?: Profile;
        }
      >
    >("GET", "/products"),
  resume: (id: string) =>
    request<{ accepted: boolean }>("POST", `/features/${encodeURIComponent(id)}/resume`, {}),
  syncWork: (id: string) => request<unknown>("POST", `/features/${encodeURIComponent(id)}/sync`, {}),
  feature: (id: string) => request<Feature>("GET", `/features/${encodeURIComponent(id)}`),
  agentSteps: (id: string) =>
    request<{ steps: AgentStep[] }>("GET", `/features/${encodeURIComponent(id)}/steps`),
  setShip: (id: string, releaseId: string) =>
    request<Ship>("PUT", `/features/${encodeURIComponent(id)}/ship`, { release_id: releaseId }),
  initiative: (id: string) => request<InitiativeView>("GET", `/features/${encodeURIComponent(id)}`),
  inception: (id: string) => request<InceptionView>("GET", `/features/${encodeURIComponent(id)}`),
  inceptions: () => request<InceptionSummary[]>("GET", "/inceptions"),
  createInception: (body: InceptionIn) => request<{ feature_id: string }>("POST", "/inceptions", body),
  release: (id: string) => request<ReleaseView>("GET", `/features/${encodeURIComponent(id)}`),
  review: (id: string) => request<ReviewView>("GET", `/features/${encodeURIComponent(id)}`),
  incident: (id: string) => request<IncidentView>("GET", `/features/${encodeURIComponent(id)}`),
  mergeIncident: (id: string, into: string, reason: string) =>
    request<{ merged: string; into: string }>("POST", `/incidents/${encodeURIComponent(id)}/merge`, {
      into,
      reason,
    }),
  splitSignal: (id: string, fingerprint: string) =>
    request<{ new_incident: string | null }>("POST", `/incidents/${encodeURIComponent(id)}/split`, {
      fingerprint,
    }),
  incidents: () => request<IncidentRow[]>("GET", "/incidents"),
  unmatchedSignals: () => request<{ count: number; signals: UnmatchedSignal[] }>("GET", "/signals/unmatched"),
  reportProblem: (body: {
    product_id: string;
    title: string;
    description: string;
    severity: string;
    component?: string;
  }) => request<{ feature_id: string }>("POST", "/incidents", body),
  roadmap: (productId: string) => request<Roadmap>("GET", `/roadmap/${encodeURIComponent(productId)}`),
  recordReading: (
    productId: string,
    goalId: string,
    body: { value: number; start: string; end: string; n?: number; k?: number },
  ) =>
    request<{ reading: ReadingRow; readouts: Readout[] }>(
      "POST",
      `/products/${encodeURIComponent(productId)}/goals/${encodeURIComponent(goalId)}/readings`,
      body,
    ),
  featureOutcome: (id: string) =>
    request<FeatureOutcome>("GET", `/features/${encodeURIComponent(id)}/outcome`),
  createInitiative: (body: {
    product_id: string;
    title: string;
    request: string;
    measure?: string;
    horizon?: string;
    research?: string;
  }) => request<{ feature_id: string }>("POST", "/initiatives", body),
  records: (id: string) => request<RecordPr[]>("GET", `/threads/${encodeURIComponent(id)}/records`),
  mergeRecord: (id: string, key: string) =>
    request<RecordPr>("POST", `/threads/${encodeURIComponent(id)}/records/${key}/merge`),
  startFeatures: (id: string, indexes: number[], order: "together" | "sequence") =>
    request<StartResult>("POST", `/initiatives/${encodeURIComponent(id)}/start`, {
      indexes,
      order,
    }),
  firstFeature: (id: string) =>
    request<{ feature_id: string }>("POST", `/inceptions/${encodeURIComponent(id)}/first-feature`),
  startCandidate: (id: string, index: number, not_duplicate_reason = "") =>
    request<{ feature_id: string }>("POST", `/initiatives/${encodeURIComponent(id)}/features`, {
      index,
      not_duplicate_reason,
    }),
  createRelease: (body: {
    product_id: string;
    title: string;
    model: string;
    features: string[];
    target_date?: string;
    initiative?: string;
  }) => request<{ feature_id: string }>("POST", "/releases", body),
  artifact: (id: string, kind: string) =>
    request<{ id: string; content: string; version: number; constitution_version: string }>(
      "GET",
      `/features/${encodeURIComponent(id)}/artifacts/${kind}`,
    ),
  inbox: (mine: boolean) => request<PendingItem[]>("GET", `/inbox${mine ? "?mine=true" : ""}`),
  notifications: () => request<NotifyPrefs>("GET", "/me/notifications"),
  setNotifications: (body: Partial<Pick<NotifyPrefs, "email" | "needed" | "staging">>) =>
    request<NotifyPrefs>("PUT", "/me/notifications", body),
  watch: (id: string, on: boolean) =>
    request<{ watching: boolean }>("POST", `/threads/${encodeURIComponent(id)}/watch`, { on }),
  changeRequest: (id: string, description: string) =>
    request<{ feature_id: string; sent_back: string; note: string }>(
      "PUT",
      `/features/${encodeURIComponent(id)}/request`,
      { description },
    ),
  approveBatch: (items: { feature_id: string; interrupt_id: string }[], active_minutes = 0) =>
    request<{ approved: string[]; skipped: { feature_id: string; why: string }[] }>(
      "POST",
      "/inbox/approve",
      { items, active_minutes },
    ),
  reading: (featureId: string, interruptId: string, seconds: number) =>
    request<{ seconds: number }>(
      "POST",
      `/features/${encodeURIComponent(featureId)}/decisions/${interruptId}/reading`,
      { seconds },
    ),
  decisionBrief: (featureId: string, interruptId: string) =>
    request<DecisionBriefData>(
      "GET",
      `/features/${encodeURIComponent(featureId)}/decisions/${interruptId}/brief`,
    ),
  decide: (featureId: string, interruptId: string, body: Record<string, unknown>) =>
    request<{ accepted: boolean }>(
      "POST",
      `/features/${encodeURIComponent(featureId)}/decisions/${interruptId}`,
      body,
    ),
  intakeStart: (workspace = "") => request<IntakeSession>("POST", "/intake", workspace ? { workspace } : {}),
  intakeList: () => request<IntakeSession[]>("GET", "/intake"),
  intake: (id: string) => request<IntakeSession>("GET", `/intake/${id}`),
  intakeSay: (id: string, text: string) => request<IntakeSession>("POST", `/intake/${id}/messages`, { text }),
  intakeDraft: (
    id: string,
    changes: Partial<Omit<Draft, "experience" | "cloud">> & {
      experience?: Partial<Record<ExperienceKey, string>>;
      /** "later", or "non-production: <id>, production: <id>, region: <region>" */
      cloud?: string;
    },
  ) => request<IntakeSession>("PATCH", `/intake/${id}/draft`, changes),
  resize: (id: string) =>
    request<{ feature_id: string }>("POST", `/threads/${encodeURIComponent(id)}/resize`),
  intakeAttach: async (id: string, file: File): Promise<IntakeSession> => {
    const res = await fetch(`/api/intake/${id}/attachments?name=${encodeURIComponent(file.name)}`, {
      method: "POST",
      credentials: "same-origin",
      body: file,
      headers: { "Content-Type": "application/octet-stream", "X-Valxel-Client": "web" },
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok)
      throw new ApiError(
        res.status,
        typeof data.detail === "string" ? data.detail : `Couldn't attach ${file.name}`,
      );
    return data as IntakeSession;
  },
  intakeDetach: (id: string, aid: string) =>
    request<IntakeSession>("DELETE", `/intake/${id}/attachments/${aid}`),
  intakeSubmit: (id: string, pilot = false, profile = "") =>
    request<IntakeSession>("POST", `/intake/${id}/submit`, { pilot, profile }),
  trace: (id: string) => request<Trace>("GET", `/features/${encodeURIComponent(id)}/trace`),
  traceCsvUrl: (id: string) => `/api/features/${encodeURIComponent(id)}/trace.csv`,
  workspaces: () => request<Workspace[]>("GET", "/workspaces"),
  plans: () => request<Plans>("GET", "/plans"),
  billing: (ws: string) => request<Billing>("GET", `/workspaces/${encodeURIComponent(ws)}/billing`),
  setPlan: (ws: string, plan: "lite" | "full") =>
    request<Workspace>("PUT", `/workspaces/${encodeURIComponent(ws)}/plan`, { plan }),
  createWorkspace: (body: { id: string; name: string; plan?: string }) =>
    request<Workspace>("POST", "/workspaces", body),
  workspace: (ws: string) => request<WorkspaceView>("GET", `/workspaces/${encodeURIComponent(ws)}`),
  invite: (
    ws: string,
    body: { name: string; email: string; github_login: string; role: Role; products: string[] },
  ) => request<Invited>("POST", `/workspaces/${encodeURIComponent(ws)}/members`, body),
  resendInvite: (ws: string, id: string) =>
    request<{ invite_url: string; emailed: boolean }>(
      "POST",
      `/workspaces/${encodeURIComponent(ws)}/members/${encodeURIComponent(id)}/resend`,
      {},
    ),
  signMemberOut: (ws: string, id: string) =>
    request<{ signed_out: string[] }>(
      "POST",
      `/workspaces/${encodeURIComponent(ws)}/members/${encodeURIComponent(id)}/sign-out`,
      {},
    ),
  updateMember: (ws: string, id: string, body: { role?: Role; products?: string[]; status?: string }) =>
    request<Member>("PATCH", `/workspaces/${encodeURIComponent(ws)}/members/${encodeURIComponent(id)}`, body),
  removeMember: (ws: string, id: string) =>
    request<void>("DELETE", `/workspaces/${encodeURIComponent(ws)}/members/${encodeURIComponent(id)}`),
  join: (ws: string, token: string) =>
    request<Member>("POST", `/workspaces/${encodeURIComponent(ws)}/join/${encodeURIComponent(token)}`, {}),
  createGroup: (ws: string, name: string) =>
    request<Group>("POST", `/workspaces/${encodeURIComponent(ws)}/groups`, { name }),
  setGroup: (ws: string, slug: string, members: string[]) =>
    request<Group>("PUT", `/workspaces/${encodeURIComponent(ws)}/groups/${encodeURIComponent(slug)}`, {
      members,
    }),
  deleteGroup: (ws: string, slug: string) =>
    request<void>("DELETE", `/workspaces/${encodeURIComponent(ws)}/groups/${encodeURIComponent(slug)}`),
  setGates: (ws: string, productId: string, gates: Record<string, string[]>) =>
    request<{ version: number }>(
      "PUT",
      `/workspaces/${encodeURIComponent(ws)}/products/${encodeURIComponent(productId)}/gates`,
      { gates },
    ),
  connections: (ws: string) => request<ToolRow[]>("GET", `/workspaces/${encodeURIComponent(ws)}/connections`),
  awsLaunch: (ws: string, mode: string, region: string, account = "") =>
    request<{ url: string; mode: string; sign_in?: string }>(
      "GET",
      `/workspaces/${encodeURIComponent(ws)}/connections/aws/launch?mode=${mode}&region=${encodeURIComponent(region)}${account ? `&account=${encodeURIComponent(account)}` : ""}`,
    ),
  awsCheck: (ws: string, account: string, mode: string) =>
    request<Connection>("POST", `/workspaces/${encodeURIComponent(ws)}/connections/aws/check`, {
      account,
      mode,
    }),
  gcpLaunch: (ws: string, mode: string, region: string, account = "") =>
    request<{ url: string; command: string; mode: string }>(
      "GET",
      `/workspaces/${encodeURIComponent(ws)}/connections/gcp/launch?mode=${mode}&region=${encodeURIComponent(region)}${account ? `&account=${encodeURIComponent(account)}` : ""}`,
    ),
  gcpCheck: (ws: string, project: string, mode: string) =>
    request<Connection>("POST", `/workspaces/${encodeURIComponent(ws)}/connections/gcp/check`, {
      project,
      mode,
    }),
  azureLaunch: (ws: string, mode: string, region: string, account = "") =>
    request<{ url: string; command: string; mode: string }>(
      "GET",
      `/workspaces/${encodeURIComponent(ws)}/connections/azure/launch?mode=${mode}&region=${encodeURIComponent(region)}${account ? `&account=${encodeURIComponent(account)}` : ""}`,
    ),
  /** Is one account connected yet? Valxel checks AWS and Google Cloud itself; Azure's command reports back. */
  connectionPoll: (ws: string, tool: string, account: string) =>
    request<{ status: string; why?: string; connection?: Connection }>(
      "GET",
      `/workspaces/${encodeURIComponent(ws)}/connections/${encodeURIComponent(tool)}/poll?account=${encodeURIComponent(account)}`,
    ),
  /** A workspace's two shared accounts, where every product that shares its clusters runs. */
  workspaceCloudAccounts: (ws: string) =>
    request<WorkspaceClouds>("GET", `/workspaces/${encodeURIComponent(ws)}/cloud-accounts`),
  setWorkspaceCloudAccounts: (ws: string, accounts: string) =>
    request<WorkspaceClouds>("PUT", `/workspaces/${encodeURIComponent(ws)}/cloud-accounts`, { accounts }),
  /** How to get a new product's cloud accounts ready: the installation's cloud's own pages. */
  cloudOnboarding: (product: string, workspace = "") =>
    request<CloudGuide>(
      "GET",
      `/cloud/onboarding?product=${encodeURIComponent(product)}&workspace=${encodeURIComponent(workspace)}`,
    ),
  azureCheck: (
    ws: string,
    body: { subscription: string; deploy_client_id: string; agents_client_id: string; mode: string },
  ) => request<Connection>("POST", `/workspaces/${encodeURIComponent(ws)}/connections/azure/check`, body),
  disconnect: (ws: string, tool: string, account: string) =>
    request<{ revoke: string }>(
      "DELETE",
      `/workspaces/${encodeURIComponent(ws)}/connections/${encodeURIComponent(tool)}/${encodeURIComponent(account)}`,
    ),
  workspaceAudit: (ws: string) => request<AuditEntry[]>("GET", `/workspaces/${encodeURIComponent(ws)}/audit`),
  lessons: (productId: string) =>
    request<LessonsOverview>("GET", `/products/${encodeURIComponent(productId)}/lessons`),
  pilotReport: (productId: string) =>
    request<PilotReport>("GET", `/reports/pilot?product_id=${encodeURIComponent(productId)}`),
  toolHealth: (productId?: string) =>
    request<ToolHealth>(
      "GET",
      productId ? `/reports/health?product_id=${encodeURIComponent(productId)}` : "/reports/health",
    ),
  safeModeEnter: (productId: string, note: string) =>
    request<unknown>("POST", `/safe-mode/${encodeURIComponent(productId)}/enter`, { note }),
  safeModeFixed: (productId: string, note: string) =>
    request<unknown>("POST", `/safe-mode/${encodeURIComponent(productId)}/fixed`, { note }),
  safeModeReplay: (productId: string) =>
    request<unknown>("POST", `/safe-mode/${encodeURIComponent(productId)}/replay`, {}),
  safeModeLeave: (productId: string) =>
    request<SafeModeStatus & { approvals_to_review: { feature_id: string; gate: string; at: string }[] }>(
      "POST",
      `/safe-mode/${encodeURIComponent(productId)}/leave`,
      {},
    ),
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

/** An independent review of one pull request (valxel_sdlc.independent_review). */
export interface IndependentReview {
  repo: string;
  number: number;
  head_sha: string;
  state: "success" | "failure";
  description: string;
  at: string;
  product_id: string;
  summary: string;
  findings: {
    severity: "blocking" | "major" | "minor" | "nit";
    file: string;
    line?: number | null;
    issue: string;
  }[];
}
