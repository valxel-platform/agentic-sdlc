import type { Page } from "@playwright/test";
import type {
  FeatureSummary,
  InceptionSummary,
  InceptionView,
  IntakeSession,
  Me,
  PendingItem,
  ToolRow,
} from "../src/api";

/**
 * Shared API stubs for the E2E suite.
 *
 * The suite stubs the API at the network level (page.route) and runs against
 * vite preview with no live backend. Every route the app calls during a test
 * is answered here from in-memory state, so tests can flip that state (for
 * example, remove an approved decision from the inbox) and watch the UI
 * follow. Anything NOT stubbed below answers 404 JSON on purpose: vite
 * preview's SPA fallback would otherwise return index.html, which fetch
 * would fail to parse and surface as a confusing error.
 *
 * Known limitation: there is no test identity in this repo, so sign-in is
 * stubbed too — the session comes from GET /api/me, never from a real
 * GitHub OAuth or passkey flow, and no real credentials exist anywhere in
 * this suite. Real sign-in is a deployment concern, not something these
 * specs can prove.
 */

export const WS = "acme";
export const PRODUCT_ID = "CLN";

/** The signed-in user every spec uses: an owner of one workspace, on one product. */
export const ME: Me = {
  login: "ada",
  name: "Ada Lovelace",
  avatar_url: "",
  orgs: ["acme-water"],
  teams: ["acme-water/approvers"],
  lanes: [{ id: PRODUCT_ID, name: "Clinic" }],
  email: "ada@example.com",
  idp: "dev",
  strong: true,
  roles: { [WS]: "owner" },
  groups: [],
  products: { [WS]: [] },
};

/** The seven-stage rail every feature summary carries (Idea → Live). */
export function rail(state: { g2?: string; design?: string } = {}): FeatureSummary["rail"] {
  const g2 = state.g2 ?? "waiting";
  const design = state.design ?? "todo";
  return [
    { key: "discovery", kind: "phase", state: "done" },
    { key: "g1", kind: "gate", state: "done" },
    { key: "spec", kind: "phase", state: "done" },
    { key: "g2", kind: "gate", state: g2 },
    { key: "design", kind: "phase", state: design },
    { key: "g3", kind: "gate", state: "todo" },
    { key: "architecture", kind: "phase", state: "todo" },
    { key: "g4", kind: "gate", state: "todo" },
    { key: "build", kind: "phase", state: "todo" },
    { key: "g5", kind: "gate", state: "todo" },
    { key: "release", kind: "phase", state: "todo" },
    { key: "g6", kind: "gate", state: "todo" },
    { key: "operate", kind: "phase", state: "todo" },
  ];
}

export function featureSummary(over: Partial<FeatureSummary> = {}): FeatureSummary {
  return {
    feature_id: "VX-FEAT-101",
    product_id: PRODUCT_ID,
    title: "Add a CSV export to the visits list",
    status: "in_progress",
    running: true,
    rail: rail(),
    pending: [],
    updated_at: "2026-11-18T10:00:00Z",
    ...over,
  };
}

export function pendingItem(over: Partial<PendingItem> = {}): PendingItem {
  return {
    interrupt_id: "int-1",
    type: "gate",
    gate: "g2",
    approvers: ["product-council"],
    can_decide: true,
    feature_id: "VX-FEAT-101",
    product_id: PRODUCT_ID,
    product_name: "Clinic",
    title: "Add a CSV export to the visits list",
    cost_usd: 120,
    budget_usd: 500,
    ...over,
  };
}

export function toolRow(over: Partial<ToolRow> = {}): ToolRow {
  return {
    tool: "github",
    name: "GitHub",
    asked_when: "When a feature needs a repository",
    grants: "Read and write repositories in the orgs you choose.",
    revoke: "Remove the Valxel app from the GitHub org.",
    available: true,
    status: "not_connected",
    connections: [],
    ...over,
  };
}

export function inceptionView(over: Partial<InceptionView> = {}): InceptionView {
  return {
    feature_id: "VX-PRD-301",
    kind: "inception",
    product_id: "CLEANW",
    title: "Clean Water Monitor",
    created_by: ME.login,
    status: "researching",
    running: true,
    error: null,
    pending: [],
    events: [],
    cost_usd: 0,
    record_pr: {},
    vision: {
      name: "Clean Water Monitor",
      org: "acme-water",
      vision: "Farmers lose crops to dry fields they only discover on a walk-round.",
      goals: ["Fewer dry-field incidents (measure: under 2 a month per farm)"],
      users: ["Farm managers: out in the fields on a phone"],
      constraints: [],
      budget_usd: 50000,
      product_id: "CLEANW",
      existing: false,
    },
    brief: "",
    architecture: "",
    roadmap: "",
    reports: {},
    research: "",
    setup: {},
    council: ["product-council"],
    ...over,
  } as InceptionView;
}

function newIntakeSession(id: string, starter: boolean): IntakeSession {
  return {
    id,
    owner: ME.login,
    lanes: [PRODUCT_ID],
    draft: {
      product_id: "",
      title: "",
      problem: "",
      users: "",
      success_metric: "",
      related_capability: "",
      similar: [],
      not_duplicate_reason: "",
      ui: false,
      experience: {},
      size: "",
      size_suggested: "",
      size_why: "",
      horizon: "",
      research: "",
      name: "",
      org: "",
      budget_usd: 0,
    },
    ready: false,
    missing: ["product_id", "problem", "users", "success_metric"],
    attachments: [],
    feature_id: "",
    created_at: new Date().toISOString(),
    starter,
    kind: "",
    awaiting: "",
    messages: [
      {
        role: "agent",
        text: "Hi — describe what you want to build, in your own words.",
        at: new Date().toISOString(),
      },
    ],
  };
}

export interface DecidePost {
  featureId: string;
  interruptId: string;
  body: Record<string, unknown>;
}

/** Mutable stub state, one instance per test file (created in beforeEach). */
export class StubApi {
  signedIn = true;
  me: Me = { ...ME };
  authConfig: { mode: string; teams?: string[]; providers?: { id: string; label: string }[] } = {
    mode: "github",
    providers: [{ id: "github", label: "Continue with GitHub" }],
  };
  inboxItems: PendingItem[] = [];
  featureSummaries: FeatureSummary[] = [];
  /** GET /api/features/:id, keyed by id — features and inceptions alike. */
  featureDetails: Record<string, unknown> = {};
  connections: ToolRow[] = [];
  inceptions: InceptionSummary[] = [];
  products: Record<string, { name?: string; kind: string; ok: boolean }> = {
    [PRODUCT_ID]: { name: "Clinic", kind: "product", ok: true },
  };
  roadmap: Record<string, unknown> = {
    [PRODUCT_ID]: {
      product_id: PRODUCT_ID,
      record: { repo: "", sha: "", error: "" },
      goals: [],
      initiatives: [],
      releases: [],
      unplanned: [],
    },
  };
  productCost: Record<string, unknown> = {
    [PRODUCT_ID]: {
      product_id: PRODUCT_ID,
      month: "2026-11",
      provisional: false,
      totals: { run_usd: 0, build_usd: 0, total_usd: 0, build_to_date_usd: 0 },
      run: { by_category: [], by_env: {} },
      build: { by_category: [] },
      features: [],
    },
  };
  changes: unknown[] = [];
  audit: unknown[] = [];
  sessions: IntakeSession[] = [];
  /** Whether the next intake session may start a new product (inception starter). */
  starterForNext = false;
  /** The agent's reply to a chat message; tests may replace it. */
  agentReply: (text: string, session: IntakeSession) => string = () =>
    "Got it — anything else to add before you review the intent?";
  /** Called synchronously inside the decide route, before it answers. */
  onDecide: ((post: DecidePost) => void) | null = null;

  decidePosts: DecidePost[] = [];
  approveBatchPosts: unknown[] = [];
  inceptionPosts: Record<string, unknown>[] = [];
  firstFeaturePosts: string[] = [];
  disconnectCalls: { tool: string; account: string }[] = [];
  intakeSubmitPosts: { id: string; feature_id: string }[] = [];

  private findSession(pathname: string) {
    const id = decodeURIComponent(pathname.split("/")[3]);
    return this.sessions.find((s) => s.id === id);
  }

  private recompute(session: IntakeSession) {
    const d = session.draft;
    const filled = (v: unknown) => (typeof v === "string" ? v.trim() !== "" : Boolean(v));
    let missing: string[];
    if (d.size === "product") {
      missing = ["name", "org", "problem", "users", "success_metric", "research"].filter(
        (k) => !filled((d as unknown as Record<string, unknown>)[k]),
      );
    } else if (d.size === "initiative") {
      missing = ["product_id", "problem", "success_metric", "horizon", "research"].filter(
        (k) => !filled((d as unknown as Record<string, unknown>)[k]),
      );
    } else {
      missing = ["product_id", "problem", "users", "success_metric"].filter(
        (k) => !filled((d as unknown as Record<string, unknown>)[k]),
      );
      if (d.ui) {
        const exp = d.experience ?? {};
        const all = (["where", "main_task", "context", "look"] as const).every((k) => filled(exp[k]));
        if (!all) missing.push("experience");
      }
    }
    session.missing = missing;
    session.ready = missing.length === 0;
    session.kind =
      d.size === "product"
        ? "inception"
        : d.size === "initiative"
          ? "initiative"
          : d.size === "feature"
            ? "feature"
            : session.kind;
  }

  async install(page: Page) {
    // One catch-all route with an internal dispatcher. A single handler
    // keeps matching order out of the picture entirely: every /api call
    // the app makes is answered here, and anything not listed below
    // answers 404 JSON on purpose (vite preview's SPA fallback would
    // otherwise return index.html, which fetch fails to parse).
    await page.route("**/api/**", async (route) => {
      const req = route.request();
      const url = new URL(req.url());
      const path = url.pathname;
      const method = req.method();
      const json = (body: unknown, status = 200) => route.fulfill({ status, json: body });
      const body = <T>(): T => JSON.parse(req.postData() ?? "{}") as T;
      const seg = (n: number) => decodeURIComponent(path.split("/")[n] ?? "");

      // Live updates ride an EventSource the stubs never feed; abort it
      // so the app fails open to "Reconnecting…" instead of retrying.
      if (path === "/api/events") return route.abort();

      // Session
      if (path === "/api/me" && method === "GET") {
        return this.signedIn ? json(this.me) : json({ detail: "Not authenticated" }, 401);
      }
      if (path === "/api/auth/config" && method === "GET") return json(this.authConfig);
      if (path === "/api/auth/dev-login" && method === "POST") {
        this.signedIn = true;
        return json(this.me);
      }
      if (path === "/api/auth/logout" && method === "POST") return json({ ok: true });

      // Decisions
      if (path === "/api/inbox" && method === "GET") return json(this.inboxItems);
      if (path === "/api/inbox/approve" && method === "POST") {
        const b = body<{ items: { feature_id: string }[] }>();
        this.approveBatchPosts.push(b);
        return json({ approved: (b.items ?? []).map((i) => i.feature_id), skipped: [] });
      }
      if (path === "/api/features" && method === "GET") return json(this.featureSummaries);
      if (/^\/api\/features\/[^/]+$/.test(path) && method === "GET") {
        const detail = this.featureDetails[seg(3)];
        return detail ? json(detail) : json({ detail: "not found" }, 404);
      }
      if (path.endsWith("/brief") && method === "GET") {
        return json({
          summary: {
            lines: [
              "Agents drafted the scope from the discovery notes.",
              "Two repos change; no new data stores.",
            ],
            by: "agent",
          },
        });
      }
      if (/\/api\/features\/[^/]+\/decisions\/[^/]+$/.test(path) && method === "POST") {
        const post: DecidePost = { featureId: seg(3), interruptId: seg(5), body: body() };
        this.decidePosts.push(post);
        this.onDecide?.(post);
        return json({ accepted: true });
      }

      // Products and inception
      if (path === "/api/products" && method === "GET") return json(this.products);
      if (path === "/api/inceptions" && method === "GET") return json(this.inceptions);
      if (path === "/api/inceptions" && method === "POST") {
        const b = body<Record<string, unknown>>();
        this.inceptionPosts.push(b);
        const featureId = "VX-PRD-301";
        this.inceptions.push({
          id: featureId,
          title: String(b.name ?? ""),
          org: String(b.org ?? ""),
          existing: false,
          product_id: String(b.product_id ?? ""),
          status: "researching",
          created_by: this.me.login,
          cost_usd: 0,
          running: true,
        });
        return json({ feature_id: featureId });
      }
      if (/\/api\/inceptions\/[^/]+\/first-feature$/.test(path) && method === "POST") {
        this.firstFeaturePosts.push(seg(3));
        return json({ feature_id: "VX-FEAT-401" });
      }
      if (/^\/api\/threads\/[^/]+\/records$/.test(path) && method === "GET") return json([]);

      // Intake
      if (path === "/api/intake" && method === "GET") return json(this.sessions);
      if (path === "/api/intake" && method === "POST") {
        const session = newIntakeSession(`intake-${this.sessions.length + 1}`, this.starterForNext);
        this.sessions.push(session);
        return json(session);
      }
      if (/^\/api\/intake\/[^/]+$/.test(path) && method === "GET") {
        const session = this.findSession(path);
        return session ? json(session) : json({ detail: "no such intake" }, 404);
      }
      if (/\/api\/intake\/[^/]+\/messages$/.test(path) && method === "POST") {
        const session = this.findSession(path);
        if (!session) return json({ detail: "no such intake" }, 404);
        const { text } = body<{ text: string }>();
        const at = new Date().toISOString();
        session.messages.push({ role: "user", text, at });
        session.messages.push({ role: "agent", text: this.agentReply(text, session), at });
        return json(session);
      }
      if (/\/api\/intake\/[^/]+\/draft$/.test(path) && method === "PATCH") {
        const session = this.findSession(path);
        if (!session) return json({ detail: "no such intake" }, 404);
        const changes = body<Record<string, unknown>>();
        const { experience, ...rest } = changes;
        Object.assign(session.draft, rest);
        if (experience && typeof experience === "object") {
          Object.assign(session.draft.experience ?? {}, experience as Record<string, unknown>);
        }
        this.recompute(session);
        return json(session);
      }
      if (/\/api\/intake\/[^/]+\/submit$/.test(path) && method === "POST") {
        const session = this.findSession(path);
        if (!session) return json({ detail: "no such intake" }, 404);
        session.feature_id =
          session.kind === "inception"
            ? "VX-PRD-301"
            : session.kind === "initiative"
              ? "VX-INI-201"
              : "VX-FEAT-201";
        this.intakeSubmitPosts.push({ id: session.id, feature_id: session.feature_id });
        return json(session);
      }

      // Connected apps
      if (/^\/api\/workspaces\/[^/]+\/connections$/.test(path) && method === "GET") {
        return json(this.connections);
      }
      // The install URL is a full-page redirect to the vendor, and the
      // vendor would send the user back to /connected-apps. The stub
      // plays both parts: it flips the app to connected, then redirects
      // home. (Checked before the generic disconnect pattern below: an
      // install URL has the same shape as a disconnect URL.)
      if (/\/connections\/[^/]+\/install$/.test(path) && method === "GET") {
        const tool = path.split("/").at(-2)!;
        const row = this.connections.find((t) => t.tool === tool);
        if (row) {
          row.status = "connected";
          row.connections = [
            {
              tool,
              account: "acme-water",
              status: "connected",
              connected_by: this.me.login,
              connected_at: Date.now() / 1000,
              last_ok: Date.now() / 1000,
              last_error: "",
              reach: { selection: "all" },
            },
          ];
        }
        return route.fulfill({ status: 302, headers: { location: "/connected-apps" } });
      }
      if (/\/api\/workspaces\/[^/]+\/connections\/[^/]+\/[^/]+$/.test(path) && method === "DELETE") {
        const parts = path.split("/");
        const tool = parts[parts.length - 2];
        const account = decodeURIComponent(parts[parts.length - 1]);
        this.disconnectCalls.push({ tool, account });
        const row = this.connections.find((t) => t.tool === tool);
        if (row) {
          row.connections = row.connections.filter((c) => c.account !== account);
          if (row.connections.length === 0) row.status = "not_connected";
        }
        return json({ revoke: `Remove the Valxel app from ${row?.name ?? tool}.` });
      }
      if (/\/connections\/aws\/launch$/.test(path) && method === "GET") {
        return json({ url: "https://example.invalid/aws-console", mode: "lite" });
      }
      if (/\/connections\/aws\/check$/.test(path) && method === "POST") {
        const { account } = body<{ account: string }>();
        const row = this.connections.find((t) => t.tool === "aws");
        const connection = {
          tool: "aws",
          account,
          status: "connected",
          connected_by: this.me.login,
          connected_at: Date.now() / 1000,
          last_ok: Date.now() / 1000,
          last_error: "",
          reach: {},
        };
        if (row) {
          row.status = "connected";
          row.connections = [connection];
        }
        return json(connection);
      }

      // Pages the navigation spec visits
      if (/^\/api\/roadmap\/[^/]+$/.test(path) && method === "GET") {
        const id = seg(3);
        return json(
          this.roadmap[id] ?? {
            product_id: id,
            record: { repo: "", sha: "", error: "" },
            goals: [],
            initiatives: [],
            releases: [],
            unplanned: [],
          },
        );
      }
      if (/^\/api\/costs\/[^/]+$/.test(path) && method === "GET") {
        const id = seg(3);
        return json(
          this.productCost[id] ?? {
            product_id: id,
            month: "2026-11",
            provisional: false,
            totals: { run_usd: 0, build_usd: 0, total_usd: 0, build_to_date_usd: 0 },
            run: { by_category: [], by_env: {} },
            build: { by_category: [] },
            features: [],
          },
        );
      }
      if (path === "/api/workspaces" && method === "GET") {
        return json([{ id: WS, name: "Acme", role: "owner" }]);
      }
      if (path.startsWith("/api/changes") && method === "GET") return json(this.changes);
      if (/\/api\/workspaces\/[^/]+\/audit$/.test(path) && method === "GET") return json(this.audit);

      return json({ detail: `not stubbed in E2E: ${method} ${path}` }, 404);
    });
  }
}
