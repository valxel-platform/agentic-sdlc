import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { api, type Group, type Me, type Member, type ToolRow, type WorkspaceView } from "../api";
import { JoinPage, WorkspacePage, WorkspacesPage } from "./Workspace";

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

function member(overrides: Partial<Member> = {}): Member {
  return {
    id: "m-ada",
    name: "Ada",
    email: "ada@example.com",
    github_login: "ada",
    role: "owner",
    products: [],
    status: "active",
    ...overrides,
  };
}

function group(overrides: Partial<Group> = {}): Group {
  return { slug: "approvers", name: "Approvers", members: ["m-ada"], ...overrides };
}

function workspaceFixture(overrides: Record<string, unknown> = {}): WorkspaceView {
  return {
    id: "ws1",
    name: "Acme",
    plan: "lite",
    role: "owner",
    members: [
      member(),
      member({
        id: "m-sam",
        name: "Sam",
        email: "sam@example.com",
        github_login: "sam",
        role: "approver",
        products: ["cln"],
        status: "active",
      }),
      member({
        id: "m-jo",
        name: "Jo",
        email: "jo@example.com",
        github_login: "jo",
        role: "member",
        products: [],
        status: "invited",
      }),
      member({
        id: "m-kim",
        name: "Kim",
        email: "kim@example.com",
        github_login: "kim",
        role: "viewer",
        products: [],
        status: "suspended",
      }),
    ],
    groups: [group(), group({ slug: "qa", name: "QA", members: [] })],
    products: [
      { id: "cln", gates: { g1: ["approvers"], g2: ["approvers"] }, version: 2 },
      { id: "ops", gates: {}, version: 0 },
    ],
    ...overrides,
  } as unknown as WorkspaceView;
}

function toolRow(overrides: Record<string, unknown> = {}): ToolRow {
  return {
    tool: "github",
    name: "GitHub",
    asked_when: "2026-01-01",
    grants: "Read repos",
    revoke: "revoke on GitHub",
    available: true,
    status: "connected",
    connections: [
      {
        tool: "github",
        account: "acme",
        status: "connected",
        connected_by: "ada",
        connected_at: 1700000000,
        last_ok: 1763000000,
        last_error: "",
        reach: { account_type: "org", selection: "all" },
      },
    ],
    ...overrides,
  } as unknown as ToolRow;
}

const baseMe: Me = {
  login: "ada",
  name: "Ada",
  avatar_url: "",
  orgs: [],
  teams: [],
  lanes: [{ id: "cln", name: "Clinic" }],
  email: "ada@example.com",
};

let originalLocation: Location;

function stubLocation() {
  originalLocation = window.location;
  Object.defineProperty(window, "location", {
    value: { href: "http://localhost/" },
    writable: true,
    configurable: true,
  });
}

function restoreLocation() {
  Object.defineProperty(window, "location", { value: originalLocation, writable: true, configurable: true });
}

function renderWorkspace(path = "/workspaces/ws1", me: Me = baseMe, onChanged = vi.fn()) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/workspaces" element={<WorkspacesPage />} />
        <Route path="/workspaces/:id" element={<WorkspacePage me={me} onChanged={onChanged} />} />
        <Route path="/join/:ws/:token" element={<JoinPage onJoined={onChanged} />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  defaultRejectAll();
  stubLocation();
  vi.spyOn(window, "confirm").mockReturnValue(true);
  vi.spyOn(window, "open").mockReturnValue(null);
});

afterEach(() => {
  cleanup();
  restoreLocation();
  vi.restoreAllMocks();
});

describe("WorkspacesPage", () => {
  it("lists workspaces with role and id", async () => {
    vi.mocked(api.workspaces).mockResolvedValue([
      { id: "ws1", name: "Acme", plan: "lite", role: "owner" },
      { id: "ws2", name: "Beta", plan: "full", role: "member" },
    ]);
    renderWorkspace("/workspaces");
    expect(await screen.findByText("Acme")).toBeInTheDocument();
    expect(screen.getByText("Beta")).toBeInTheDocument();
    expect(screen.getByText("owner")).toBeInTheDocument();
    expect(screen.getByText("ws2")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Acme" })).toHaveAttribute("href", "/workspaces/ws1");
  });

  it("shows a load error", async () => {
    vi.mocked(api.workspaces).mockRejectedValue(new Error("workspaces down"));
    renderWorkspace("/workspaces");
    expect(await screen.findByRole("alert")).toHaveTextContent("workspaces down");
  });

  it("creates a workspace, auto-generating the id from the name, and navigates", async () => {
    const user = userEvent.setup();
    vi.mocked(api.workspaces).mockResolvedValue([]);
    vi.mocked(api.createWorkspace).mockResolvedValue({ id: "acme-farms", name: "Acme Farms", role: "owner" });
    vi.mocked(api.workspace).mockResolvedValue(workspaceFixture({ id: "acme-farms", name: "Acme Farms" }));
    renderWorkspace("/workspaces");
    await screen.findByRole("heading", { name: "New workspace" });
    // The id derives from the name only while it is still empty, so set the
    // whole name in one change event (typing char by char would freeze it
    // after the first character).
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Acme Farms" } });
    expect(screen.getByLabelText(/Id /)).toHaveValue("acme-farms");
    await user.click(screen.getByRole("button", { name: "Create workspace" }));
    await waitFor(() =>
      expect(vi.mocked(api.createWorkspace)).toHaveBeenCalledWith({
        id: "acme-farms",
        name: "Acme Farms",
        plan: "lite",
      }),
    );
    expect(await screen.findByRole("heading", { name: "Acme Farms" })).toBeInTheDocument();
  });

  it("shows a create error", async () => {
    const user = userEvent.setup();
    vi.mocked(api.workspaces).mockResolvedValue([]);
    vi.mocked(api.createWorkspace).mockRejectedValue(new Error("id taken"));
    renderWorkspace("/workspaces");
    await screen.findByRole("heading", { name: "New workspace" });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Acme" } });
    await user.click(screen.getByRole("button", { name: "Create workspace" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("id taken");
  });
});

describe("WorkspacePage people tab", () => {
  function mockWorkspace(fixture: WorkspaceView = workspaceFixture()) {
    vi.mocked(api.workspace).mockResolvedValue(fixture);
  }

  it("renders nothing while loading and an alert on load error", async () => {
    let resolve!: (v: WorkspaceView) => void;
    vi.mocked(api.workspace).mockReturnValue(
      new Promise<WorkspaceView>((r) => {
        resolve = r;
      }),
    );
    const first = renderWorkspace();
    expect(first.container).toBeEmptyDOMElement();
    resolve(workspaceFixture());
    first.unmount();

    vi.mocked(api.workspace).mockRejectedValue(new Error("workspace down"));
    renderWorkspace();
    expect(await screen.findByRole("alert")).toHaveTextContent("workspace down");
  });

  it("shows the role line, members sorted by role, and the passkey notice for Cognito admins", async () => {
    mockWorkspace();
    renderWorkspace("/workspaces/ws1", { ...baseMe, idp: "cognito", strong: false });
    expect(await screen.findByRole("heading", { name: "Acme" })).toBeInTheDocument();
    expect(screen.getByText(/You're an owner here/)).toBeInTheDocument();
    expect(screen.getByText(/Signed in as/)).toBeInTheDocument();
    const rows = screen
      .getAllByRole("row")
      .slice(1)
      .map((r) => r.textContent);
    expect(rows[0]).toContain("Ada");
    expect(rows[1]).toContain("Sam");
    expect(rows[2]).toContain("Jo");
    expect(rows[3]).toContain("Kim");
    expect(screen.getByRole("link", { name: "sign in with a passkey" })).toHaveAttribute(
      "href",
      "/api/auth/login?provider=passkey&next=/workspaces",
    );
    expect(screen.getByRole("link", { name: "Add a passkey" })).toHaveAttribute("href", "/api/auth/passkey");
    // Two members are active.
    expect(screen.getAllByText("active")).toHaveLength(2);
    expect(screen.getByText("invited")).toBeInTheDocument();
    expect(screen.getByText("suspended")).toBeInTheDocument();
  });

  it("shows the passkey confirmation for a strong Cognito sign-in and nothing for GitHub", async () => {
    mockWorkspace();
    const first = renderWorkspace("/workspaces/ws1", { ...baseMe, idp: "cognito", strong: true });
    expect(await screen.findByText("You signed in with a passkey.")).toBeInTheDocument();
    first.unmount();

    mockWorkspace();
    renderWorkspace("/workspaces/ws1", { ...baseMe, idp: "github" });
    await screen.findByRole("heading", { name: "Acme" });
    expect(screen.queryByText(/passkey/)).not.toBeInTheDocument();
  });

  it("hides the Audit tab from non-admins and shows it to admins", async () => {
    mockWorkspace(workspaceFixture({ role: "member" }));
    renderWorkspace("/workspaces/ws1", baseMe);
    await screen.findByRole("heading", { name: "Acme" });
    expect(screen.getByRole("tab", { name: "People" })).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Audit log" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Add someone" })).not.toBeInTheDocument();
  });

  it("changes a member's role", async () => {
    const user = userEvent.setup();
    mockWorkspace();
    vi.mocked(api.updateMember).mockResolvedValue(member({ role: "admin" }));
    renderWorkspace();
    await screen.findByRole("heading", { name: "Acme" });
    await user.selectOptions(screen.getByLabelText("Sam's role"), "admin");
    await waitFor(() =>
      expect(vi.mocked(api.updateMember)).toHaveBeenCalledWith("ws1", "m-sam", { role: "admin" }),
    );
  });

  it("shows the owner role as plain text to a non-owner admin", async () => {
    mockWorkspace(workspaceFixture({ role: "admin" }));
    renderWorkspace();
    await screen.findByRole("heading", { name: "Acme" });
    expect(screen.queryByLabelText("Ada's role")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Sam's role")).toBeInTheDocument();
  });

  it("invites someone and shows the invite URL with the emailed flag", async () => {
    const user = userEvent.setup();
    mockWorkspace();
    vi.mocked(api.invite).mockResolvedValue({
      ...member({
        id: "m-new",
        name: "New Person",
        email: "new@example.com",
        github_login: "",
        status: "invited",
      }),
      invite_url: "https://example.com/join/ws1/token-1",
      emailed: true,
    });
    renderWorkspace();
    await screen.findByRole("heading", { name: "Acme" });
    const invite = screen.getByRole("button", { name: "Invite" });
    expect(invite).toBeDisabled();
    await user.type(screen.getByLabelText("Name"), "New Person");
    await user.type(screen.getByLabelText("Email"), "new@example.com");
    await user.click(invite);
    await waitFor(() =>
      expect(vi.mocked(api.invite)).toHaveBeenCalledWith("ws1", {
        name: "New Person",
        email: "new@example.com",
        github_login: "",
        role: "member",
        products: [],
      }),
    );
    expect(await screen.findByText(/We emailed New Person an invitation\./)).toBeInTheDocument();
    expect(screen.getByText("https://example.com/join/ws1/token-1")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy link" })).toBeInTheDocument();
  });

  it("shows the one-time link wording when the invite was not emailed", async () => {
    const user = userEvent.setup();
    mockWorkspace();
    vi.mocked(api.invite).mockResolvedValue({
      ...member({ id: "m-new", name: "New Person", email: "", github_login: "newbie", status: "invited" }),
      invite_url: "https://example.com/join/ws1/token-2",
      emailed: false,
    });
    renderWorkspace();
    await screen.findByRole("heading", { name: "Acme" });
    await user.type(screen.getByLabelText("GitHub login"), "newbie");
    await user.click(screen.getByRole("button", { name: "Invite" }));
    expect(
      await screen.findByText(/Send New Person this link; it works once, for them alone\./),
    ).toBeInTheDocument();
  });

  it("resends an invitation link for an invited member", async () => {
    const user = userEvent.setup();
    mockWorkspace();
    vi.mocked(api.resendInvite).mockResolvedValue({
      invite_url: "https://example.com/join/ws1/token-3",
      emailed: true,
    });
    renderWorkspace();
    await screen.findByRole("heading", { name: "Acme" });
    await user.click(screen.getByRole("button", { name: "New link" }));
    await waitFor(() => expect(vi.mocked(api.resendInvite)).toHaveBeenCalledWith("ws1", "m-jo"));
    expect(await screen.findByText(/We emailed Jo an invitation\./)).toBeInTheDocument();
  });

  it("suspends and reactivates a member", async () => {
    const user = userEvent.setup();
    mockWorkspace();
    vi.mocked(api.updateMember).mockResolvedValue(member({ status: "suspended" }));
    renderWorkspace();
    await screen.findByRole("heading", { name: "Acme" });
    await user.click(screen.getByRole("button", { name: "Suspend" }));
    await waitFor(() =>
      expect(vi.mocked(api.updateMember)).toHaveBeenCalledWith("ws1", "m-sam", { status: "suspended" }),
    );
    expect(vi.mocked(api.updateMember).mock.calls.length).toBe(1);
  });

  it("removes a member only after confirmation", async () => {
    const user = userEvent.setup();
    mockWorkspace();
    vi.mocked(api.removeMember).mockResolvedValue(undefined);
    renderWorkspace();
    await screen.findByRole("heading", { name: "Acme" });
    const samRow = screen.getByText("Sam").closest("tr")!;
    await user.click(within(samRow).getByRole("button", { name: "Remove" }));
    expect(window.confirm).toHaveBeenCalledWith(
      "Remove Sam from Acme? They lose access on their next click.",
    );
    await waitFor(() => expect(vi.mocked(api.removeMember)).toHaveBeenCalledWith("ws1", "m-sam"));
  });

  it("does not remove when the confirmation is cancelled, and never offers to remove yourself", async () => {
    const user = userEvent.setup();
    mockWorkspace();
    vi.mocked(window.confirm).mockReturnValue(false);
    renderWorkspace();
    await screen.findByRole("heading", { name: "Acme" });
    const samRow = screen.getByText("Sam").closest("tr")!;
    await user.click(within(samRow).getByRole("button", { name: "Remove" }));
    expect(vi.mocked(api.removeMember)).not.toHaveBeenCalled();
    // Ada is the signed-in user: no Suspend/Remove buttons on her own row.
    const adaRow = screen.getByText("Ada").closest("tr")!;
    expect(within(adaRow).queryByRole("button", { name: "Suspend" })).not.toBeInTheDocument();
    expect(within(adaRow).queryByRole("button", { name: "Remove" })).not.toBeInTheDocument();
  });

  it("shows an invite error from the run handler", async () => {
    const user = userEvent.setup();
    mockWorkspace();
    vi.mocked(api.invite).mockRejectedValue(new Error("already a member"));
    renderWorkspace();
    await screen.findByRole("heading", { name: "Acme" });
    await user.type(screen.getByLabelText("Email"), "sam@example.com");
    await user.click(screen.getByRole("button", { name: "Invite" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("already a member");
  });
});

describe("WorkspacePage groups tab", () => {
  function mockWorkspace(fixture: WorkspaceView = workspaceFixture()) {
    vi.mocked(api.workspace).mockResolvedValue(fixture);
  }

  function openGroups() {
    renderWorkspace("/workspaces/ws1?tab=groups");
    return screen.findByRole("heading", { name: /Approvers/ });
  }

  it("toggles group membership with checkboxes", async () => {
    const user = userEvent.setup();
    mockWorkspace();
    vi.mocked(api.setGroup).mockResolvedValue(group({ members: ["m-ada", "m-sam"] }));
    await openGroups();
    const approvers = screen.getByRole("heading", { name: /Approvers/ }).closest("section")!;
    const sam = within(approvers).getByLabelText("Sam");
    expect(sam).not.toBeChecked();
    await user.click(sam);
    await waitFor(() =>
      expect(vi.mocked(api.setGroup)).toHaveBeenCalledWith("ws1", "approvers", ["m-ada", "m-sam"]),
    );
  });

  it("creates a group and clears the input", async () => {
    const user = userEvent.setup();
    mockWorkspace();
    vi.mocked(api.createGroup).mockResolvedValue(group({ slug: "qa2", name: "QA2", members: [] }));
    await openGroups();
    const input = screen.getByLabelText("New group");
    await user.type(input, "QA2");
    await user.click(screen.getByRole("button", { name: "Add group" }));
    await waitFor(() => expect(vi.mocked(api.createGroup)).toHaveBeenCalledWith("ws1", "QA2"));
    expect(input).toHaveValue("");
  });

  it("deletes an unused group after confirmation and refuses a used one", async () => {
    const user = userEvent.setup();
    mockWorkspace();
    vi.mocked(api.deleteGroup).mockResolvedValue(undefined);
    await openGroups();
    expect(screen.getByText(/Approves gates, so it can't be deleted/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Delete" }));
    expect(window.confirm).toHaveBeenCalledWith("Delete the QA group?");
    await waitFor(() => expect(vi.mocked(api.deleteGroup)).toHaveBeenCalledWith("ws1", "qa"));
  });

  it("shows member names as text to non-admins", async () => {
    mockWorkspace(workspaceFixture({ role: "member" }));
    await openGroups();
    expect(screen.getByText("Ada")).toBeInTheDocument();
    expect(screen.queryByLabelText("Sam")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add group" })).not.toBeInTheDocument();
  });
});

describe("WorkspacePage gates tab", () => {
  function mockWorkspace(fixture: WorkspaceView = workspaceFixture()) {
    vi.mocked(api.workspace).mockResolvedValue(fixture);
  }

  it("renders the gate matrix and saves only after a change", async () => {
    const user = userEvent.setup();
    mockWorkspace();
    vi.mocked(api.setGates).mockResolvedValue({ version: 3 });
    renderWorkspace("/workspaces/ws1?tab=gates");
    await screen.findAllByText("Initiative approval");
    // Two products each render a matrix; scope to the cln one.
    const cln = screen.getByRole("heading", { name: /cln/ }).closest("section")!;
    const save = within(cln).getByRole("button", { name: "Save as a new version" });
    expect(save).toBeDisabled();
    expect(within(cln).getByLabelText("Approvers approves Idea approval")).toBeChecked();
    expect(within(cln).getByLabelText("QA approves Idea approval")).not.toBeChecked();
    await user.click(within(cln).getByLabelText("QA approves Idea approval"));
    expect(save).not.toBeDisabled();
    await user.click(save);
    await waitFor(() =>
      expect(vi.mocked(api.setGates)).toHaveBeenCalledWith("ws1", "cln", {
        g1: ["approvers", "qa"],
        g2: ["approvers"],
      }),
    );
  });

  it("shows ticks to non-admins and no save button", async () => {
    mockWorkspace(workspaceFixture({ role: "member" }));
    renderWorkspace("/workspaces/ws1?tab=gates");
    await screen.findAllByText("Initiative approval");
    expect(screen.getAllByText("✓")).toHaveLength(2);
    expect(screen.queryByRole("button", { name: "Save as a new version" })).not.toBeInTheDocument();
  });

  it("shows the no-products message", async () => {
    mockWorkspace(workspaceFixture({ products: [] }));
    renderWorkspace("/workspaces/ws1?tab=gates");
    expect(await screen.findByText("No products in this workspace yet.")).toBeInTheDocument();
  });
});

describe("WorkspacePage connections tab", () => {
  function mockWorkspace(fixture: WorkspaceView = workspaceFixture()) {
    vi.mocked(api.workspace).mockResolvedValue(fixture);
    vi.mocked(api.connections).mockResolvedValue([toolRow()]);
  }

  it("renders connection rows with status and navigates to install on Connect", async () => {
    const user = userEvent.setup();
    mockWorkspace();
    vi.mocked(api.connections).mockResolvedValue([
      toolRow({ status: "not_connected", connections: [] }),
      toolRow({
        tool: "jira",
        name: "Jira",
        status: "needs_attention",
        connections: [
          {
            tool: "jira",
            account: "acme",
            status: "needs_attention",
            connected_by: "ada",
            connected_at: 1,
            last_ok: 1,
            last_error: "token expired",
            reach: { sites: [{ name: "Acme" }] },
          },
        ],
      }),
      toolRow({ tool: "lucid", name: "Lucid", status: "not_connected", available: false, connections: [] }),
    ]);
    renderWorkspace("/workspaces/ws1?tab=connections");
    await screen.findByText("GitHub");
    expect(screen.getByRole("button", { name: "Connect" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Connect" }));
    expect(window.location.href).toBe("/api/workspaces/ws1/connections/github/install");
    expect(screen.getByText(/Reconnect to fix it\./)).toBeInTheDocument();
    expect(screen.getByText(/It isn't set up on this Valxel yet/)).toBeInTheDocument();
  });

  it("shows the connected notice from ?connected= and the error from ?error=", async () => {
    mockWorkspace();
    renderWorkspace("/workspaces/ws1?tab=connections&connected=github");
    expect(await screen.findByText("Connected github.")).toBeInTheDocument();

    mockWorkspace();
    const second = renderWorkspace("/workspaces/ws1?tab=connections&error=oauth+failed");
    expect(await screen.findByRole("alert")).toHaveTextContent("oauth failed");
    second.unmount();
  });

  it("disconnects from the tab and shows the revoke text", async () => {
    const user = userEvent.setup();
    mockWorkspace();
    vi.mocked(api.disconnect).mockResolvedValue({ revoke: "revoke on GitHub" });
    renderWorkspace("/workspaces/ws1?tab=connections");
    await screen.findByText("GitHub");
    await user.click(screen.getByRole("button", { name: "Disconnect" }));
    await waitFor(() => expect(vi.mocked(api.disconnect)).toHaveBeenCalledWith("ws1", "github", "acme"));
    expect(
      await screen.findByText(
        /Disconnected\. To remove Valxel's access on GitHub's side too: revoke on GitHub\./,
      ),
    ).toBeInTheDocument();
  });

  it("shows the AWS two-step form in the tab", async () => {
    const user = userEvent.setup();
    mockWorkspace();
    vi.mocked(api.connections).mockResolvedValue([
      toolRow({ tool: "aws", name: "AWS", status: "not_connected", connections: [] }),
    ]);
    vi.mocked(api.awsLaunch).mockResolvedValue({ url: "https://console.aws.example/stack", mode: "lite" });
    vi.mocked(api.awsCheck).mockResolvedValue({
      tool: "aws",
      account: "123456789012",
      status: "connected",
      connected_by: "ada",
      connected_at: 1,
      last_ok: 1,
      last_error: "",
      reach: {},
    } as never);
    renderWorkspace("/workspaces/ws1?tab=connections");
    await screen.findByText("AWS");
    await user.click(screen.getByRole("button", { name: "Open AWS to create the stack" }));
    await waitFor(() => expect(vi.mocked(api.awsLaunch)).toHaveBeenCalledWith("ws1", "lite", "us-east-1"));
    expect(window.open).toHaveBeenCalledWith("https://console.aws.example/stack", "_blank", "noopener");
    const check = screen.getByRole("button", { name: "Check the connection" });
    expect(check).toBeDisabled();
    await user.type(screen.getByLabelText("Your AWS account ID"), "123456789012");
    await user.click(check);
    await waitFor(() => expect(vi.mocked(api.awsCheck)).toHaveBeenCalledWith("ws1", "123456789012", "lite"));
    expect(await screen.findByText("Connected AWS account 123456789012.")).toBeInTheDocument();
  });
});

describe("WorkspacePage audit tab", () => {
  it("renders audit entries with action labels and details", async () => {
    vi.mocked(api.workspace).mockResolvedValue(workspaceFixture());
    vi.mocked(api.workspaceAudit).mockResolvedValue([
      {
        at: "2026-11-18T09:00:00Z",
        actor: "sam",
        action: "member.add",
        target: "ada",
        detail: { role: "approver" },
      },
      { at: "2026-11-18T10:00:00Z", actor: "ada", action: "mystery.action", detail: {} },
      {
        at: "2026-11-18T11:00:00Z",
        actor: "ada",
        action: "gates.set",
        detail: { product: "cln", version: 3 },
      },
    ]);
    renderWorkspace("/workspaces/ws1?tab=audit");
    await screen.findByRole("heading", { name: "Audit log" });
    expect(screen.getByText("sam")).toBeInTheDocument();
    expect(screen.getByText(/added someone/)).toBeInTheDocument();
    expect(screen.getByText(/role: approver/)).toBeInTheDocument();
    expect(screen.getByText(/mystery\.action/)).toBeInTheDocument();
    expect(screen.getByText(/changed gate approvers/)).toBeInTheDocument();
    expect(screen.getByText("2026-11-18 09:00")).toBeInTheDocument();
  });

  it("shows an audit load error", async () => {
    vi.mocked(api.workspace).mockResolvedValue(workspaceFixture());
    vi.mocked(api.workspaceAudit).mockRejectedValue(new Error("audit down"));
    renderWorkspace("/workspaces/ws1?tab=audit");
    expect(await screen.findByRole("alert")).toHaveTextContent("audit down");
  });
});

describe("JoinPage", () => {
  it("accepts the invitation, calls onJoined and navigates", async () => {
    const user = userEvent.setup();
    const onJoined = vi.fn();
    vi.mocked(api.join).mockResolvedValue(member());
    vi.mocked(api.workspace).mockResolvedValue(workspaceFixture());
    renderWorkspace("/join/ws1/token-abc", baseMe, onJoined);
    expect(await screen.findByRole("heading", { name: "Join the ws1 workspace" })).toBeInTheDocument();
    expect(screen.getByText(/The link works once/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Accept the invitation" }));
    await waitFor(() => expect(vi.mocked(api.join)).toHaveBeenCalledWith("ws1", "token-abc"));
    expect(onJoined).toHaveBeenCalled();
    expect(await screen.findByRole("heading", { name: "Acme" })).toBeInTheDocument();
  });

  it("shows a join error and lets the user retry", async () => {
    const user = userEvent.setup();
    vi.mocked(api.join).mockRejectedValue(new Error("link already used"));
    renderWorkspace("/join/ws1/token-abc");
    await screen.findByRole("heading", { name: "Join the ws1 workspace" });
    await user.click(screen.getByRole("button", { name: "Accept the invitation" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("link already used");
    expect(screen.getByRole("button", { name: "Accept the invitation" })).not.toBeDisabled();
  });

  it("disables the button while the join is in flight", async () => {
    const user = userEvent.setup();
    let resolve!: (v: Member) => void;
    vi.mocked(api.join).mockReturnValue(
      new Promise<Member>((r) => {
        resolve = r;
      }),
    );
    vi.mocked(api.workspace).mockResolvedValue(workspaceFixture());
    renderWorkspace("/join/ws1/token-abc");
    await screen.findByRole("heading", { name: "Join the ws1 workspace" });
    const button = screen.getByRole("button", { name: "Accept the invitation" });
    await user.click(button);
    expect(button).toBeDisabled();
    resolve(member());
    expect(await screen.findByRole("heading", { name: "Acme" })).toBeInTheDocument();
  });
});

describe("WorkspacePage extra branches", () => {
  function mockWorkspace(fixture: WorkspaceView = workspaceFixture()) {
    vi.mocked(api.workspace).mockResolvedValue(fixture);
  }

  it("switches tabs by clicking them", async () => {
    const user = userEvent.setup();
    mockWorkspace();
    vi.mocked(api.workspaceAudit).mockResolvedValue([]);
    vi.mocked(api.connections).mockResolvedValue([toolRow()]);
    renderWorkspace("/workspaces/ws1");
    await screen.findByRole("heading", { name: "Acme" });
    await user.click(screen.getByRole("tab", { name: "Approver groups" }));
    expect(await screen.findByRole("heading", { name: /Approvers/ })).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Gate approvers" }));
    expect(await screen.findAllByText("Initiative approval")).toBeTruthy();
    await user.click(screen.getByRole("tab", { name: "Connections" }));
    expect(await screen.findByText("GitHub")).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Audit log" }));
    expect(await screen.findByRole("heading", { name: "Audit log" })).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "People" }));
    expect(await screen.findByRole("heading", { name: /People \(4\)/ })).toBeInTheDocument();
  });

  it("scopes an invite to ticked products and copies the invite link", async () => {
    const user = userEvent.setup();
    mockWorkspace();
    vi.mocked(api.invite).mockResolvedValue({
      ...member({
        id: "m-new",
        name: "New Person",
        email: "new@example.com",
        github_login: "",
        status: "invited",
      }),
      invite_url: "https://example.com/join/ws1/token-1",
      emailed: true,
    });
    renderWorkspace();
    await screen.findByRole("heading", { name: "Acme" });
    await user.type(screen.getByLabelText("Name"), "New Person");
    await user.type(screen.getByLabelText("Email"), "new@example.com");
    await user.click(screen.getByLabelText("cln"));
    await user.selectOptions(screen.getByLabelText("Role"), "approver");
    await user.click(screen.getByRole("button", { name: "Invite" }));
    await waitFor(() =>
      expect(vi.mocked(api.invite)).toHaveBeenCalledWith(
        "ws1",
        expect.objectContaining({
          role: "approver",
          products: ["cln"],
        }),
      ),
    );
    expect(await screen.findByText("https://example.com/join/ws1/token-1")).toBeInTheDocument();
    // Stub the clipboard after userEvent.setup() (which replaces it with its
    // own stub), then click with fireEvent so the stub survives.
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    fireEvent.click(screen.getByRole("button", { name: "Copy link" }));
    expect(writeText).toHaveBeenCalledWith("https://example.com/join/ws1/token-1");
  });

  it("hides the product scoping when the workspace has one product", async () => {
    mockWorkspace(workspaceFixture({ products: [{ id: "cln", gates: {}, version: 0 }] }));
    renderWorkspace();
    await screen.findByRole("heading", { name: "Acme" });
    // The members table still has a "Products" column; only the invite
    // form's product scoping fieldset is gone.
    expect(screen.queryByRole("group", { name: /Products/ })).not.toBeInTheDocument();
  });

  it("removes a member from a group by unchecking", async () => {
    const user = userEvent.setup();
    mockWorkspace();
    vi.mocked(api.setGroup).mockResolvedValue(group({ members: [] }));
    renderWorkspace("/workspaces/ws1?tab=groups");
    await screen.findByRole("heading", { name: /Approvers/ });
    const approvers = screen.getByRole("heading", { name: /Approvers/ }).closest("section")!;
    const ada = within(approvers).getByLabelText("Ada");
    expect(ada).toBeChecked();
    await user.click(ada);
    await waitFor(() => expect(vi.mocked(api.setGroup)).toHaveBeenCalledWith("ws1", "approvers", []));
  });

  it("does not delete a group when the confirmation is cancelled", async () => {
    const user = userEvent.setup();
    mockWorkspace();
    vi.mocked(window.confirm).mockReturnValue(false);
    renderWorkspace("/workspaces/ws1?tab=groups");
    await screen.findByRole("heading", { name: /Approvers/ });
    await user.click(screen.getByRole("button", { name: "Delete" }));
    expect(vi.mocked(api.deleteGroup)).not.toHaveBeenCalled();
  });

  it("unchecks a gate approver and saves without the emptied gate", async () => {
    const user = userEvent.setup();
    mockWorkspace();
    vi.mocked(api.setGates).mockResolvedValue({ version: 3 });
    renderWorkspace("/workspaces/ws1?tab=gates");
    await screen.findAllByText("Initiative approval");
    const cln = screen.getByRole("heading", { name: /cln/ }).closest("section")!;
    await user.click(within(cln).getByLabelText("Approvers approves Idea approval"));
    await user.click(within(cln).getByRole("button", { name: "Save as a new version" }));
    await waitFor(() =>
      expect(vi.mocked(api.setGates)).toHaveBeenCalledWith("ws1", "cln", {
        g2: ["approvers"],
      }),
    );
  });

  it("renders every reach variant in the connections tab", async () => {
    mockWorkspace();
    vi.mocked(api.connections).mockResolvedValue([
      toolRow({
        tool: "github",
        name: "GitHub",
        connections: [
          {
            tool: "github",
            account: "acme",
            status: "connected",
            connected_by: "ada",
            connected_at: 0,
            last_ok: 0,
            last_error: "",
            reach: { selection: "selected", repos: ["repo-a", "repo-b"] },
          },
        ],
      }),
      toolRow({
        tool: "aws",
        name: "AWS",
        status: "connected",
        connections: [
          {
            tool: "aws",
            account: "123456789012",
            status: "connected",
            connected_by: "ada",
            connected_at: 1700000000,
            last_ok: 1700000000,
            last_error: "",
            reach: { mode: "full" },
          },
        ],
      }),
      toolRow({
        tool: "sentry",
        name: "Sentry",
        status: "connected",
        connections: [
          {
            tool: "sentry",
            account: "acme",
            status: "connected",
            connected_by: "ada",
            connected_at: 1700000000,
            last_ok: 1700000000,
            last_error: "",
            reach: { org: "acme-org" },
          },
        ],
      }),
      toolRow({
        tool: "pagerduty",
        name: "PagerDuty",
        status: "connected",
        connections: [
          {
            tool: "pagerduty",
            account: "acme",
            status: "connected",
            connected_by: "ada",
            connected_at: 1700000000,
            last_ok: 1700000000,
            last_error: "",
            reach: { subdomain: "acme", user: "bob" },
          },
        ],
      }),
      toolRow({
        tool: "jira",
        name: "Jira",
        status: "connected",
        connections: [
          {
            tool: "jira",
            account: "acme",
            status: "connected",
            connected_by: "ada",
            connected_at: 1700000000,
            last_ok: 1700000000,
            last_error: "",
            reach: { user: "bob" },
          },
        ],
      }),
    ]);
    renderWorkspace("/workspaces/ws1?tab=connections");
    expect(await screen.findByText("2 repositories: repo-a, repo-b")).toBeInTheDocument();
    expect(screen.getByText("full mode: a deploy role and a read-only role for agents")).toBeInTheDocument();
    expect(screen.getByText("Sentry organization acme-org")).toBeInTheDocument();
    expect(screen.getByText("PagerDuty account acme")).toBeInTheDocument();
    expect(screen.getByText("as bob")).toBeInTheDocument();
    // The GitHub connection has no timestamps: "Connected by ada on ; last worked ."
    expect(screen.getByText(/Connected by ada on ; last worked \./)).toBeInTheDocument();
  });

  it("shows a connections load error in the tab", async () => {
    mockWorkspace();
    vi.mocked(api.connections).mockRejectedValue(new Error("connections down"));
    renderWorkspace("/workspaces/ws1?tab=connections");
    expect(await screen.findByRole("alert")).toHaveTextContent("connections down");
  });

  it("shows an AWS check error and passes mode/region in the tab", async () => {
    const user = userEvent.setup();
    mockWorkspace();
    vi.mocked(api.connections).mockResolvedValue([
      toolRow({ tool: "aws", name: "AWS", status: "not_connected", connections: [] }),
    ]);
    vi.mocked(api.awsLaunch).mockResolvedValue({ url: "https://console.aws.example/stack", mode: "full" });
    vi.mocked(api.awsCheck).mockRejectedValue(new Error("roles not found"));
    renderWorkspace("/workspaces/ws1?tab=connections");
    await screen.findByText("AWS");
    await user.selectOptions(screen.getByLabelText("Mode"), "full");
    await user.selectOptions(screen.getByLabelText("Region"), "eu-west-1");
    await user.click(screen.getByRole("button", { name: "Open AWS to create the stack" }));
    await waitFor(() => expect(vi.mocked(api.awsLaunch)).toHaveBeenCalledWith("ws1", "full", "eu-west-1"));
    await user.type(screen.getByLabelText("Your AWS account ID"), "123456789012");
    await user.click(screen.getByRole("button", { name: "Check the connection" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("roles not found");
  });

  it("renders audit details with arrays and objects", async () => {
    vi.mocked(api.workspace).mockResolvedValue(workspaceFixture());
    vi.mocked(api.workspaceAudit).mockResolvedValue([
      {
        at: "2026-11-18T09:00:00Z",
        actor: "ada",
        action: "gates.set",
        detail: { tags: ["a", "b"], nested: { v: 1 } },
      },
    ]);
    renderWorkspace("/workspaces/ws1?tab=audit");
    await screen.findByRole("heading", { name: "Audit log" });
    expect(screen.getByText(/tags: a, b/)).toBeInTheDocument();
    expect(screen.getByText(/nested: \{"v":1\}/)).toBeInTheDocument();
  });

  it("shows the passkey notice to a Cognito approver who is not an admin", async () => {
    mockWorkspace(workspaceFixture({ role: "approver" }));
    renderWorkspace("/workspaces/ws1", { ...baseMe, idp: "cognito", strong: false });
    expect(await screen.findByRole("link", { name: "sign in with a passkey" })).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Audit log" })).not.toBeInTheDocument();
  });

  it("creates a workspace with the chosen plan", async () => {
    const user = userEvent.setup();
    vi.mocked(api.workspaces).mockResolvedValue([]);
    vi.mocked(api.createWorkspace).mockResolvedValue({ id: "acme", name: "Acme", role: "owner" });
    vi.mocked(api.workspace).mockResolvedValue(workspaceFixture());
    renderWorkspace("/workspaces");
    await screen.findByRole("heading", { name: "New workspace" });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Acme" } });
    await user.selectOptions(screen.getByLabelText("Plan"), "full");
    await user.click(screen.getByRole("button", { name: "Create workspace" }));
    await waitFor(() =>
      expect(vi.mocked(api.createWorkspace)).toHaveBeenCalledWith({
        id: "acme",
        name: "Acme",
        plan: "full",
      }),
    );
  });
});
