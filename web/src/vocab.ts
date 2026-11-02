// Names people see. Keep them identical everywhere: a gate is always called by these words.
export const PHASES: Record<string, string> = {
  discovery: "Discovery", spec: "Spec", design: "Design", architecture: "Architecture",
  plan: "Plan", build: "Build", release: "Release", operate: "Operate", delivery: "Delivery", security: "Security", provisioning: "Repository setup",
};

const FAILED: Record<string, string> = {
  build: "OpenChoreo build", dev: "Dev deploy", staging: "Staging deploy", production: "Production deploy",
};

export const WAITS: Record<string, string> = {
  ci: "Waiting for CI", build: "Building in OpenChoreo", dev: "Deploying to dev",
  staging: "Deploying to staging", production: "Deploying to production",
};

export const GATES: Record<string, { name: string; question: string; approve: string }> = {
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

export function gateLabel(gate?: string): string {
  if (!gate) return "";
  const g = GATES[gate];
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
