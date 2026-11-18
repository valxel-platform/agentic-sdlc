import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { cleanup, configure, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { StartPage } from "./Start";
import type { Me } from "../api";

// RTL does not auto-cleanup under vitest with globals: false.
afterEach(cleanup);

// Async waits and the test timeout get room: this VM is shared with other
// agents running the full suite at the same time.
configure({ asyncUtilTimeout: 5000 });
vi.setConfig({ testTimeout: 30000 });

const apiMock = vi.hoisted(() => ({
  plans: vi.fn(),
  createWorkspace: vi.fn(),
  connections: vi.fn(),
  billing: vi.fn(),
  intakeStart: vi.fn(),
}));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock };
});

const me: Me = {
  login: "ada",
  name: "Ada",
  avatar_url: "",
  orgs: ["acme"],
  teams: [],
  lanes: [],
  roles: {},
};

const plansFixture = {
  plans: [
    {
      id: "self-hosted",
      name: "Self-hosted",
      for_whom: "Teams who run their own infra",
      price: "Free",
      card_when: "never",
      sign_in: "GitHub",
      runs_on: "Your infrastructure",
      monthly_usd: null,
      hosted: false,
    },
    {
      id: "lite",
      name: "Lite",
      for_whom: "Small teams getting started",
      price: "$49/mo",
      card_when: "after the trial",
      sign_in: "GitHub",
      runs_on: "Valxel Cloud",
      monthly_usd: 49,
      hosted: true,
    },
    {
      id: "full",
      name: "Full",
      for_whom: "Teams shipping to production",
      price: "$199/mo",
      card_when: "after the trial",
      sign_in: "GitHub",
      runs_on: "Valxel Cloud",
      monthly_usd: 199,
      hosted: true,
    },
  ],
  trial_days: 14,
  install_guide: "https://example.com/install",
};

const billingFixture = {
  plan: plansFixture.plans[1],
  provider: "stripe",
  card_on_file: false,
  trial_ends: "2026-12-01",
  trial_days_left: 14,
  card_needed: false,
  note: "Trial covers hosted dev and staging.",
};

function toolRow(tool: string, status: "connected" | "needs_attention" | "not_connected", available = true) {
  return {
    tool,
    name: tool,
    asked_when: "At setup",
    grants: "repos",
    revoke: "Uninstall the App",
    available,
    status,
    connections: [],
  };
}

function LocationProbe() {
  const { pathname, search } = useLocation();
  return (
    <div data-testid="loc">
      {pathname}
      {search}
    </div>
  );
}

function renderStart(path: string, onChanged = vi.fn()) {
  return {
    ...render(
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/start" element={<StartPage me={me} onChanged={onChanged} />} />
          <Route path="/start/:ws" element={<StartPage me={me} onChanged={onChanged} />} />
          <Route path="/intake/:sid" element={<div>Intake stub</div>} />
        </Routes>
        <LocationProbe />
      </MemoryRouter>,
    ),
    onChanged,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  sessionStorage.clear();
  apiMock.plans.mockResolvedValue(plansFixture);
  apiMock.createWorkspace.mockResolvedValue({ id: "acme", name: "Acme Farms", plan: "lite" });
  apiMock.connections.mockResolvedValue([]);
  apiMock.billing.mockResolvedValue(billingFixture);
  apiMock.intakeStart.mockResolvedValue({ id: "s1" });
});

describe("ChoosePlan", () => {
  it("renders the plan cards from plans()", async () => {
    renderStart("/start");
    expect(await screen.findByRole("radio", { name: /Self-hosted/ })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /Lite/ })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /Full/ })).toBeInTheDocument();
    expect(screen.getByText("$49/mo")).toBeInTheDocument();
    expect(screen.getByText("Plan")).toBeInTheDocument();
  });

  it("shows an alert when plans() fails", async () => {
    apiMock.plans.mockRejectedValue(new Error("Plans are down"));
    renderStart("/start");
    expect(await screen.findByRole("alert")).toHaveTextContent("Plans are down");
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
  });

  it("starts on the Plan step and moves to Workspace when a hosted plan is picked", async () => {
    const user = userEvent.setup();
    renderStart("/start");
    const steps = screen.getByRole("list", { name: "Sign-up steps" });
    expect(within(steps).getByText("Plan").closest("li")).toHaveAttribute("aria-current", "step");
    await user.click(await screen.findByRole("radio", { name: /Lite/ }));
    expect(within(steps).getByText("Workspace").closest("li")).toHaveAttribute("aria-current", "step");
    expect(screen.getByRole("heading", { name: "Name your workspace" })).toBeInTheDocument();
    expect(screen.getByText(/Hosted dev and staging are free for 14 days/)).toBeInTheDocument();
  });

  it("shows the install guide for self-hosted only after the licence check", async () => {
    const user = userEvent.setup();
    renderStart("/start");
    await user.click(await screen.findByRole("radio", { name: /Self-hosted/ }));
    expect(screen.getByRole("heading", { name: "Self-hosted" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Open the install guide" })).not.toBeInTheDocument();
    expect(screen.getByText(/Larger organizations/)).toBeInTheDocument();
    expect(screen.queryByLabelText(/Name/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("checkbox"));
    const guide = screen.getByRole("link", { name: "Open the install guide" });
    expect(guide).toHaveAttribute("href", "https://example.com/install");
    expect(guide).toHaveAttribute("target", "_blank");
  });

  it("derives the workspace id from the name as it is typed", async () => {
    const user = userEvent.setup();
    renderStart("/start");
    await user.click(await screen.findByRole("radio", { name: /Lite/ }));
    await user.type(screen.getByPlaceholderText("Acme Farms"), "Acme Farms!");
    expect(screen.getByLabelText(/Id lowercase/)).toHaveValue("acme-farms");
  });

  it("creates the workspace and navigates to /start/:ws on submit", async () => {
    const user = userEvent.setup();
    const { onChanged } = renderStart("/start");
    await user.click(await screen.findByRole("radio", { name: /Full/ }));
    await user.type(screen.getByPlaceholderText("Acme Farms"), "Acme Farms");
    await user.click(screen.getByRole("button", { name: "Create workspace" }));
    expect(apiMock.createWorkspace).toHaveBeenCalledWith({
      id: "acme-farms",
      name: "Acme Farms",
      plan: "full",
    });
    expect(onChanged).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("/start/acme")).toBeInTheDocument();
  });

  it("does not submit when the id fails the pattern", async () => {
    const user = userEvent.setup();
    renderStart("/start");
    await user.click(await screen.findByRole("radio", { name: /Lite/ }));
    await user.type(screen.getByPlaceholderText("Acme Farms"), "Acme");
    const id = screen.getByLabelText(/Id lowercase/);
    await user.clear(id);
    await user.type(id, "AB");
    expect((id as HTMLInputElement).checkValidity()).toBe(false);
    await user.click(screen.getByRole("button", { name: "Create workspace" }));
    expect(apiMock.createWorkspace).not.toHaveBeenCalled();
  });

  it("shows an alert when createWorkspace fails", async () => {
    const user = userEvent.setup();
    apiMock.createWorkspace.mockRejectedValue(new Error("That id is taken"));
    renderStart("/start");
    await user.click(await screen.findByRole("radio", { name: /Lite/ }));
    await user.type(screen.getByPlaceholderText("Acme Farms"), "Acme");
    await user.click(screen.getByRole("button", { name: "Create workspace" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("That id is taken");
    expect(screen.getByTestId("loc")).toHaveTextContent("/start");
  });
});

describe("Setup", () => {
  it("shows the GitHub connect step when GitHub is not connected", async () => {
    apiMock.connections.mockResolvedValue([
      toolRow("github", "not_connected"),
      toolRow("github-provisioning", "not_connected"),
    ]);
    renderStart("/start/acme");
    expect(await screen.findByRole("heading", { name: "Connect GitHub" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Install Valxel on GitHub" })).toHaveAttribute(
      "href",
      "/api/workspaces/acme/connections/github/install?back=%2Fstart%2Facme",
    );
  });

  it("shows the trial days left from billing", async () => {
    apiMock.connections.mockResolvedValue([toolRow("github", "not_connected")]);
    renderStart("/start/acme");
    expect(await screen.findByText(/hosted trial: 14 days left/)).toBeInTheDocument();
  });

  it("renders even when billing fails", async () => {
    apiMock.connections.mockResolvedValue([toolRow("github", "not_connected")]);
    apiMock.billing.mockRejectedValue(new Error("no billing yet"));
    renderStart("/start/acme");
    expect(await screen.findByRole("heading", { name: "Connect GitHub" })).toBeInTheDocument();
    expect(screen.queryByText(/hosted trial/)).not.toBeInTheDocument();
  });

  it("shows an alert when connections() fails", async () => {
    apiMock.connections.mockRejectedValue(new Error("Cannot list connections"));
    renderStart("/start/acme");
    expect(await screen.findByRole("alert")).toHaveTextContent("Cannot list connections");
  });

  it("explains when GitHub installs are not set up on this Valxel", async () => {
    apiMock.connections.mockResolvedValue([toolRow("github", "not_connected", false)]);
    renderStart("/start/acme");
    expect(await screen.findByText(/GitHub installs aren't set up on this Valxel yet/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Install Valxel on GitHub" })).not.toBeInTheDocument();
  });

  it("offers the guided account creation when the user has no GitHub org yet", async () => {
    const user = userEvent.setup();
    apiMock.connections.mockResolvedValue([toolRow("github", "not_connected")]);
    renderStart("/start/acme");
    await user.click(await screen.findByRole("button", { name: "I don't have a GitHub organization yet" }));
    expect(screen.getByRole("link", { name: "Create a personal GitHub account" })).toHaveAttribute(
      "href",
      "https://github.com/signup",
    );
    expect(screen.getByRole("link", { name: /Create an\s*organization/ })).toHaveAttribute(
      "href",
      "https://github.com/account/organizations/new?plan=free",
    );
    expect(screen.getByRole("link", { name: "I've made it: install Valxel" })).toHaveAttribute(
      "href",
      "/api/workspaces/acme/connections/github/install?back=%2Fstart%2Facme",
    );
  });

  it("shows the repo-creation step once GitHub is connected", async () => {
    apiMock.connections.mockResolvedValue([
      toolRow("github", "connected"),
      toolRow("github-provisioning", "not_connected"),
    ]);
    renderStart("/start/acme");
    expect(await screen.findByRole("heading", { name: "Let Valxel create your repos" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Install the repo-creation App" })).toHaveAttribute(
      "href",
      "/api/workspaces/acme/connections/github-provisioning/install?back=%2Fstart%2Facme",
    );
  });

  it("skipping provisioning remembers the choice and shows the first-product step", async () => {
    const user = userEvent.setup();
    apiMock.connections.mockResolvedValue([
      toolRow("github", "connected"),
      toolRow("github-provisioning", "not_connected"),
    ]);
    renderStart("/start/acme");
    await user.click(await screen.findByRole("button", { name: "Skip: I'll create repos myself" }));
    expect(sessionStorage.getItem("valxel-skip-provisioning-acme")).toBe("1");
    expect(await screen.findByRole("heading", { name: "Your first product" })).toBeInTheDocument();
  });

  it("honours a skip remembered from sessionStorage on load", async () => {
    sessionStorage.setItem("valxel-skip-provisioning-acme", "1");
    apiMock.connections.mockResolvedValue([
      toolRow("github", "connected"),
      toolRow("github-provisioning", "not_connected"),
    ]);
    renderStart("/start/acme");
    expect(await screen.findByRole("heading", { name: "Your first product" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Let Valxel create your repos" })).not.toBeInTheDocument();
  });

  it("still shows the first-product step when remembering the skip throws", async () => {
    const user = userEvent.setup();
    apiMock.connections.mockResolvedValue([
      toolRow("github", "connected"),
      toolRow("github-provisioning", "not_connected"),
    ]);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("storage full");
    });
    renderStart("/start/acme");
    await user.click(await screen.findByRole("button", { name: "Skip: I'll create repos myself" }));
    expect(await screen.findByRole("heading", { name: "Your first product" })).toBeInTheDocument();
    vi.mocked(Storage.prototype.setItem).mockRestore();
  });

  it("still shows the repo-creation step when reading the remembered skip throws", async () => {
    apiMock.connections.mockResolvedValue([
      toolRow("github", "connected"),
      toolRow("github-provisioning", "not_connected"),
    ]);
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("storage unavailable");
    });
    renderStart("/start/acme");
    expect(await screen.findByRole("heading", { name: "Let Valxel create your repos" })).toBeInTheDocument();
    vi.mocked(Storage.prototype.getItem).mockRestore();
  });

  it("starts the first product and navigates to the intake session", async () => {
    const user = userEvent.setup();
    apiMock.connections.mockResolvedValue([
      toolRow("github", "connected"),
      toolRow("github-provisioning", "connected"),
    ]);
    renderStart("/start/acme");
    await user.click(await screen.findByRole("button", { name: "Describe your first product" }));
    expect(apiMock.intakeStart).toHaveBeenCalledWith("acme");
    expect(await screen.findByText("/intake/s1")).toBeInTheDocument();
    expect(screen.getByText("Intake stub")).toBeInTheDocument();
  });

  it("shows an alert when starting the first product fails", async () => {
    const user = userEvent.setup();
    apiMock.connections.mockResolvedValue([
      toolRow("github", "connected"),
      toolRow("github-provisioning", "connected"),
    ]);
    apiMock.intakeStart.mockRejectedValue(new Error("Intake is down"));
    renderStart("/start/acme");
    await user.click(await screen.findByRole("button", { name: "Describe your first product" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Intake is down");
    expect(screen.getByTestId("loc")).toHaveTextContent("/start/acme");
  });

  it("shows the connected notice after returning from GitHub", async () => {
    apiMock.connections.mockResolvedValue([
      toolRow("github", "connected"),
      toolRow("github-provisioning", "not_connected"),
    ]);
    renderStart("/start/acme?connected=github");
    expect(await screen.findByText("Connected github.")).toBeInTheDocument();
  });

  it("shows the error notice after a failed GitHub return", async () => {
    apiMock.connections.mockResolvedValue([toolRow("github", "not_connected")]);
    renderStart("/start/acme?error=install+denied");
    expect(await screen.findByRole("alert")).toHaveTextContent("install denied");
  });

  it("shows the note notice after a GitHub return", async () => {
    apiMock.connections.mockResolvedValue([toolRow("github", "not_connected")]);
    renderStart("/start/acme?note=already+connected");
    expect(await screen.findByText("already connected")).toBeInTheDocument();
  });

  it("shows the billing note on the first-product step", async () => {
    apiMock.connections.mockResolvedValue([
      toolRow("github", "connected"),
      toolRow("github-provisioning", "connected"),
    ]);
    renderStart("/start/acme");
    expect(await screen.findByText("Trial covers hosted dev and staging.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "invite your team" })).toHaveAttribute(
      "href",
      "/workspaces/acme?tab=people",
    );
    expect(screen.getByRole("link", { name: "connect other tools" })).toHaveAttribute(
      "href",
      "/workspaces/acme?tab=connections",
    );
  });

  it("marks the First product step current once GitHub is done", async () => {
    apiMock.connections.mockResolvedValue([
      toolRow("github", "connected"),
      toolRow("github-provisioning", "connected"),
    ]);
    renderStart("/start/acme");
    await screen.findByRole("heading", { name: "Your first product" });
    const steps = screen.getByRole("list", { name: "Sign-up steps" });
    expect(within(steps).getByText("First product").closest("li")).toHaveAttribute("aria-current", "step");
    expect(within(steps).getByText("GitHub").closest("li")).toHaveClass("done");
  });
});
