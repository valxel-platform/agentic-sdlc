import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { api, type AuditEntry, type ChangeRecord, type Me } from "../api";
import { ActivityPage } from "./Activity";

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

const me: Me = {
  login: "ada",
  name: "Ada",
  avatar_url: "",
  orgs: [],
  teams: [],
  lanes: [{ id: "cln", name: "Clinic" }],
  roles: { ws1: "admin" },
};

function change(overrides: Record<string, unknown> = {}): ChangeRecord {
  return {
    id: "chg-1",
    at: "2026-11-17T10:00:00Z",
    product_id: "cln",
    env: "production",
    kind: "release",
    type: "standard",
    action: "deploy",
    summary: "Released triage queue",
    targets: [],
    feature_id: "CLN-1",
    release_id: "",
    incident_id: "",
    requested_by: "ada",
    approved_by: [],
    approvals: [],
    evidence: {},
    outcome: "done",
    detail: "",
    caused_incidents: [],
    hash: "aa",
    prev_hash: "bb",
    ...overrides,
  } as unknown as ChangeRecord;
}

function audit(overrides: Record<string, unknown> = {}): AuditEntry {
  return {
    at: "2026-11-18T09:00:00Z",
    actor: "sam",
    action: "member.add",
    target: "ada",
    ...overrides,
  } as AuditEntry;
}

function renderActivity(meOverride: Me = me) {
  return render(
    <MemoryRouter initialEntries={["/activity"]}>
      <ActivityPage me={meOverride} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  defaultRejectAll();
});

afterEach(() => cleanup());

describe("ActivityPage", () => {
  it("shows Loading… until the first log resolves", () => {
    let resolve!: (v: ChangeRecord[]) => void;
    vi.mocked(api.changes).mockReturnValue(
      new Promise<ChangeRecord[]>((r) => {
        resolve = r;
      }),
    );
    vi.mocked(api.workspaceAudit).mockResolvedValue([]);
    renderActivity();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    resolve([]);
  });

  it("merges changes and audit entries newest first with kind chips", async () => {
    vi.mocked(api.changes).mockResolvedValue([
      change(),
      change({
        id: "chg-2",
        at: "2026-11-16T10:00:00Z",
        summary: "Flag turned on",
        kind: "flag",
        env: "staging",
        feature_id: "",
      }),
    ]);
    vi.mocked(api.workspaceAudit).mockResolvedValue([audit()]);
    renderActivity();
    expect(await screen.findByText("Released triage queue (release, production)")).toBeInTheDocument();
    expect(screen.getByText("sam member.add — ada")).toBeInTheDocument();
    expect(screen.getByText("Flag turned on (flag, staging)")).toBeInTheDocument();
    expect(screen.getAllByText("Change")).toHaveLength(2);
    expect(screen.getByText("Workspace")).toBeInTheDocument();
    // Newest first: audit (11-18) before change (11-17) before older change.
    const items = screen.getAllByRole("listitem").map((li) => li.textContent);
    expect(items[0]).toContain("sam member.add");
    expect(items[1]).toContain("Released triage queue");
    expect(items[2]).toContain("Flag turned on");
    // Change rows link to their feature thread; the flag change has none.
    expect(screen.getByRole("link", { name: "Released triage queue (release, production)" })).toHaveAttribute(
      "href",
      "/features/CLN-1",
    );
    expect(screen.getByText("2026-11-18 09:00")).toBeInTheDocument();
    expect(screen.getByText(/Sealed history: entries are hash-chained/)).toBeInTheDocument();
  });

  it("shows the empty state when both logs are empty", async () => {
    vi.mocked(api.changes).mockResolvedValue([]);
    vi.mocked(api.workspaceAudit).mockResolvedValue([]);
    renderActivity();
    expect(await screen.findByRole("heading", { name: "Nothing here yet" })).toBeInTheDocument();
    expect(screen.getByText(/Changes and workspace events will appear here/)).toBeInTheDocument();
    expect(screen.getByText(/Sealed history: entries are hash-chained/)).toBeInTheDocument();
  });

  it("shows an error alert when the change log fails", async () => {
    vi.mocked(api.changes).mockRejectedValue(new Error("changes down"));
    vi.mocked(api.workspaceAudit).mockResolvedValue([]);
    renderActivity();
    expect(await screen.findByRole("alert")).toHaveTextContent("changes down");
  });

  it("notes the admin-only audit log when the audit call is denied", async () => {
    vi.mocked(api.changes).mockResolvedValue([change()]);
    vi.mocked(api.workspaceAudit).mockRejectedValue(new Error("forbidden"));
    renderActivity();
    expect(await screen.findByText("Released triage queue (release, production)")).toBeInTheDocument();
    expect(
      screen.getByText("The workspace audit log is only visible to workspace admins."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Workspace")).not.toBeInTheDocument();
  });

  it("renders audit entries without a target and shows only changes when there is no workspace", async () => {
    vi.mocked(api.changes).mockResolvedValue([change()]);
    vi.mocked(api.workspaceAudit).mockResolvedValue([audit({ target: undefined })]);
    renderActivity({ ...me, roles: {} });
    expect(await screen.findByText("Released triage queue (release, production)")).toBeInTheDocument();
    expect(vi.mocked(api.workspaceAudit)).not.toHaveBeenCalled();
    expect(screen.queryByText(/sam member.add/)).not.toBeInTheDocument();
  });

  it("shows the empty state immediately when there is no product and no workspace", async () => {
    renderActivity({ ...me, lanes: [], roles: {} });
    expect(await screen.findByRole("heading", { name: "Nothing here yet" })).toBeInTheDocument();
    expect(vi.mocked(api.changes)).not.toHaveBeenCalled();
    expect(vi.mocked(api.workspaceAudit)).not.toHaveBeenCalled();
  });
});
