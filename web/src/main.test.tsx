import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { configure, fireEvent, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError, type Me } from "./api";

// The shell re-imports the whole app graph per test; on a shared VM the
// default 1 s async timeout is too tight, so waits get more room.
configure({ asyncUtilTimeout: 5000 });

// The first dynamic import of main.tsx transforms the whole page graph
// (including mermaid via the Feature page); give it room.
vi.setConfig({ testTimeout: 30000 });

// main.tsx mounts itself with createRoot at import time, so each test
// builds a fresh #root, sets the path, and dynamically imports it.
// The api module is mocked (real constants/classes kept); pages that mount
// under a route get empty-but-valid fixtures from the same mock.

const apiMock = vi.hoisted(() => {
  const store: Record<string, ReturnType<typeof vi.fn>> = {};
  return new Proxy(store, {
    get(target, prop: string) {
      if (!(prop in target)) target[prop] = vi.fn(async () => undefined);
      return target[prop];
    },
  });
});

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return { ...actual, api: apiMock };
});

class FakeSource {
  static instances: FakeSource[] = [];
  url: string;
  withCredentials: boolean;
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  closed = false;

  constructor(url: string, init?: EventSourceInit) {
    this.url = String(url);
    this.withCredentials = init?.withCredentials ?? false;
    FakeSource.instances.push(this);
  }

  close(): void {
    this.closed = true;
  }

  open(): void {
    this.onopen?.(new Event("open"));
  }

  fail(): void {
    this.onerror?.(new Event("error"));
  }

  emit(type: string): void {
    this.onmessage?.({ data: JSON.stringify({ type, at: "2026-11-18T12:00:00Z" }) } as MessageEvent);
  }
}

const origEventSource = globalThis.EventSource;

const me: Me = {
  login: "ada",
  name: "Ada Lovelace",
  avatar_url: "",
  orgs: ["acme"],
  teams: [],
  lanes: [{ id: "CLN", name: "Clean" }],
  roles: { acme: "admin" },
};

const newcomerMe: Me = { ...me, lanes: [], roles: {} };

const pendingItem = {
  interrupt_id: "i1",
  feature_id: "CLN-1",
  title: "Approve the idea",
  type: "gate",
  gate: "g1",
  approvers: ["ada"],
  can_decide: true,
};

async function renderApp(path: string) {
  document.body.innerHTML = "";
  const root = document.createElement("div");
  root.id = "root";
  document.body.appendChild(root);
  window.history.pushState({}, "", path);
  vi.resetModules();
  await import("./main");
  return root;
}

beforeEach(() => {
  vi.clearAllMocks();
  FakeSource.instances = [];
  globalThis.EventSource = origEventSource;
  apiMock.me.mockResolvedValue(me);
  apiMock.inbox.mockResolvedValue([]);
  apiMock.features.mockResolvedValue([]);
  apiMock.products.mockResolvedValue({});
  apiMock.connections.mockResolvedValue([]);
  apiMock.logout.mockResolvedValue({ ok: true });
  apiMock.authConfig.mockResolvedValue({
    mode: "github",
    providers: [{ id: "github", label: "Sign in with GitHub" }],
  });
  apiMock.plans.mockResolvedValue({
    plans: [],
    trial_days: 14,
    install_guide: "https://example.com/install",
  });
  apiMock.billing.mockResolvedValue(null);
});

afterEach(() => {
  document.body.innerHTML = "";
  globalThis.EventSource = origEventSource;
});

describe("App auth gating", () => {
  it("renders nothing while me() is pending", async () => {
    apiMock.me.mockReturnValue(new Promise(() => {}));
    const root = await renderApp("/today");
    await new Promise((r) => setTimeout(r, 50));
    expect(root.innerHTML).toBe("");
  });

  it("renders SignIn when me() rejects with 401", async () => {
    apiMock.me.mockRejectedValue(new ApiError(401, "Unauthorized"));
    await renderApp("/today");
    expect(await screen.findByText("Welcome back to Valxel")).toBeInTheDocument();
  });

  it("renders SignIn when me() rejects for any other reason", async () => {
    apiMock.me.mockRejectedValue(new Error("network down"));
    await renderApp("/today");
    expect(await screen.findByText("Welcome back to Valxel")).toBeInTheDocument();
  });
});

describe("root redirect", () => {
  it("sends a newcomer to /start", async () => {
    apiMock.me.mockResolvedValue(newcomerMe);
    await renderApp("/");
    expect(await screen.findByText("Set up Valxel")).toBeInTheDocument();
    expect(window.location.pathname).toBe("/start");
  });

  it("sends a returning user to /today", async () => {
    await renderApp("/");
    expect(await screen.findByText(/Here.s what needs you/)).toBeInTheDocument();
    expect(window.location.pathname).toBe("/today");
  });

  it("sends an unknown path to /inbox", async () => {
    await renderApp("/definitely-not-a-route");
    expect(await screen.findByRole("heading", { name: "Decisions" })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/inbox");
  });
});

describe("rail navigation", () => {
  it("renders the grouped rail links", async () => {
    await renderApp("/today");
    await screen.findByText(/Here.s what needs you/);
    const nav = screen.getAllByLabelText("Main")[0];
    for (const group of ["Work", "Decisions", "Operate", "Workspace"]) {
      expect(within(nav).getByText(group)).toBeInTheDocument();
    }
    for (const link of [
      "Today",
      "Features",
      "Products",
      "Roadmap",
      "New request",
      "Approvals",
      "Changes",
      "Incidents",
      "Pilot",
      "Costs",
      "Inventory",
      "Lessons",
      "Connected apps",
      "Activity",
      "Workspace admin",
      "Start page",
    ]) {
      expect(within(nav).getByRole("link", { name: new RegExp(link) })).toBeInTheDocument();
    }
    expect(within(nav).getAllByRole("link")).toHaveLength(16);
  });

  it("renders the mobile tab bar with four tabs", async () => {
    await renderApp("/today");
    await screen.findByText(/Here.s what needs you/);
    const tabbar = screen.getAllByLabelText("Main")[1];
    expect(within(tabbar).getAllByRole("link")).toHaveLength(4);
    expect(within(tabbar).getByRole("link", { name: /Today/ })).toBeInTheDocument();
    expect(within(tabbar).getByRole("link", { name: /More/ })).toBeInTheDocument();
  });

  it("shows the Decisions badge when items wait, and hides it at zero", async () => {
    apiMock.inbox.mockResolvedValue([pendingItem, { ...pendingItem, interrupt_id: "i2" }]);
    await renderApp("/today");
    expect(await screen.findAllByLabelText("2 waiting on you")).toHaveLength(2);
  });

  it("hides the badge when nothing waits", async () => {
    await renderApp("/today");
    await screen.findByText(/Here.s what needs you/);
    expect(screen.queryByLabelText(/waiting on you/)).not.toBeInTheDocument();
  });

  it("refreshes the badge on a feature.updated live event", async () => {
    globalThis.EventSource = FakeSource as unknown as typeof EventSource;
    let updated = false;
    apiMock.inbox.mockImplementation(async () =>
      updated ? [pendingItem, { ...pendingItem, interrupt_id: "i2" }] : [pendingItem],
    );
    await renderApp("/today");
    expect(await screen.findAllByLabelText("1 waiting on you")).toHaveLength(2);
    updated = true;
    FakeSource.instances[0]?.emit("feature.updated");
    await new Promise((r) => setTimeout(r, 350));
    expect(await screen.findAllByLabelText("2 waiting on you")).toHaveLength(2);
  });

  it("shows the apps note when connections load, and hides it on failure", async () => {
    apiMock.connections.mockResolvedValue([
      { tool: "github", status: "connected" },
      { tool: "jira", status: "not_connected" },
      { tool: "aws", status: "connected" },
    ]);
    await renderApp("/today");
    const note = await screen.findByRole("link", { name: "2 of 3 apps connected" });
    expect(note).toHaveAttribute("href", "/connected-apps");
  });

  it("hides the apps note when connections() fails", async () => {
    apiMock.connections.mockRejectedValue(new Error("down"));
    await renderApp("/today");
    await screen.findByText(/Here.s what needs you/);
    expect(screen.queryByText(/apps connected/)).not.toBeInTheDocument();
  });
});

describe("live pill", () => {
  it("shows Reconnecting… until the stream opens, then Live", async () => {
    globalThis.EventSource = FakeSource as unknown as typeof EventSource;
    await renderApp("/today");
    expect(await screen.findByText("Reconnecting…")).toBeInTheDocument();
    FakeSource.instances[0]?.open();
    expect(await screen.findByText("Live")).toBeInTheDocument();
    FakeSource.instances[0]?.fail();
    expect(await screen.findByText("Reconnecting…")).toBeInTheDocument();
  });
});

describe("command palette wiring", () => {
  it("opens the palette on ⌘K and on the search button, and closes on Escape", async () => {
    const user = userEvent.setup();
    await renderApp("/today");
    await screen.findByText(/Here.s what needs you/);
    fireEvent.keyDown(document, { key: "k", metaKey: true });
    expect(await screen.findByLabelText("Search projects, approvals, apps")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByLabelText("Search projects, approvals, apps")).not.toBeInTheDocument();
    await user.click(screen.getByLabelText("Search (Command K)"));
    expect(await screen.findByLabelText("Search projects, approvals, apps")).toBeInTheDocument();
  });
});

describe("account menu", () => {
  it("shows the initial when there is no avatar, and the avatar when there is one", async () => {
    await renderApp("/today");
    await screen.findByText(/Here.s what needs you/);
    expect(screen.getByText("A")).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("shows the avatar image when one is set", async () => {
    apiMock.me.mockResolvedValue({ ...me, avatar_url: "https://example.com/a.png" });
    await renderApp("/today");
    await screen.findByText(/Here.s what needs you/);
    const img = document.querySelector(".account img") as HTMLImageElement;
    expect(img).toBeInTheDocument();
    expect(img).toHaveAttribute("src", "https://example.com/a.png");
    expect(document.querySelector(".account .initial")).not.toBeInTheDocument();
  });

  it("labels the workspace link Workspaces with several roles, Workspace with one", async () => {
    const user = userEvent.setup();
    apiMock.me.mockResolvedValue({ ...me, roles: { acme: "admin", other: "member" } });
    await renderApp("/today");
    await screen.findByText(/Here.s what needs you/);
    await user.click(screen.getByText("Ada Lovelace"));
    expect(await screen.findByRole("link", { name: "Workspaces" })).toBeInTheDocument();
  });

  it("labels the workspace link Workspace with a single role", async () => {
    const user = userEvent.setup();
    await renderApp("/today");
    await screen.findByText(/Here.s what needs you/);
    await user.click(screen.getByText("Ada Lovelace"));
    const menu = document.querySelector(".account-menu") as HTMLElement;
    expect(within(menu).getByRole("link", { name: "Workspace" })).toBeInTheDocument();
    expect(within(menu).getByText("Signed in as")).toBeInTheDocument();
    expect(within(menu).getByRole("link", { name: "Set up a new workspace" })).toHaveAttribute(
      "href",
      "/start",
    );
    expect(within(menu).getByRole("link", { name: "Connected apps" })).toHaveAttribute(
      "href",
      "/connected-apps",
    );
    expect(within(menu).getByRole("link", { name: "Activity" })).toHaveAttribute("href", "/activity");
  });

  it("closes the menu on Escape and on an outside click", async () => {
    const user = userEvent.setup();
    await renderApp("/today");
    await screen.findByText(/Here.s what needs you/);
    await user.click(screen.getByText("Ada Lovelace"));
    const details = document.querySelector("details.account") as HTMLDetailsElement;
    details.open = true;
    await user.keyboard("{Escape}");
    expect(details.open).toBe(false);
    details.open = true;
    await user.click(document.body);
    expect(details.open).toBe(false);
  });

  it("signs out: logout, close the live stream, and back to SignIn", async () => {
    const user = userEvent.setup();
    globalThis.EventSource = FakeSource as unknown as typeof EventSource;
    await renderApp("/today");
    await screen.findByText(/Here.s what needs you/);
    await user.click(screen.getByText("Ada Lovelace"));
    await user.click(screen.getByRole("button", { name: "Sign out" }));
    expect(apiMock.logout).toHaveBeenCalledTimes(1);
    expect(FakeSource.instances[0]?.closed).toBe(true);
    expect(await screen.findByText("Welcome back to Valxel")).toBeInTheDocument();
  });

  it("returns to SignIn even when logout fails", async () => {
    const user = userEvent.setup();
    apiMock.logout.mockRejectedValue(new Error("logout failed"));
    await renderApp("/today");
    await screen.findByText(/Here.s what needs you/);
    await user.click(screen.getByText("Ada Lovelace"));
    await user.click(screen.getByRole("button", { name: "Sign out" }));
    expect(await screen.findByText("Welcome back to Valxel")).toBeInTheDocument();
  });
});

describe("shell edge branches", () => {
  it("renders SignIn when me() rejects with a non-401 ApiError", async () => {
    apiMock.me.mockRejectedValue(new ApiError(500, "Server error"));
    await renderApp("/today");
    expect(await screen.findByText("Welcome back to Valxel")).toBeInTheDocument();
  });

  it("falls back to the login on the More sign-out button when there is no display name", async () => {
    apiMock.me.mockResolvedValue({ ...me, name: "" });
    await renderApp("/more");
    expect(await screen.findByRole("button", { name: "Sign out (ada)" })).toBeInTheDocument();
  });

  it("falls back to the login when the user has no display name", async () => {
    apiMock.me.mockResolvedValue({ ...me, name: "" });
    await renderApp("/today");
    await screen.findByText(/Here.s what needs you/);
    const summary = document.querySelector("details.account summary") as HTMLElement;
    expect(summary.textContent).toContain("ada");
    expect(summary.textContent).not.toContain("Ada Lovelace");
  });

  it("renders without a workspace when roles is undefined", async () => {
    apiMock.me.mockResolvedValue({ ...me, roles: undefined });
    await renderApp("/today");
    await screen.findByText(/Here.s what needs you/);
    expect(apiMock.connections).not.toHaveBeenCalled();
    expect(screen.queryByText(/apps connected/)).not.toBeInTheDocument();
  });

  it("renders without a badge when the inbox count fails", async () => {
    apiMock.inbox.mockRejectedValue(new Error("inbox down"));
    await renderApp("/today");
    await screen.findByText(/Here.s what needs you/);
    expect(screen.queryByLabelText(/waiting on you/)).not.toBeInTheDocument();
  });

  it("opens the palette on Ctrl+K, ignores other keys, and toggles closed on a second ⌘K", async () => {
    await renderApp("/today");
    await screen.findByText(/Here.s what needs you/);
    fireEvent.keyDown(document, { key: "k", ctrlKey: true });
    expect(await screen.findByLabelText("Search projects, approvals, apps")).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "k", metaKey: true });
    expect(screen.queryByLabelText("Search projects, approvals, apps")).not.toBeInTheDocument();
    fireEvent.keyDown(document, { key: "x", metaKey: true });
    expect(screen.queryByLabelText("Search projects, approvals, apps")).not.toBeInTheDocument();
  });

  it("does not close the account menu on a non-Escape key", async () => {
    const user = userEvent.setup();
    await renderApp("/today");
    await screen.findByText(/Here.s what needs you/);
    await user.click(screen.getByText("Ada Lovelace"));
    const details = document.querySelector("details.account") as HTMLDetailsElement;
    details.open = true;
    await user.keyboard("{Enter}");
    expect(details.open).toBe(true);
  });

  it("does not refetch the badge on a non-matching live event", async () => {
    globalThis.EventSource = FakeSource as unknown as typeof EventSource;
    await renderApp("/today");
    await screen.findByText(/Here.s what needs you/);
    const calls = apiMock.inbox.mock.calls.length;
    expect(calls).toBeGreaterThan(0);
    FakeSource.instances[0]?.emit("signal.unmatched");
    await new Promise((r) => setTimeout(r, 350));
    expect(apiMock.inbox.mock.calls.length).toBe(calls);
  });
});

describe("routes", () => {
  it("/more lists every other section and signs out from there", async () => {
    const user = userEvent.setup();
    await renderApp("/more");
    expect(await screen.findByRole("heading", { name: "More" })).toBeInTheDocument();
    const list = screen.getByLabelText("More sections");
    for (const link of [
      "Products",
      "Roadmap",
      "Changes",
      "Incidents",
      "Pilot",
      "Costs",
      "Inventory",
      "Lessons",
      "Connected apps",
      "Activity",
      "Workspace admin",
      "Start page",
    ]) {
      expect(within(list).getByRole("link", { name: link })).toBeInTheDocument();
    }
    await user.click(screen.getByRole("button", { name: "Sign out (Ada Lovelace)" }));
    expect(apiMock.logout).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("Welcome back to Valxel")).toBeInTheDocument();
  });

  it("/board renders the features page", async () => {
    await renderApp("/board");
    expect(await screen.findByRole("heading", { name: "Features" })).toBeInTheDocument();
    expect(await screen.findByText("No features here yet")).toBeInTheDocument();
  });

  it("/inbox renders the decisions page", async () => {
    await renderApp("/inbox");
    expect(await screen.findByRole("heading", { name: "Decisions" })).toBeInTheDocument();
    expect(await screen.findByText("Nothing is waiting on you")).toBeInTheDocument();
  });

  it("/start renders the bare wizard without the rail", async () => {
    await renderApp("/start");
    expect(await screen.findByText("Set up Valxel")).toBeInTheDocument();
    expect(screen.queryByLabelText("Main")).not.toBeInTheDocument();
    expect(screen.queryByText("Today")).not.toBeInTheDocument();
  });
});
