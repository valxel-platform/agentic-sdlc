import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { api, type Me, type ToolRow } from "../api";
import { ConnectedAppsPage } from "./ConnectedApps";

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

function tool(overrides: Record<string, unknown> = {}): ToolRow {
  return {
    tool: "github",
    name: "GitHub",
    asked_when: "2026-01-01",
    grants: "Read and write repos",
    revoke: "revoke on GitHub under Settings",
    available: true,
    status: "not_connected",
    connections: [],
    ...overrides,
  } as unknown as ToolRow;
}

function connection(overrides: Record<string, unknown> = {}) {
  return {
    tool: "github",
    account: "acme",
    status: "connected",
    connected_by: "ada",
    connected_at: 1700000000,
    last_ok: 1763000000,
    last_error: "",
    reach: { account_type: "org", selection: "all" },
    ...overrides,
  };
}

const ALL_TOOLS: ToolRow[] = [
  tool(),
  tool({
    tool: "github-provisioning",
    name: "GitHub repo creation",
    status: "connected",
    connections: [connection()],
  }),
  tool({ tool: "aws", name: "AWS", status: "not_connected" }),
  tool({
    tool: "jira",
    name: "Jira",
    status: "needs_attention",
    connections: [
      connection({
        tool: "jira",
        account: "acme.atlassian.net",
        status: "needs_attention",
        last_error: "token expired",
        reach: { sites: [{ name: "Acme" }] },
      }),
    ],
  }),
  tool({ tool: "lucid", name: "Lucid", status: "not_connected" }),
  tool({ tool: "sentry", name: "Sentry", status: "not_connected" }),
  tool({ tool: "pagerduty", name: "PagerDuty", status: "not_connected" }),
];

const adminMe: Me = {
  login: "ada",
  name: "Ada",
  avatar_url: "",
  orgs: [],
  teams: [],
  lanes: [{ id: "cln", name: "Clinic" }],
  roles: { ws1: "admin" },
};
const memberMe: Me = { ...adminMe, roles: { ws1: "member" } };

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
  Object.defineProperty(window, "location", {
    value: originalLocation,
    writable: true,
    configurable: true,
  });
}

function renderApps(me: Me = adminMe, rows: ToolRow[] = ALL_TOOLS) {
  vi.mocked(api.connections).mockResolvedValue(rows);
  return render(
    <MemoryRouter initialEntries={["/connected-apps"]}>
      <ConnectedAppsPage me={me} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  defaultRejectAll();
  stubLocation();
  vi.spyOn(window, "open").mockReturnValue(null);
  vi.spyOn(window, "confirm").mockReturnValue(true);
});

afterEach(() => {
  cleanup();
  restoreLocation();
  vi.restoreAllMocks();
});

describe("ConnectedAppsPage", () => {
  it("shows the no-workspace empty state when the user has no roles", () => {
    render(
      <MemoryRouter initialEntries={["/connected-apps"]}>
        <ConnectedAppsPage me={{ ...adminMe, roles: {} }} />
      </MemoryRouter>,
    );
    expect(screen.getByRole("heading", { name: "No workspace yet" })).toBeInTheDocument();
    expect(screen.getByText(/Set up a workspace first/)).toBeInTheDocument();
    expect(vi.mocked(api.connections)).not.toHaveBeenCalled();
  });

  it("shows Loading… then the count and every real tool, and no Slack/Figma/Vercel", async () => {
    renderApps();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(await screen.findByText("1 of 7 apps connected")).toBeInTheDocument();
    for (const name of [
      "GitHub Not connected",
      "GitHub repo creation Connected",
      "AWS Not connected",
      "Jira Needs attention",
      "Lucid Not connected",
      "Sentry Not connected",
      "PagerDuty Not connected",
    ]) {
      expect(screen.getByRole("heading", { name })).toBeInTheDocument();
    }
    expect(document.body.textContent).not.toMatch(/Slack|Figma|Vercel/);
  });

  it("shows an error alert when connections fail", async () => {
    vi.mocked(api.connections).mockRejectedValue(new Error("connections down"));
    render(
      <MemoryRouter initialEntries={["/connected-apps"]}>
        <ConnectedAppsPage me={adminMe} />
      </MemoryRouter>,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("connections down");
  });

  it("shows status chips, grants and the needs-attention error", async () => {
    renderApps();
    await screen.findByText("1 of 7 apps connected");
    expect(screen.getByText("Connected")).toBeInTheDocument();
    expect(screen.getByText("Needs attention")).toBeInTheDocument();
    expect(screen.getByText("token expired")).toBeInTheDocument();
    expect(screen.getByText("acme.atlassian.net")).toBeInTheDocument();
    expect(screen.getByText("acme")).toBeInTheDocument();
    expect(screen.getAllByText(/connected by ada/)).toHaveLength(2);
  });

  it("shows the isn't-set-up note for an unavailable tool", async () => {
    renderApps(adminMe, [tool({ available: false })]);
    expect(await screen.findByText(/It isn’t set up on this Valxel yet/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Connect" })).not.toBeInTheDocument();
  });

  it("tells non-admins that only admins connect or disconnect apps", async () => {
    renderApps(memberMe);
    await screen.findByText("1 of 7 apps connected");
    expect(screen.getAllByText("Only workspace admins connect or disconnect apps.")).toHaveLength(7);
    expect(screen.queryByRole("button", { name: "Connect" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Disconnect" })).not.toBeInTheDocument();
  });

  it("walks the 3-step connect modal: can/can-never, scope, approve on the vendor", async () => {
    const user = userEvent.setup();
    renderApps();
    await screen.findByText("1 of 7 apps connected");
    const card = screen.getByRole("heading", { name: "GitHub Not connected" }).closest("section")!;
    await user.click(within(card).getByRole("button", { name: "Connect" }));
    const dialog = await screen.findByRole("dialog", { name: "Connect GitHub" });
    expect(within(dialog).getByText("What it can do")).toBeInTheDocument();
    expect(within(dialog).getByText("Read and write repos")).toBeInTheDocument();
    expect(within(dialog).getByText("What it can never do")).toBeInTheDocument();
    expect(within(dialog).getByText("See or store your passwords")).toBeInTheDocument();
    expect(within(dialog).getByText("Change who can approve a gate")).toBeInTheDocument();
    expect(within(dialog).getByText("Read anything outside what you choose below")).toBeInTheDocument();
    // Step 2: scope picker
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    expect(within(dialog).getByText("Choose the scope")).toBeInTheDocument();
    await user.click(within(dialog).getByLabelText(/Only what you choose/));
    expect(
      within(dialog).getByText(/You confirm only what you choose on GitHub’s own sign-in page/),
    ).toBeInTheDocument();
    // Back, then forward again with the widest scope
    await user.click(within(dialog).getByRole("button", { name: "Back" }));
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    await user.click(within(dialog).getByLabelText(/Everything it can reach/));
    // Step 3: approve on the vendor's own sign-in
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    expect(within(dialog).getByText(/Approve on GitHub’s own sign-in/)).toBeInTheDocument();
    expect(within(dialog).getByText(/Valxel never sees your password/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Continue to GitHub" }));
    expect(window.location.href).toBe("/api/workspaces/ws1/connections/github/install");
  });

  it("closes the modal with Cancel and with an overlay click", async () => {
    const user = userEvent.setup();
    renderApps();
    await screen.findByText("1 of 7 apps connected");
    const card = screen.getByRole("heading", { name: "GitHub Not connected" }).closest("section")!;
    await user.click(within(card).getByRole("button", { name: "Connect" }));
    const dialog = await screen.findByRole("dialog", { name: "Connect GitHub" });
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.click(within(card).getByRole("button", { name: "Connect" }));
    await screen.findByRole("dialog", { name: "Connect GitHub" });
    fireEvent.click(document.querySelector(".modal-overlay")!);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("shows Manage for a connected tool and notes the existing account", async () => {
    const user = userEvent.setup();
    renderApps();
    await screen.findByText("1 of 7 apps connected");
    await user.click(screen.getByRole("button", { name: "Manage" }));
    const dialog = await screen.findByRole("dialog", { name: "Connect GitHub repo creation" });
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    expect(
      within(dialog).getByText(/Already connected: acme\. This adds another account\./),
    ).toBeInTheDocument();
  });

  it("disconnects with confirmation and shows the vendor revoke text", async () => {
    const user = userEvent.setup();
    vi.mocked(api.disconnect).mockResolvedValue({ revoke: "revoke on GitHub under Settings" });
    renderApps();
    await screen.findByText("1 of 7 apps connected");
    const card = screen.getByRole("heading", { name: /^GitHub repo creation / }).closest("section")!;
    await user.click(within(card).getByRole("button", { name: "Disconnect" }));
    expect(window.confirm).toHaveBeenCalledWith(
      expect.stringContaining("Disconnect GitHub repo creation for acme?"),
    );
    await waitFor(() =>
      expect(vi.mocked(api.disconnect)).toHaveBeenCalledWith("ws1", "github-provisioning", "acme"),
    );
    expect(
      await screen.findByText(
        /Disconnected\. To remove Valxel's access on GitHub repo creation's side too: revoke on GitHub under Settings\./,
      ),
    ).toBeInTheDocument();
  });

  it("does not disconnect when the confirmation is cancelled", async () => {
    const user = userEvent.setup();
    vi.mocked(window.confirm).mockReturnValue(false);
    renderApps();
    await screen.findByText("1 of 7 apps connected");
    const card = screen.getByRole("heading", { name: /^GitHub repo creation / }).closest("section")!;
    await user.click(within(card).getByRole("button", { name: "Disconnect" }));
    expect(vi.mocked(api.disconnect)).not.toHaveBeenCalled();
  });

  it("shows a disconnect error", async () => {
    const user = userEvent.setup();
    vi.mocked(api.disconnect).mockRejectedValue(new Error("revoke failed"));
    renderApps();
    await screen.findByText("1 of 7 apps connected");
    const card = screen.getByRole("heading", { name: /^GitHub repo creation / }).closest("section")!;
    await user.click(within(card).getByRole("button", { name: "Disconnect" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("revoke failed");
  });

  it("switches workspace and refetches when there are several", async () => {
    const user = userEvent.setup();
    renderApps({ ...adminMe, roles: { ws1: "admin", ws2: "owner" } });
    await screen.findByText("1 of 7 apps connected");
    expect(vi.mocked(api.connections)).toHaveBeenCalledWith("ws1");
    await user.selectOptions(screen.getByLabelText("Workspace"), "ws2");
    await waitFor(() => expect(vi.mocked(api.connections)).toHaveBeenCalledWith("ws2"));
  });

  describe("AWS two-step flow", () => {
    it("launches the CloudFormation stack in a new tab", async () => {
      const user = userEvent.setup();
      vi.mocked(api.awsLaunch).mockResolvedValue({ url: "https://console.aws.example/stack", mode: "lite" });
      renderApps();
      await screen.findByText("1 of 7 apps connected");
      const aws = screen.getByRole("heading", { name: /^AWS / }).closest("section")!;
      await user.click(within(aws).getByRole("button", { name: "Open AWS to create the stack" }));
      await waitFor(() => expect(vi.mocked(api.awsLaunch)).toHaveBeenCalledWith("ws1", "lite", "us-east-1"));
      expect(window.open).toHaveBeenCalledWith("https://console.aws.example/stack", "_blank", "noopener");
    });

    it("passes the chosen mode and region to the launch", async () => {
      const user = userEvent.setup();
      vi.mocked(api.awsLaunch).mockResolvedValue({ url: "https://console.aws.example/stack", mode: "full" });
      renderApps();
      await screen.findByText("1 of 7 apps connected");
      const aws = screen.getByRole("heading", { name: /^AWS / }).closest("section")!;
      await user.selectOptions(within(aws).getByLabelText("Mode"), "full");
      await user.selectOptions(within(aws).getByLabelText("Region"), "eu-west-1");
      await user.click(within(aws).getByRole("button", { name: "Open AWS to create the stack" }));
      await waitFor(() => expect(vi.mocked(api.awsLaunch)).toHaveBeenCalledWith("ws1", "full", "eu-west-1"));
    });

    it("shows a launch error", async () => {
      const user = userEvent.setup();
      vi.mocked(api.awsLaunch).mockRejectedValue(new Error("launch failed"));
      renderApps();
      await screen.findByText("1 of 7 apps connected");
      const aws = screen.getByRole("heading", { name: /^AWS / }).closest("section")!;
      await user.click(within(aws).getByRole("button", { name: "Open AWS to create the stack" }));
      expect(await screen.findByRole("alert")).toHaveTextContent("launch failed");
    });

    it("gates the Check button on a 12-digit account id and checks the connection", async () => {
      const user = userEvent.setup();
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
      renderApps();
      await screen.findByText("1 of 7 apps connected");
      const aws = screen.getByRole("heading", { name: /^AWS / }).closest("section")!;
      const check = within(aws).getByRole("button", { name: "Check the connection" });
      expect(check).toBeDisabled();
      const input = within(aws).getByLabelText("Your AWS account ID");
      await user.type(input, "12345678901");
      expect(check).toBeDisabled();
      await user.type(input, "2");
      expect(check).not.toBeDisabled();
      await user.click(check);
      await waitFor(() =>
        expect(vi.mocked(api.awsCheck)).toHaveBeenCalledWith("ws1", "123456789012", "lite"),
      );
      expect(await screen.findByText("Connected AWS account 123456789012.")).toBeInTheDocument();
    });

    it("shows a check error", async () => {
      const user = userEvent.setup();
      vi.mocked(api.awsCheck).mockRejectedValue(new Error("roles not found"));
      renderApps();
      await screen.findByText("1 of 7 apps connected");
      const aws = screen.getByRole("heading", { name: /^AWS / }).closest("section")!;
      await user.type(within(aws).getByLabelText("Your AWS account ID"), "123456789012");
      await user.click(within(aws).getByRole("button", { name: "Check the connection" }));
      expect(await screen.findByRole("alert")).toHaveTextContent("roles not found");
    });
  });
});
