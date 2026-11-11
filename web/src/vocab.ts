// Names people see. Keep them identical everywhere: a gate is always called by these words.
export const PHASES: Record<string, string> = {
  discovery: "Discovery", spec: "Spec", design: "Design", architecture: "Architecture",
  plan: "Plan", build: "Build", release: "Release", operate: "Operate", delivery: "Delivery", security: "Security", provisioning: "Repository setup",
  brief: "Product brief", product_plan: "Architecture and roadmap", setup: "Setup",
};

const FAILED: Record<string, string> = {
  build: "OpenChoreo build", change_set: "Change set's own environment", dev: "Dev deploy", staging: "Staging deploy", production: "Production deploy",
  rollout: "Store rollout health check",
};

export const WAITS: Record<string, string> = {
  release_ready: "Waiting for its features", release_train: "Waiting for its release train",
  incident_recovery: "Waiting for alerts to resolve",
  ci: "Waiting for CI", build: "Building in OpenChoreo", change_set: "Testing the change set together before merging",
  dev: "Deploying to dev",
  staging: "Deploying to staging", production: "Deploying to production",
  rollout: "Rolling out in the stores",
};

export const GATES: Record<string, { name: string; question: string; approve: string }> = {
  g0: { name: "Initiative", question: "Commit to this initiative?", approve: "Approve initiative" },
  brief: { name: "Brief", question: "Is this the product we want?", approve: "Approve brief" },
  product: { name: "Product", question: "Approve the architecture and roadmap?", approve: "Approve and set up" },
  release: { name: "Release", question: "Approve this release?", approve: "Approve release" },
  incident: { name: "Incident", question: "What should we do about this incident?", approve: "Approve actions" },
  postmortem: { name: "Postmortem", question: "Close the incident with this postmortem?", approve: "Close incident" },
  g1: { name: "Intent", question: "Is this worth building?", approve: "Approve intent" },
  g2: { name: "Scope", question: "Is the scope right?", approve: "Approve scope" },
  g3: { name: "Experience", question: "Is the experience right?", approve: "Approve design" },
  g4: { name: "Architecture", question: "Approve the architecture?", approve: "Approve architecture" },
  g5: { name: "Merge", question: "Merge these changes?", approve: "Approve merge" },
  g6: { name: "Go-live", question: "Release to production?", approve: "Release" },
};

export const ARTIFACTS: Record<string, string> = {
  OPP: "Opportunity brief", PRD: "Product spec", UX: "Design spec", HLD: "Architecture",
  PLAN: "Plan", BUILD: "Build", REL: "Release", OPS: "Operating report",
};

export const RISK: Record<string, string> = {
  low: "Low risk", high: "High risk", regulated: "Regulated",
};

export const SEVERITY: Record<string, string> = { sev1: "SEV1", sev2: "SEV2", sev3: "SEV3", sev4: "SEV4" };
export const ACTION_LABEL: Record<string, string> = {
  flag_off: "Turn the feature's flag off", rollback: "Roll back to the previous release", hotfix: "Start a hotfix",
  intent_change: "Change the intent", monitor: "Keep watching",
};

export const HORIZONS: Record<string, string> = { now: "Now", next: "Next", later: "Later" };
export const MODELS: Record<string, string> = { launch: "Launch group", train: "Release train" };

/** Where a thread lives in the app: features, initiatives and releases share ids like CLN-INI-1. */
export function threadPath(id: string): string {
  const kind = id.split("-")[1];
  return kind === "INI" ? `/initiatives/${id}` : kind === "REL" ? `/releases/${id}` : kind === "PRD" ? `/products/${id}`
    : kind === "INC" ? `/incidents/${id}` : `/features/${id}`;
}

export function gateLabel(gate?: string): string {
  if (!gate) return "";
  const g = GATES[gate];
  if (gate === "release") return "Release";
  if (gate === "incident") return "On-call decision";
  if (gate === "postmortem") return "Postmortem";
  if (gate === "brief") return "Brief";
  if (gate === "product") return "Product";
  return g ? `${gate.toUpperCase()} ${g.name}` : gate.toUpperCase();
}

export function waitingLabel(item: { type: string; gate?: string; phase?: string; lane?: string; wait?: string }, ownLane?: string): string {
  if (item.type === "external") return WAITS[item.wait ?? ""] ?? "Waiting on delivery";
  if (item.type === "escalation" && item.phase === "security") return "Security findings: needs a person";
  if (item.type === "escalation" && item.phase === "provisioning") return "Repository setup failed: needs a person";
  if (item.type === "escalation" && item.phase === "delivery") return `${FAILED[item.wait ?? ""] ?? "Delivery"} failed: needs a person`;
  if (item.type === "escalation") return `${PHASES[item.phase ?? ""] ?? item.phase} needs a person`;
  if (item.type === "handback") return `${gateLabel(item.gate)}: taken over, waiting for hand-back`;
  const lane = item.lane && ownLane && item.lane !== ownLane ? ` (${item.lane})` : "";
  if (item.gate === "brief" || item.gate === "product") return `Waiting for the ${item.gate} gate`;
  return `Waiting for ${gateLabel(item.gate)}${lane}`;
}

export function money(n?: number | null): string {
  if (n == null) return "–";
  return n < 1 ? `$${n.toFixed(3)}` : `$${n.toFixed(2)}`;
}

export function ago(iso?: string | null): string {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}
