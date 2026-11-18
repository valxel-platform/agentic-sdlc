import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { api, type FeatureSummary, type Me, type PendingItem, type ToolRow } from "../api";
import { TodayPage } from "./Today";

vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  const mocked = Object.fromEntries(Object.keys(actual.api).map((k) => [k, vi.fn()]));
  return { ...actual, api: mocked };
});

type MockFn = { mockRejectedValue: (e: unknown) => unknown };

function defaultRejectAll() {
  for (const fn of Object.values(api)) {
    (fn as unknown as MockFn).mockRejectedValue(new Error("not mocked"));
  }
}

function pending(overrides: Partial<PendingItem> = {}): PendingItem {
  return {
    interrupt_id: "int-1",
    type: "gate",
    gate: "g1",
    title: "Triage queue",
    feature_id: "CLN-1",
    product_id: "cln",
    approvers: ["approvers"],
    can_decide: true,
    ...overrides,
  } as PendingItem;
}

function feature(overrides: Record<string, unknown> = {}): FeatureSummary {
  return {
    feature_id: "CLN-1",
    product_id: "cln",
    title: "Triage queue",
    status: "in_progress",
    running: true,
    rail: [{ key: "discovery", kind: "phase", state: "done" }],
    pending: [],
    updated_at: new Date().toISOString(),
    ...overrides,
  } as unknown as FeatureSummary;
}

function tool(overrides: Record<string, unknown> = {}): ToolRow {
  return {
    tool: "github",
    name: "GitHub",
    asked_when: "2026-01-01",
    grants: "Read repos",
    revoke: "revoke on GitHub",
    available: true,
    status: "connected",
    connections: [],
    ...overrides,
  } as unknown as ToolRow;
}

const me: Me = {
  login: "ada",
  name: "Ada",
  avatar_url: "",
  orgs: [],
  teams: [],
  lanes: [{ id: "cln", name: "Clinic" }],
  roles: { ws1: "admin" },
};

function renderToday(meOverride: Me = me) {
  return render(
    <MemoryRouter initialEntries={["/today"]}>
      <TodayPage me={meOverride} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  defaultRejectAll();
});

afterEach(() => cleanup());

describe("TodayPage", () => {
  it("greets and shows decisions waiting with links to the threads", async () => {
    vi.mocked(api.inbox).mockResolvedValue([
      pending(),
      pending({ interrupt_id: "int-2", feature_id: "CLN-2", title: "Alerts" }),
    ]);
    vi.mocked(api.features).mockResolvedValue([]);
    vi.mocked(api.connections).mockResolvedValue([]);
    renderToday();
    expect(await screen.findByText(/Here’s what needs you\./)).toBeInTheDocument();
    expect(screen.getByText("2 decisions waiting")).toBeInTheDocument();
    expect(screen.getByText("Triage queue")).toBeInTheDocument();
    expect(screen.getByText("Alerts")).toBeInTheDocument();
    const decideLinks = screen.getAllByRole("link", { name: "Decide" });
    expect(decideLinks).toHaveLength(2);
    expect(decideLinks[0]).toHaveAttribute("href", "/features/CLN-1");
    expect(screen.getByRole("link", { name: "All decisions" })).toHaveAttribute("href", "/inbox");
    expect(screen.getAllByText("Waiting for Idea approval (G1)")).toHaveLength(2);
  });

  it("shows the all-caught-up state when nothing waits", async () => {
    vi.mocked(api.inbox).mockResolvedValue([]);
    vi.mocked(api.features).mockResolvedValue([]);
    vi.mocked(api.connections).mockResolvedValue([]);
    renderToday();
    expect(await screen.findByText("Nothing is waiting on you right now.")).toBeInTheDocument();
    expect(
      screen.getByText("All caught up. Agents are working, or every feature has finished."),
    ).toBeInTheDocument();
  });

  it("shows an error alert when the inbox fails but keeps the rest", async () => {
    vi.mocked(api.inbox).mockRejectedValue(new Error("inbox down"));
    vi.mocked(api.features).mockResolvedValue([feature()]);
    vi.mocked(api.connections).mockResolvedValue([]);
    renderToday();
    expect(await screen.findByRole("alert")).toHaveTextContent("inbox down");
    // The feature shows in both Your projects and Latest activity.
    expect(screen.getAllByText("Triage queue")).toHaveLength(2);
  });

  it("swallows a features failure and shows no projects", async () => {
    vi.mocked(api.inbox).mockResolvedValue([]);
    vi.mocked(api.features).mockRejectedValue(new Error("features down"));
    vi.mocked(api.connections).mockResolvedValue([]);
    renderToday();
    expect(await screen.findByText(/No features in flight\./)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("lists in-flight projects with mini steppers, filtering done and aborted", async () => {
    vi.mocked(api.inbox).mockResolvedValue([]);
    vi.mocked(api.features).mockResolvedValue([
      feature(),
      feature({ feature_id: "CLN-2", title: "Alerts", status: "done" }),
      feature({ feature_id: "CLN-3", title: "Dropped work", status: "aborted" }),
      feature({ feature_id: "CLN-4", title: "Second project", updated_at: "2026-11-01T00:00:00Z" }),
    ]);
    vi.mocked(api.connections).mockResolvedValue([]);
    renderToday();
    // Each in-flight project also appears in Latest activity.
    expect(await screen.findAllByText("Triage queue")).toHaveLength(2);
    expect(screen.getAllByText("Second project")).toHaveLength(2);
    // Done/aborted features are filtered out of Your projects (they still
    // show in Latest activity, which lists every feature by update time).
    const projects = screen.getByRole("heading", { name: "Your projects" }).closest("section")!;
    expect(within(projects).queryByText("Alerts")).not.toBeInTheDocument();
    expect(within(projects).queryByText("Dropped work")).not.toBeInTheDocument();
    expect(screen.getAllByLabelText("Where this is, from idea to live")).toHaveLength(2);
    expect(screen.getByRole("link", { name: "All features" })).toHaveAttribute("href", "/board");
  });

  it("shows the empty projects state with a start link", async () => {
    vi.mocked(api.inbox).mockResolvedValue([]);
    vi.mocked(api.features).mockResolvedValue([]);
    vi.mocked(api.connections).mockResolvedValue([]);
    renderToday();
    expect(await screen.findByText(/No features in flight\./)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Start a request" })).toHaveAttribute("href", "/intake");
  });

  it("summarises connected apps from the first workspace role", async () => {
    vi.mocked(api.inbox).mockResolvedValue([]);
    vi.mocked(api.features).mockResolvedValue([]);
    vi.mocked(api.connections).mockResolvedValue([
      tool(),
      tool({ tool: "jira", name: "Jira", status: "needs_attention" }),
      tool({ tool: "sentry", name: "Sentry", status: "not_connected" }),
    ]);
    renderToday();
    expect(await screen.findByText("1 of 3 apps connected")).toBeInTheDocument();
    expect(screen.getByText("GitHub: Connected")).toBeInTheDocument();
    expect(screen.getByText("Jira: Needs attention")).toBeInTheDocument();
    expect(screen.getByText("Sentry: Not connected")).toBeInTheDocument();
    expect(vi.mocked(api.connections)).toHaveBeenCalledWith("ws1");
    expect(screen.getByRole("link", { name: "Manage connections" })).toHaveAttribute(
      "href",
      "/connected-apps",
    );
  });

  it("shows the no-workspace note when the user has no roles", async () => {
    vi.mocked(api.inbox).mockResolvedValue([]);
    vi.mocked(api.features).mockResolvedValue([]);
    renderToday({ ...me, roles: {} });
    expect(await screen.findByText("No workspace to check connections for yet.")).toBeInTheDocument();
    expect(vi.mocked(api.connections)).not.toHaveBeenCalled();
  });

  it("shows the latest activity sorted newest first, capped at four", async () => {
    vi.mocked(api.inbox).mockResolvedValue([]);
    vi.mocked(api.features).mockResolvedValue([
      feature({ feature_id: "CLN-1", title: "Newest", updated_at: "2026-11-18T10:00:00Z" }),
      feature({ feature_id: "CLN-2", title: "Middle", updated_at: "2026-11-17T10:00:00Z" }),
      feature({ feature_id: "CLN-3", title: "Older", updated_at: "2026-11-16T10:00:00Z" }),
      feature({ feature_id: "CLN-4", title: "Oldest", updated_at: "2026-11-15T10:00:00Z" }),
      feature({ feature_id: "CLN-5", title: "Too old", updated_at: "2026-11-14T10:00:00Z" }),
    ]);
    vi.mocked(api.connections).mockResolvedValue([]);
    renderToday();
    // Each activity item also appears in Your projects.
    expect(await screen.findAllByText("Newest")).toHaveLength(2);
    const activity = screen.getByRole("heading", { name: "Latest activity" }).closest("section")!;
    const links = Array.from(activity.querySelectorAll("a")).map((a) => a.textContent);
    expect(links).toEqual(["Newest", "Middle", "Older", "Oldest", "Full history"]);
    expect(screen.getByRole("link", { name: "Full history" })).toHaveAttribute("href", "/activity");
  });

  it("shows the empty activity state", async () => {
    vi.mocked(api.inbox).mockResolvedValue([]);
    vi.mocked(api.features).mockResolvedValue([]);
    vi.mocked(api.connections).mockResolvedValue([]);
    renderToday();
    expect(await screen.findByText("Nothing yet.")).toBeInTheDocument();
  });

  it("caps the decisions list at three", async () => {
    vi.mocked(api.inbox).mockResolvedValue([
      pending({ interrupt_id: "i1" }),
      pending({ interrupt_id: "i2", feature_id: "CLN-2", title: "Second" }),
      pending({ interrupt_id: "i3", feature_id: "CLN-3", title: "Third" }),
      pending({ interrupt_id: "i4", feature_id: "CLN-4", title: "Fourth" }),
    ]);
    vi.mocked(api.features).mockResolvedValue([]);
    vi.mocked(api.connections).mockResolvedValue([]);
    renderToday();
    expect(await screen.findByText("4 decisions waiting")).toBeInTheDocument();
    expect(screen.getByText("Third")).toBeInTheDocument();
    expect(screen.queryByText("Fourth")).not.toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "Decide" })).toHaveLength(3);
  });

  it("loads the inbox with mine=true and refetches on live updates", async () => {
    vi.mocked(api.inbox).mockResolvedValue([]);
    vi.mocked(api.features).mockResolvedValue([]);
    vi.mocked(api.connections).mockResolvedValue([]);
    renderToday();
    await screen.findByText("Nothing is waiting on you right now.");
    expect(vi.mocked(api.inbox)).toHaveBeenCalledWith(true);
    expect(vi.mocked(api.features)).toHaveBeenCalled();
  });

  it.each([
    [9, "Good morning"],
    [14, "Good afternoon"],
    [20, "Good evening"],
  ])("greets with %s o'clock wording", async (hour, greeting) => {
    const spy = vi.spyOn(Date.prototype, "getHours").mockReturnValue(hour);
    try {
      vi.mocked(api.inbox).mockResolvedValue([]);
      vi.mocked(api.features).mockResolvedValue([]);
      vi.mocked(api.connections).mockResolvedValue([]);
      renderToday();
      expect(
        await screen.findByRole("heading", { name: new RegExp(`^${greeting}\\. Here’s what needs you\\.$`) }),
      ).toBeInTheDocument();
    } finally {
      spy.mockRestore();
    }
  });

  it("says one decision waiting and links to the inbox when there is no feature", async () => {
    vi.mocked(api.inbox).mockResolvedValue([pending({ feature_id: undefined, title: "Orphan decision" })]);
    vi.mocked(api.features).mockResolvedValue([]);
    vi.mocked(api.connections).mockResolvedValue([]);
    renderToday();
    expect(await screen.findByText("1 decision waiting")).toBeInTheDocument();
    expect(screen.getByText("Orphan decision")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Decide" })).toHaveAttribute("href", "/inbox");
  });
});
