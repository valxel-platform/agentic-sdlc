import { vi } from "vitest";

// One shared mock of the `api` object for every test file that mocks
// `../api`. Each test file does:
//
//   import { apiMock, resetApiMock } from "../test/apiMock";
//   vi.mock("../api", async (importOriginal) => {
//     const actual = await importOriginal<typeof import("../api")>();
//     return { ...actual, api: apiMock };
//   });
//
// The real module's named exports (ApiError, PROFILE_LABEL, ...) survive
// via the spread, so `instanceof ApiError` keeps working in tests.
// Defaults resolve to empty/safe values so a component that calls an
// un-stubbed method does not crash; each test overrides what it needs
// and calls resetApiMock() in beforeEach (reset re-applies the defaults,
// so per-test implementations never leak).

function prefs(over: Record<string, unknown> = {}) {
  return {
    email: "",
    needed: false,
    staging: false,
    watch: [],
    channels: { email: false, chat: false },
    sent: [],
    ...over,
  };
}

function applyDefaults() {
  apiMock.agentSteps.mockResolvedValue({ steps: [] });
  apiMock.changes.mockResolvedValue([]);
  apiMock.verifyChanges.mockResolvedValue({ ok: true, records: 0, broken_at: null });
  apiMock.lookback.mockResolvedValue({
    product_id: "",
    months: [],
    total: {
      incidents: 0,
      fixed_by_tool: 0,
      escalated: 0,
      releases: 0,
      releases_causing_incidents: 0,
      failed_changes: 0,
      change_failure_rate: null,
    },
    causes: [],
  });
  apiMock.inventory.mockResolvedValue({
    product_id: "",
    at: "",
    project: "",
    namespace: "",
    pipeline: [],
    items: [],
    summary: {
      per_env: {},
      production: { components: 0, resources: 0, store_apps: 0, cost_30d_usd: 0, planned_monthly_usd: 0 },
      lower: { components: 0, resources: 0, store_apps: 0, cost_30d_usd: 0, planned_monthly_usd: 0 },
    },
    unmanaged: [],
    aws: "",
    problems: [],
  });
  apiMock.featureCost.mockResolvedValue(undefined);
  apiMock.productCost.mockResolvedValue(undefined);
  apiMock.authConfig.mockResolvedValue({ mode: "dev" });
  apiMock.devLogin.mockResolvedValue(undefined);
  apiMock.logout.mockResolvedValue({ ok: true });
  apiMock.me.mockResolvedValue(undefined);
  apiMock.features.mockResolvedValue([]);
  apiMock.products.mockResolvedValue({});
  apiMock.resume.mockResolvedValue({ accepted: true });
  apiMock.syncWork.mockResolvedValue({});
  apiMock.feature.mockResolvedValue(undefined);
  apiMock.initiative.mockResolvedValue(undefined);
  apiMock.inception.mockResolvedValue(undefined);
  apiMock.inceptions.mockResolvedValue([]);
  apiMock.createInception.mockResolvedValue({ feature_id: "CLN-PRD-1" });
  apiMock.release.mockResolvedValue(undefined);
  apiMock.review.mockResolvedValue(undefined);
  apiMock.incident.mockResolvedValue(undefined);
  apiMock.mergeIncident.mockResolvedValue({ merged: "", into: "" });
  apiMock.splitSignal.mockResolvedValue({ new_incident: null });
  apiMock.incidents.mockResolvedValue([]);
  apiMock.unmatchedSignals.mockResolvedValue({ count: 0, signals: [] });
  apiMock.reportProblem.mockResolvedValue({ feature_id: "CLN-INC-1" });
  apiMock.roadmap.mockResolvedValue(undefined);
  apiMock.recordReading.mockResolvedValue({ reading: {}, readouts: [] });
  apiMock.featureOutcome.mockResolvedValue(undefined);
  apiMock.createInitiative.mockResolvedValue({ feature_id: "CLN-INI-1" });
  apiMock.records.mockResolvedValue([]);
  apiMock.mergeRecord.mockResolvedValue({});
  apiMock.startFeatures.mockResolvedValue({ started: [], queued: [] });
  apiMock.firstFeature.mockResolvedValue({ feature_id: "CLN-1" });
  apiMock.startCandidate.mockResolvedValue({ feature_id: "CLN-1" });
  apiMock.createRelease.mockResolvedValue({ feature_id: "CLN-REL-1" });
  apiMock.artifact.mockResolvedValue({ id: "", content: "", version: 1, constitution_version: "v1" });
  apiMock.inbox.mockResolvedValue([]);
  apiMock.notifications.mockResolvedValue(prefs());
  apiMock.setNotifications.mockImplementation(async (b: Record<string, unknown>) => prefs(b));
  apiMock.watch.mockResolvedValue({ watching: true });
  apiMock.changeRequest.mockResolvedValue({ feature_id: "", sent_back: "", note: "" });
  apiMock.approveBatch.mockResolvedValue({ approved: [], skipped: [] });
  apiMock.decisionBrief.mockResolvedValue(null);
  apiMock.reading.mockResolvedValue({ seconds: 0 });
  apiMock.decide.mockResolvedValue({ accepted: true });
  apiMock.intakeStart.mockResolvedValue(undefined);
  apiMock.intakeList.mockResolvedValue([]);
  apiMock.intake.mockResolvedValue(undefined);
  apiMock.intakeSay.mockResolvedValue(undefined);
  apiMock.intakeDraft.mockResolvedValue(undefined);
  apiMock.resize.mockResolvedValue({ feature_id: "CLN-INI-1" });
  apiMock.intakeAttach.mockResolvedValue(undefined);
  apiMock.intakeDetach.mockResolvedValue(undefined);
  apiMock.intakeSubmit.mockResolvedValue(undefined);
  apiMock.trace.mockResolvedValue(undefined);
  apiMock.traceCsvUrl.mockImplementation((id: string) => `/api/features/${encodeURIComponent(id)}/trace.csv`);
  apiMock.workspaces.mockResolvedValue([]);
  apiMock.plans.mockResolvedValue({ plans: [], trial_days: 0, install_guide: "" });
  apiMock.billing.mockResolvedValue(undefined);
  apiMock.setPlan.mockResolvedValue(undefined);
  apiMock.createWorkspace.mockResolvedValue(undefined);
  apiMock.workspace.mockResolvedValue(undefined);
  apiMock.invite.mockResolvedValue(undefined);
  apiMock.resendInvite.mockResolvedValue({ invite_url: "", emailed: false });
  apiMock.updateMember.mockResolvedValue(undefined);
  apiMock.removeMember.mockResolvedValue(undefined);
  apiMock.join.mockResolvedValue(undefined);
  apiMock.createGroup.mockResolvedValue(undefined);
  apiMock.setGroup.mockResolvedValue(undefined);
  apiMock.deleteGroup.mockResolvedValue(undefined);
  apiMock.setGates.mockResolvedValue({ version: 1 });
  apiMock.connections.mockResolvedValue([]);
  apiMock.awsLaunch.mockResolvedValue({ url: "", mode: "lite" });
  apiMock.awsCheck.mockResolvedValue(undefined);
  apiMock.disconnect.mockResolvedValue({ revoke: "" });
  apiMock.workspaceAudit.mockResolvedValue([]);
  apiMock.lessons.mockResolvedValue(undefined);
  apiMock.pilotReport.mockResolvedValue(undefined);
}

export const apiMock = {
  changes: vi.fn(),
  verifyChanges: vi.fn(),
  lookback: vi.fn(),
  inventory: vi.fn(),
  featureCost: vi.fn(),
  productCost: vi.fn(),
  authConfig: vi.fn(),
  devLogin: vi.fn(),
  logout: vi.fn(),
  me: vi.fn(),
  features: vi.fn(),
  products: vi.fn(),
  resume: vi.fn(),
  syncWork: vi.fn(),
  feature: vi.fn(),
  agentSteps: vi.fn(),
  initiative: vi.fn(),
  inception: vi.fn(),
  inceptions: vi.fn(),
  createInception: vi.fn(),
  release: vi.fn(),
  review: vi.fn(),
  incident: vi.fn(),
  mergeIncident: vi.fn(),
  splitSignal: vi.fn(),
  incidents: vi.fn(),
  unmatchedSignals: vi.fn(),
  reportProblem: vi.fn(),
  roadmap: vi.fn(),
  recordReading: vi.fn(),
  featureOutcome: vi.fn(),
  setShip: vi.fn(),
  createInitiative: vi.fn(),
  records: vi.fn(),
  mergeRecord: vi.fn(),
  startFeatures: vi.fn(),
  firstFeature: vi.fn(),
  startCandidate: vi.fn(),
  createRelease: vi.fn(),
  artifact: vi.fn(),
  inbox: vi.fn(),
  notifications: vi.fn(),
  setNotifications: vi.fn(),
  watch: vi.fn(),
  changeRequest: vi.fn(),
  approveBatch: vi.fn(),
  decisionBrief: vi.fn(),
  reading: vi.fn(),
  decide: vi.fn(),
  intakeStart: vi.fn(),
  intakeList: vi.fn(),
  intake: vi.fn(),
  intakeSay: vi.fn(),
  intakeDraft: vi.fn(),
  resize: vi.fn(),
  intakeAttach: vi.fn(),
  intakeDetach: vi.fn(),
  intakeSubmit: vi.fn(),
  trace: vi.fn(),
  traceCsvUrl: vi.fn(),
  workspaces: vi.fn(),
  plans: vi.fn(),
  billing: vi.fn(),
  setPlan: vi.fn(),
  createWorkspace: vi.fn(),
  workspace: vi.fn(),
  invite: vi.fn(),
  resendInvite: vi.fn(),
  updateMember: vi.fn(),
  removeMember: vi.fn(),
  join: vi.fn(),
  createGroup: vi.fn(),
  setGroup: vi.fn(),
  deleteGroup: vi.fn(),
  setGates: vi.fn(),
  connections: vi.fn(),
  awsLaunch: vi.fn(),
  awsCheck: vi.fn(),
  disconnect: vi.fn(),
  workspaceAudit: vi.fn(),
  lessons: vi.fn(),
  pilotReport: vi.fn(),
};

applyDefaults();

export function resetApiMock() {
  for (const f of Object.values(apiMock)) f.mockReset();
  applyDefaults();
}
