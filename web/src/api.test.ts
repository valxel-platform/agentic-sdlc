import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { api, ApiError } from "./api";

// Transport tests mock global fetch; no test hits the network.

const fetchMock = vi.fn();
const origFetch = globalThis.fetch;

function okJson(data: unknown, status = 200) {
  return { ok: true, status, statusText: "OK", json: async () => data };
}

beforeEach(() => {
  fetchMock.mockReset();
  globalThis.fetch = fetchMock as unknown as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = origFetch;
});

// Guard against a loaded shared box timing a test out.
vi.setConfig({ testTimeout: 30000 });

describe("request() transport", () => {
  it("GET sends X-Valxel-Client, same-origin credentials, no body", async () => {
    fetchMock.mockResolvedValue(okJson({ ok: true }));
    const out = await api.me();
    expect(out).toEqual({ ok: true });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith("/api/me", {
      method: "GET",
      credentials: "same-origin",
      headers: { "X-Valxel-Client": "web" },
      body: undefined,
    });
  });

  it("POST sends JSON Content-Type and a stringified body", async () => {
    fetchMock.mockResolvedValue(okJson({ ok: true }));
    await api.logout();
    expect(fetchMock).toHaveBeenCalledWith("/api/auth/logout", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
  });

  it("POST with no body still sends the client header, not Content-Type", async () => {
    fetchMock.mockResolvedValue(okJson({ feature_id: "CLN-1" }));
    await api.firstFeature("CLN-PRD-1");
    expect(fetchMock).toHaveBeenCalledWith("/api/inceptions/CLN-PRD-1/first-feature", {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-Valxel-Client": "web" },
      body: undefined,
    });
  });

  it("204 resolves undefined without reading the body", async () => {
    const json = vi.fn(async () => ({}));
    fetchMock.mockResolvedValue({ ok: true, status: 204, statusText: "No Content", json });
    await expect(api.removeMember("ws1", "m1")).resolves.toBeUndefined();
    expect(json).not.toHaveBeenCalled();
  });

  it("string detail becomes the ApiError message", async () => {
    fetchMock.mockResolvedValue({
      ok: false,
      status: 404,
      statusText: "Not Found",
      json: async () => ({ detail: "No such feature" }),
    });
    const err = await api.feature("CLN-9").catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toBeInstanceOf(Error);
    expect((err as ApiError).status).toBe(404);
    expect((err as ApiError).message).toBe("No such feature");
  });

  it("detail.message is used when detail is an object", async () => {
    fetchMock.mockResolvedValue({
      ok: false,
      status: 400,
      statusText: "Bad Request",
      json: async () => ({ detail: { message: "Bad workspace id" } }),
    });
    const err = await api.workspace("!!").catch((e) => e);
    expect((err as ApiError).status).toBe(400);
    expect((err as ApiError).message).toBe("Bad workspace id");
  });

  it("detail without a message is JSON-stringified", async () => {
    fetchMock.mockResolvedValue({
      ok: false,
      status: 422,
      statusText: "Unprocessable",
      json: async () => ({ detail: { field: "name" } }),
    });
    const err = await api.createWorkspace({ id: "x", name: "", plan: "lite" }).catch((e) => e);
    expect((err as ApiError).message).toBe('{"field":"name"}');
  });

  it("null detail stringifies to 'null'", async () => {
    fetchMock.mockResolvedValue({
      ok: false,
      status: 500,
      statusText: "Error",
      json: async () => ({ detail: null }),
    });
    const err = await api.me().catch((e) => e);
    expect((err as ApiError).message).toBe("null");
  });

  it("non-JSON error body falls back to statusText", async () => {
    fetchMock.mockResolvedValue({
      ok: false,
      status: 503,
      statusText: "Service Unavailable",
      json: async () => {
        throw new Error("not json");
      },
    });
    const err = await api.features().catch((e) => e);
    expect((err as ApiError).status).toBe(503);
    expect((err as ApiError).message).toBe("Service Unavailable");
  });

  it("missing detail leaves an empty message but keeps the status", async () => {
    fetchMock.mockResolvedValue({
      ok: false,
      status: 500,
      statusText: "Server Error",
      json: async () => ({}),
    });
    const err = await api.me().catch((e) => e);
    expect((err as ApiError).status).toBe(500);
    expect((err as ApiError).message).toBe("");
  });

  it("returns the JSON payload as-is", async () => {
    fetchMock.mockResolvedValue(okJson([{ a: 1 }, { a: 2 }]));
    await expect(api.incidents()).resolves.toEqual([{ a: 1 }, { a: 2 }]);
  });
});

describe("intakeAttach", () => {
  it("posts the raw file as octet-stream with the encoded name", async () => {
    const file = new File(["hello"], "my file.png", { type: "image/png" });
    fetchMock.mockResolvedValue(okJson({ id: "s1" }));
    const out = await api.intakeAttach("s1", file);
    expect(out).toEqual({ id: "s1" });
    expect(fetchMock).toHaveBeenCalledWith("/api/intake/s1/attachments?name=my%20file.png", {
      method: "POST",
      credentials: "same-origin",
      body: file,
      headers: { "Content-Type": "application/octet-stream", "X-Valxel-Client": "web" },
    });
  });

  it("uses the server detail when the attach fails", async () => {
    const file = new File(["x"], "a.txt", { type: "text/plain" });
    fetchMock.mockResolvedValue({
      ok: false,
      status: 413,
      statusText: "Too Large",
      json: async () => ({ detail: "File too big" }),
    });
    await expect(api.intakeAttach("s1", file)).rejects.toMatchObject({
      status: 413,
      message: "File too big",
    });
  });

  it('falls back to "Couldn\'t attach <name>" when the detail is not a string', async () => {
    const file = new File(["x"], "a.txt", { type: "text/plain" });
    fetchMock.mockResolvedValue({
      ok: false,
      status: 400,
      statusText: "Bad Request",
      json: async () => ({ detail: { code: 1 } }),
    });
    await expect(api.intakeAttach("s1", file)).rejects.toMatchObject({ message: "Couldn't attach a.txt" });
  });

  it('falls back to "Couldn\'t attach <name>" on a non-JSON error body', async () => {
    const file = new File(["x"], "a.txt", { type: "text/plain" });
    fetchMock.mockResolvedValue({
      ok: false,
      status: 500,
      statusText: "Error",
      json: async () => {
        throw new Error("not json");
      },
    });
    await expect(api.intakeAttach("s1", file)).rejects.toMatchObject({ message: "Couldn't attach a.txt" });
  });
});

describe("traceCsvUrl", () => {
  it("builds the CSV URL without fetching", () => {
    expect(api.traceCsvUrl("CLN-1")).toBe("/api/features/CLN-1/trace.csv");
    expect(api.traceCsvUrl("a/b")).toBe("/api/features/a%2Fb/trace.csv");
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

type Case = {
  name: string;
  call: () => Promise<unknown>;
  method: string;
  path: string;
  body?: unknown;
  response?: unknown;
  status?: number;
};

const inceptionBody = {
  name: "Acme",
  org: "acme-org",
  vision: "A product that does the thing, well.",
  goals: ["Grow (measure: users)"],
  users: ["farmers"],
  constraints: ["offline-first"],
  budget_usd: 1000,
  product_id: "",
  research: "quick",
};

const cases: Case[] = [
  // Reads
  {
    name: "changes without since",
    call: () => api.changes("P1"),
    method: "GET",
    path: "/changes?product_id=P1",
  },
  {
    name: "changes with since",
    call: () => api.changes("P1", "2026-01-01"),
    method: "GET",
    path: "/changes?product_id=P1&since=2026-01-01",
  },
  { name: "verifyChanges", call: () => api.verifyChanges(), method: "GET", path: "/changes/verify" },
  {
    name: "lookback defaults to 6 months",
    call: () => api.lookback("P1"),
    method: "GET",
    path: "/reports/lookback?product_id=P1&months=6",
  },
  {
    name: "lookback with months",
    call: () => api.lookback("P1", 3),
    method: "GET",
    path: "/reports/lookback?product_id=P1&months=3",
  },
  { name: "inventory", call: () => api.inventory("P1"), method: "GET", path: "/inventory/P1" },
  { name: "featureCost", call: () => api.featureCost("F1"), method: "GET", path: "/features/F1/cost" },
  { name: "productCost without month", call: () => api.productCost("P1"), method: "GET", path: "/costs/P1" },
  {
    name: "productCost with month",
    call: () => api.productCost("P1", "2026-11"),
    method: "GET",
    path: "/costs/P1?month=2026-11",
  },
  { name: "authConfig", call: () => api.authConfig(), method: "GET", path: "/auth/config" },
  { name: "me", call: () => api.me(), method: "GET", path: "/me" },
  { name: "features", call: () => api.features(), method: "GET", path: "/features" },
  { name: "products", call: () => api.products(), method: "GET", path: "/products" },
  { name: "feature", call: () => api.feature("F1"), method: "GET", path: "/features/F1" },
  { name: "initiative", call: () => api.initiative("F1"), method: "GET", path: "/features/F1" },
  { name: "inception", call: () => api.inception("F1"), method: "GET", path: "/features/F1" },
  { name: "inceptions", call: () => api.inceptions(), method: "GET", path: "/inceptions" },
  { name: "release", call: () => api.release("F1"), method: "GET", path: "/features/F1" },
  { name: "review", call: () => api.review("F1"), method: "GET", path: "/features/F1" },
  { name: "incident", call: () => api.incident("F1"), method: "GET", path: "/features/F1" },
  { name: "incidents", call: () => api.incidents(), method: "GET", path: "/incidents" },
  { name: "unmatchedSignals", call: () => api.unmatchedSignals(), method: "GET", path: "/signals/unmatched" },
  { name: "roadmap", call: () => api.roadmap("P1"), method: "GET", path: "/roadmap/P1" },
  {
    name: "featureOutcome",
    call: () => api.featureOutcome("F1"),
    method: "GET",
    path: "/features/F1/outcome",
  },
  { name: "records", call: () => api.records("T1"), method: "GET", path: "/threads/T1/records" },
  {
    name: "artifact",
    call: () => api.artifact("F1", "PRD"),
    method: "GET",
    path: "/features/F1/artifacts/PRD",
  },
  { name: "inbox mine=true", call: () => api.inbox(true), method: "GET", path: "/inbox?mine=true" },
  { name: "inbox mine=false", call: () => api.inbox(false), method: "GET", path: "/inbox" },
  { name: "notifications", call: () => api.notifications(), method: "GET", path: "/me/notifications" },
  {
    name: "decisionBrief",
    call: () => api.decisionBrief("F1", "int1"),
    method: "GET",
    path: "/features/F1/decisions/int1/brief",
  },
  { name: "intakeList", call: () => api.intakeList(), method: "GET", path: "/intake" },
  { name: "intake", call: () => api.intake("s1"), method: "GET", path: "/intake/s1" },
  { name: "trace", call: () => api.trace("F1"), method: "GET", path: "/features/F1/trace" },
  { name: "workspaces", call: () => api.workspaces(), method: "GET", path: "/workspaces" },
  { name: "plans", call: () => api.plans(), method: "GET", path: "/plans" },
  { name: "billing", call: () => api.billing("ws1"), method: "GET", path: "/workspaces/ws1/billing" },
  { name: "workspace", call: () => api.workspace("ws1"), method: "GET", path: "/workspaces/ws1" },
  {
    name: "connections",
    call: () => api.connections("ws1"),
    method: "GET",
    path: "/workspaces/ws1/connections",
  },
  {
    name: "awsLaunch encodes region",
    call: () => api.awsLaunch("ws1", "full", "eu-west-1"),
    method: "GET",
    path: "/workspaces/ws1/connections/aws/launch?mode=full&region=eu-west-1",
  },
  {
    name: "workspaceAudit",
    call: () => api.workspaceAudit("ws1"),
    method: "GET",
    path: "/workspaces/ws1/audit",
  },
  { name: "lessons", call: () => api.lessons("P1"), method: "GET", path: "/products/P1/lessons" },
  {
    name: "pilotReport",
    call: () => api.pilotReport("P1"),
    method: "GET",
    path: "/reports/pilot?product_id=P1",
  },
  {
    name: "id with slash and space is encoded",
    call: () => api.feature("a/b c"),
    method: "GET",
    path: "/features/a%2Fb%20c",
  },
  {
    name: "product id with space is encoded in query",
    call: () => api.changes("P 1"),
    method: "GET",
    path: "/changes?product_id=P%201",
  },
  // Writes with a JSON body
  {
    name: "devLogin",
    call: () => api.devLogin("ada", ["t1"], ["o1"], "a@example.com"),
    method: "POST",
    path: "/auth/dev-login",
    body: { login: "ada", teams: ["t1"], orgs: ["o1"], email: "a@example.com" },
  },
  {
    name: "devLogin defaults email to empty",
    call: () => api.devLogin("ada", [], []),
    method: "POST",
    path: "/auth/dev-login",
    body: { login: "ada", teams: [], orgs: [], email: "" },
  },
  {
    name: "logout posts an empty object",
    call: () => api.logout(),
    method: "POST",
    path: "/auth/logout",
    body: {},
  },
  { name: "resume", call: () => api.resume("F1"), method: "POST", path: "/features/F1/resume", body: {} },
  { name: "syncWork", call: () => api.syncWork("F1"), method: "POST", path: "/features/F1/sync", body: {} },
  {
    name: "createInception passes the body through",
    call: () => api.createInception(inceptionBody),
    method: "POST",
    path: "/inceptions",
    body: inceptionBody,
  },
  {
    name: "reportProblem",
    call: () =>
      api.reportProblem({ product_id: "P1", title: "Down", description: "It is down", severity: "sev1" }),
    method: "POST",
    path: "/incidents",
    body: { product_id: "P1", title: "Down", description: "It is down", severity: "sev1" },
  },
  {
    name: "mergeIncident",
    call: () => api.mergeIncident("I1", "I2", "Same failure"),
    method: "POST",
    path: "/incidents/I1/merge",
    body: { into: "I2", reason: "Same failure" },
  },
  {
    name: "splitSignal",
    call: () => api.splitSignal("I1", "fp1"),
    method: "POST",
    path: "/incidents/I1/split",
    body: { fingerprint: "fp1" },
  },
  {
    name: "createInitiative",
    call: () => api.createInitiative({ product_id: "P1", title: "T", request: "R" }),
    method: "POST",
    path: "/initiatives",
    body: { product_id: "P1", title: "T", request: "R" },
  },
  {
    name: "startFeatures",
    call: () => api.startFeatures("I1", [0, 2], "together"),
    method: "POST",
    path: "/initiatives/I1/start",
    body: { indexes: [0, 2], order: "together" },
  },
  {
    name: "startCandidate defaults the reason",
    call: () => api.startCandidate("I1", 1),
    method: "POST",
    path: "/initiatives/I1/features",
    body: { index: 1, not_duplicate_reason: "" },
  },
  {
    name: "startCandidate with a reason",
    call: () => api.startCandidate("I1", 0, "not a duplicate"),
    method: "POST",
    path: "/initiatives/I1/features",
    body: { index: 0, not_duplicate_reason: "not a duplicate" },
  },
  {
    name: "createRelease",
    call: () => api.createRelease({ product_id: "P1", title: "R1", model: "launch", features: ["F1"] }),
    method: "POST",
    path: "/releases",
    body: { product_id: "P1", title: "R1", model: "launch", features: ["F1"] },
  },
  {
    name: "approveBatch defaults active_minutes to 0",
    call: () => api.approveBatch([{ feature_id: "F1", interrupt_id: "i1" }]),
    method: "POST",
    path: "/inbox/approve",
    body: { items: [{ feature_id: "F1", interrupt_id: "i1" }], active_minutes: 0 },
  },
  {
    name: "approveBatch with minutes",
    call: () => api.approveBatch([], 7),
    method: "POST",
    path: "/inbox/approve",
    body: { items: [], active_minutes: 7 },
  },
  {
    name: "decide passes the decision body through",
    call: () => api.decide("F1", "int1", { decision: "approve", active_minutes: 3 }),
    method: "POST",
    path: "/features/F1/decisions/int1",
    body: { decision: "approve", active_minutes: 3 },
  },
  {
    name: "intakeStart without a workspace",
    call: () => api.intakeStart(),
    method: "POST",
    path: "/intake",
    body: {},
  },
  {
    name: "intakeStart with a workspace",
    call: () => api.intakeStart("ws1"),
    method: "POST",
    path: "/intake",
    body: { workspace: "ws1" },
  },
  {
    name: "intakeSay",
    call: () => api.intakeSay("s1", "hello"),
    method: "POST",
    path: "/intake/s1/messages",
    body: { text: "hello" },
  },
  {
    name: "intakeDraft is a PATCH",
    call: () => api.intakeDraft("s1", { title: "New" }),
    method: "PATCH",
    path: "/intake/s1/draft",
    body: { title: "New" },
  },
  {
    name: "intakeSubmit defaults",
    call: () => api.intakeSubmit("s1"),
    method: "POST",
    path: "/intake/s1/submit",
    body: { pilot: false, profile: "" },
  },
  {
    name: "intakeSubmit with pilot and profile",
    call: () => api.intakeSubmit("s1", true, "express"),
    method: "POST",
    path: "/intake/s1/submit",
    body: { pilot: true, profile: "express" },
  },
  {
    name: "intakeDetach is a DELETE",
    call: () => api.intakeDetach("s1", "a1"),
    method: "DELETE",
    path: "/intake/s1/attachments/a1",
  },
  {
    name: "watch",
    call: () => api.watch("T1", true),
    method: "POST",
    path: "/threads/T1/watch",
    body: { on: true },
  },
  {
    name: "changeRequest is a PUT",
    call: () => api.changeRequest("F1", "please change this"),
    method: "PUT",
    path: "/features/F1/request",
    body: { description: "please change this" },
  },
  {
    name: "recordReading",
    call: () => api.recordReading("P1", "G1", { value: 42, start: "2026-10-01", end: "2026-10-31" }),
    method: "POST",
    path: "/products/P1/goals/G1/readings",
    body: { value: 42, start: "2026-10-01", end: "2026-10-31" },
  },
  {
    name: "setNotifications is a PUT",
    call: () => api.setNotifications({ email: "a@example.com" }),
    method: "PUT",
    path: "/me/notifications",
    body: { email: "a@example.com" },
  },
  {
    name: "setPlan is a PUT",
    call: () => api.setPlan("ws1", "full"),
    method: "PUT",
    path: "/workspaces/ws1/plan",
    body: { plan: "full" },
  },
  {
    name: "createWorkspace",
    call: () => api.createWorkspace({ id: "ws1", name: "Acme", plan: "lite" }),
    method: "POST",
    path: "/workspaces",
    body: { id: "ws1", name: "Acme", plan: "lite" },
  },
  {
    name: "invite",
    call: () =>
      api.invite("ws1", {
        name: "Ada",
        email: "a@example.com",
        github_login: "ada",
        role: "member",
        products: [],
      }),
    method: "POST",
    path: "/workspaces/ws1/members",
    body: { name: "Ada", email: "a@example.com", github_login: "ada", role: "member", products: [] },
  },
  {
    name: "resendInvite posts an empty object",
    call: () => api.resendInvite("ws1", "m1"),
    method: "POST",
    path: "/workspaces/ws1/members/m1/resend",
    body: {},
  },
  {
    name: "updateMember is a PATCH",
    call: () => api.updateMember("ws1", "m1", { role: "admin" }),
    method: "PATCH",
    path: "/workspaces/ws1/members/m1",
    body: { role: "admin" },
  },
  {
    name: "join posts an empty object",
    call: () => api.join("ws1", "tok123"),
    method: "POST",
    path: "/workspaces/ws1/join/tok123",
    body: {},
  },
  {
    name: "createGroup",
    call: () => api.createGroup("ws1", "Approvers"),
    method: "POST",
    path: "/workspaces/ws1/groups",
    body: { name: "Approvers" },
  },
  {
    name: "setGroup is a PUT",
    call: () => api.setGroup("ws1", "g1", ["a", "b"]),
    method: "PUT",
    path: "/workspaces/ws1/groups/g1",
    body: { members: ["a", "b"] },
  },
  {
    name: "setGates is a PUT",
    call: () => api.setGates("ws1", "P1", { g1: ["grp"] }),
    method: "PUT",
    path: "/workspaces/ws1/products/P1/gates",
    body: { gates: { g1: ["grp"] } },
  },
  {
    name: "awsCheck",
    call: () => api.awsCheck("ws1", "123456789012", "full"),
    method: "POST",
    path: "/workspaces/ws1/connections/aws/check",
    body: { account: "123456789012", mode: "full" },
  },
  // Writes with no body (client header, no Content-Type)
  {
    name: "firstFeature posts with no body",
    call: () => api.firstFeature("CLN-PRD-1"),
    method: "POST",
    path: "/inceptions/CLN-PRD-1/first-feature",
  },
  {
    name: "resize posts with no body",
    call: () => api.resize("T1"),
    method: "POST",
    path: "/threads/T1/resize",
  },
  {
    name: "mergeRecord posts with no body",
    call: () => api.mergeRecord("T1", "k1"),
    method: "POST",
    path: "/threads/T1/records/k1/merge",
  },
  // Deletes returning 204
  {
    name: "removeMember resolves undefined on 204",
    call: () => api.removeMember("ws1", "m1"),
    method: "DELETE",
    path: "/workspaces/ws1/members/m1",
    status: 204,
  },
  {
    name: "deleteGroup resolves undefined on 204",
    call: () => api.deleteGroup("ws1", "g1"),
    method: "DELETE",
    path: "/workspaces/ws1/groups/g1",
    status: 204,
  },
  {
    name: "disconnect resolves undefined on 204",
    call: () => api.disconnect("ws1", "github", "acct"),
    method: "DELETE",
    path: "/workspaces/ws1/connections/github/acct",
    status: 204,
  },
];

describe("api methods: URL / method / body shaping", () => {
  it.each(cases)("$name", async ({ call, method, path, body, response, status }) => {
    const payload = response ?? { ok: true };
    fetchMock.mockResolvedValue(
      status === 204
        ? { ok: true, status: 204, statusText: "No Content", json: async () => ({}) }
        : okJson(payload),
    );
    const out = call();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith(`/api${path}`, {
      method,
      credentials: "same-origin",
      headers: body === undefined ? { "X-Valxel-Client": "web" } : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (status === 204) await expect(out).resolves.toBeUndefined();
    else await expect(out).resolves.toEqual(payload);
  });
});
