import { beforeEach, describe, expect, it, vi } from "vitest";
import { configure, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import type { FeatureSummary, Me, PendingItem } from "../api";
import { apiMock, resetApiMock } from "../test/apiMock";
import { Board } from "./Board";

// Multi-step user-event flows and async fetches exceed Vitest's 5s default
// under parallel load on the shared VM; give them room.
vi.setConfig({ testTimeout: 20000 });
configure({ asyncUtilTimeout: 5000 });

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock };
});
vi.mock("../live", () => ({ useLive: () => {}, useLiveStatus: () => true, closeLive: vi.fn() }));

const me: Me = {
  login: "ada",
  name: "Ada",
  avatar_url: "",
  orgs: [],
  teams: [],
  lanes: [
    { id: "CLN", name: "Clinic" },
    { id: "PAY", name: "Payments" },
  ],
};

function pending(over: Partial<PendingItem> = {}): PendingItem {
  return {
    interrupt_id: "int-1",
    type: "gate",
    gate: "g1",
    approvers: ["product-council"],
    can_decide: true,
    feature_id: "CLN-1",
    product_id: "CLN",
    ...over,
  };
}

function feature(over: Partial<FeatureSummary> = {}): FeatureSummary {
  return {
    feature_id: "CLN-1",
    product_id: "CLN",
    title: "Add a CSV export",
    created_by: "ada",
    status: "running",
    running: true,
    rail: [
      { key: "discovery", kind: "phase", state: "done" },
      { key: "spec", kind: "phase", state: "current" },
    ],
    risk_tier: "low",
    cost_usd: 1.2,
    pending: [],
    updated_at: new Date(Date.now() - 2 * 3600 * 1000).toISOString(),
    ...over,
  };
}

function renderBoard() {
  render(
    <MemoryRouter>
      <Board me={me} />
    </MemoryRouter>,
  );
}

function column(label: string): HTMLElement {
  return screen.getByRole("region", { name: label });
}

beforeEach(() => {
  resetApiMock();
  apiMock.products.mockResolvedValue({
    CLN: { name: "Clinic", kind: "product", ok: true, data_class: "phi" },
    PAY: { name: "Payments", kind: "product", ok: true, data_class: "general" },
  });
});

describe("Board loading, error and empty states", () => {
  it("shows the header while loading", () => {
    apiMock.features.mockReturnValue(new Promise(() => {}));
    renderBoard();
    expect(screen.getByRole("heading", { name: "Features" })).toBeInTheDocument();
    expect(screen.queryByText("No features here yet")).not.toBeInTheDocument();
  });

  it("shows an error when features fail to load", async () => {
    apiMock.features.mockRejectedValue(new Error("Board broke"));
    renderBoard();
    expect(await screen.findByRole("alert")).toHaveTextContent("Board broke");
  });

  it("shows the empty state with a link to intake", async () => {
    apiMock.features.mockResolvedValue([]);
    renderBoard();
    expect(await screen.findByText("No features here yet")).toBeInTheDocument();
    const links = screen.getAllByRole("link", { name: "New request" });
    expect(links).toHaveLength(2);
    expect(links[0]).toHaveAttribute("href", "/intake");
  });
});

describe("kanban columns", () => {
  const rows = [
    feature({ feature_id: "CLN-1", title: "Plan feature" }),
    feature({
      feature_id: "CLN-2",
      title: "Build feature",
      rail: [
        { key: "discovery", kind: "phase", state: "done" },
        { key: "spec", kind: "phase", state: "done" },
        { key: "design", kind: "phase", state: "done" },
        { key: "architecture", kind: "phase", state: "done" },
        { key: "build", kind: "phase", state: "current" },
      ],
    }),
    feature({
      feature_id: "CLN-3",
      title: "Check feature",
      rail: [{ key: "g5", kind: "gate", state: "waiting" }],
    }),
    feature({
      feature_id: "CLN-4",
      title: "Live feature",
      rail: [
        { key: "discovery", kind: "phase", state: "done" },
        { key: "spec", kind: "phase", state: "done" },
        { key: "design", kind: "phase", state: "done" },
        { key: "build", kind: "phase", state: "done" },
        { key: "g5", kind: "gate", state: "done" },
        { key: "release", kind: "phase", state: "done" },
        { key: "operate", kind: "phase", state: "done" },
      ],
    }),
  ];

  it("places each feature in the column for its current stage", async () => {
    apiMock.features.mockResolvedValue(rows);
    renderBoard();
    await screen.findByText("Plan feature");
    expect(within(column("Plan & Design")).getByText("Plan feature")).toBeInTheDocument();
    expect(within(column("Build")).getByText("Build feature")).toBeInTheDocument();
    expect(within(column("Check")).getByText("Check feature")).toBeInTheDocument();
    expect(within(column("Live")).getByText("Live feature")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Plan & Design · 1" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Build · 1" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Check · 1" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Live · 1" })).toBeInTheDocument();
  });

  it("hides released and stopped features until show-done is checked", async () => {
    const user = userEvent.setup();
    apiMock.features.mockResolvedValue([
      rows[0],
      feature({ feature_id: "CLN-4", title: "Live feature", status: "done" }),
      feature({ feature_id: "CLN-5", title: "Stopped feature", status: "aborted" }),
    ]);
    renderBoard();
    await screen.findByText("Plan feature");
    expect(screen.queryByText("Live feature")).not.toBeInTheDocument();
    expect(screen.queryByText("Stopped feature")).not.toBeInTheDocument();
    await user.click(screen.getByLabelText("Show released and stopped"));
    expect(screen.getByText("Live feature")).toBeInTheDocument();
    expect(screen.getByText("Stopped feature")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Live · 1" })).toBeInTheDocument();
  });
});

describe("filters", () => {
  const rows = [
    feature({ feature_id: "CLN-1", title: "Clinic feature", pending: [pending({ can_decide: true })] }),
    feature({
      feature_id: "PAY-1",
      product_id: "PAY",
      title: "Payments feature",
      pending: [pending({ can_decide: false, feature_id: "PAY-1" })],
    }),
  ];

  it("filters by product chip", async () => {
    const user = userEvent.setup();
    apiMock.features.mockResolvedValue(rows);
    renderBoard();
    await screen.findByText("Clinic feature");
    expect(screen.getByText("Payments feature")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clinic" }));
    expect(screen.getByText("Clinic feature")).toBeInTheDocument();
    expect(screen.queryByText("Payments feature")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clinic" }));
    expect(screen.getByText("Payments feature")).toBeInTheDocument();
  });

  it("filters to features that need my decision", async () => {
    const user = userEvent.setup();
    apiMock.features.mockResolvedValue(rows);
    renderBoard();
    await screen.findByText("Clinic feature");
    await user.click(screen.getByRole("button", { name: "Needs my decision" }));
    expect(screen.getByText("Clinic feature")).toBeInTheDocument();
    expect(screen.queryByText("Payments feature")).not.toBeInTheDocument();
  });

  it("shows the empty state when a filter matches nothing", async () => {
    const user = userEvent.setup();
    apiMock.features.mockResolvedValue(rows);
    renderBoard();
    await screen.findByText("Clinic feature");
    await user.click(screen.getByRole("button", { name: "Payments" }));
    await user.click(screen.getByRole("button", { name: "Needs my decision" }));
    expect(screen.getByText("No features here yet")).toBeInTheDocument();
  });
});

describe("cards", () => {
  it("renders status chips, cost, age and data class", async () => {
    apiMock.features.mockResolvedValue([
      feature({ pending: [pending({ gate: "g1" })] }),
      feature({ feature_id: "CLN-2", title: "Failed feature", error: "boom", pending: [] }),
      feature({
        feature_id: "CLN-3",
        title: "Escalated feature",
        pending: [pending({ type: "escalation", phase: "security" })],
      }),
      feature({ feature_id: "CLN-4", title: "Working feature", pending: [] }),
    ]);
    renderBoard();
    await screen.findByText("Plan feature").catch(() => screen.findByText("Add a CSV export"));
    expect(screen.getByText("Waiting for Idea approval (G1)")).toBeInTheDocument();
    expect(screen.getByText("Failed: see details")).toBeInTheDocument();
    expect(screen.getByText("Security findings: needs a person")).toBeInTheDocument();
    expect(screen.getByText("Agents working")).toBeInTheDocument();
    expect(screen.getAllByText(/Low risk\. \$1\.20 · 2 h ago/)).toHaveLength(4);
    expect(screen.getAllByText(/Health data \(PHI\)/)).toHaveLength(4);
  });

  it("links each card to its feature page", async () => {
    apiMock.features.mockResolvedValue([feature()]);
    renderBoard();
    const card = await screen.findByRole("link", { name: /Add a CSV export/ });
    expect(card).toHaveAttribute("href", "/features/CLN-1");
    expect(card).toHaveTextContent("from ada");
  });

  it("falls back to lane names when products() fails", async () => {
    apiMock.products.mockRejectedValue(new Error("nope"));
    apiMock.features.mockResolvedValue([feature()]);
    renderBoard();
    expect(await screen.findByText("Add a CSV export")).toBeInTheDocument();
    expect(screen.getAllByText("Clinic").length).toBeGreaterThan(0);
  });

  it("shows Released and Stopped chips for done and aborted features", async () => {
    const user = userEvent.setup();
    apiMock.features.mockResolvedValue([
      feature({ feature_id: "CLN-1", title: "Done one", status: "done" }),
      feature({ feature_id: "CLN-2", title: "Aborted one", status: "aborted" }),
    ]);
    renderBoard();
    await user.click(await screen.findByLabelText("Show released and stopped"));
    expect(screen.getByText("Released")).toBeInTheDocument();
    expect(screen.getByText("Stopped")).toBeInTheDocument();
  });
});

describe("card details and pending tones", () => {
  it("shows escalation and external waits, data classes, risk and missing creators", async () => {
    apiMock.products.mockResolvedValue({
      CLN: { name: "Clinic", kind: "product", ok: true, data_class: "phi" },
      PAY: { name: "Payments", kind: "product", ok: true },
      EXT: { name: "External", kind: "product", ok: true, data_class: "internal" },
    });
    apiMock.features.mockResolvedValue([
      feature({
        feature_id: "CLN-1",
        title: "Failed build",
        created_by: "",
        risk_tier: "high",
        pending: [pending({ type: "escalation", phase: "delivery", wait: "build" })],
      }),
      feature({
        feature_id: "PAY-1",
        product_id: "PAY",
        title: "Deploying",
        pending: [pending({ type: "external", wait: "dev" })],
      }),
      feature({ feature_id: "EXT-1", product_id: "EXT", title: "Outside product" }),
    ]);
    renderBoard();
    await screen.findByText("Failed build");
    expect(screen.getByText("OpenChoreo build failed: needs a person")).toHaveClass("chip", "stop");
    expect(screen.getByText("Deploying to dev")).toHaveClass("chip", "run");
    const failedCard = screen.getByText("Failed build").closest("a") as HTMLElement;
    expect(within(failedCard).getByText(/Health data \(PHI\)/)).toBeInTheDocument();
    expect(within(failedCard).getByText(/High risk\. \$1\.20/)).toBeInTheDocument();
    expect(within(failedCard).queryByText(/from /)).not.toBeInTheDocument();
    const outsideCard = screen.getByText("Outside product").closest("a") as HTMLElement;
    expect(within(outsideCard).getByText(/External · internal/)).toBeInTheDocument();
  });

  it("falls back to the raw product id for an unknown lane and toggles the filter off", async () => {
    const user = userEvent.setup();
    apiMock.features.mockResolvedValue([
      feature({ feature_id: "CLN-1", title: "Plan feature" }),
      feature({ feature_id: "ZZZ-1", product_id: "ZZZ", title: "Unknown lane feature" }),
    ]);
    renderBoard();
    await screen.findByText("Plan feature");
    expect(screen.getByRole("button", { name: "ZZZ" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "ZZZ" }));
    expect(screen.queryByText("Plan feature")).not.toBeInTheDocument();
    expect(screen.getByText("Unknown lane feature")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clinic" }));
    await user.click(screen.getByRole("button", { name: "Clinic" }));
    expect(screen.getByText("Plan feature")).toBeInTheDocument();
    expect(screen.getByText("Unknown lane feature")).toBeInTheDocument();
  });
});

it("clears the product and needs-me filters from the filter chips", async () => {
  const user = userEvent.setup();
  apiMock.features.mockResolvedValue([
    feature({ feature_id: "CLN-1", title: "Add a CSV export", status: "running", risk_tier: "high" }),
    feature({ feature_id: "CLN-2", title: "No risk tier", status: "running", risk_tier: undefined }),
    feature({
      feature_id: "PAY-1",
      product_id: "PAY",
      title: "Payments feature",
      pending: [pending({ can_decide: false, feature_id: "PAY-1" })],
    }),
  ]);
  renderBoard();
  expect(await screen.findByText("Add a CSV export")).toBeInTheDocument();
  expect(screen.getByText("No risk tier")).toBeInTheDocument();
  // A feature with no risk tier shows its cost with no risk label in
  // the cost line (the card title itself contains the word "risk").
  const card = screen.getByText("No risk tier").closest("a");
  const costLine = within(card as HTMLElement).getByText(/\$1\.20/);
  expect(costLine).toHaveTextContent(/^\$1\.20 · /);
  expect(costLine).not.toHaveTextContent("risk");
  await user.click(screen.getByRole("button", { name: "Clinic" }));
  expect(screen.queryByText("Payments feature")).not.toBeInTheDocument();
  expect(screen.getByText("No risk tier")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "All products" }));
  expect(screen.getByText("Payments feature")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Needs my decision" }));
  expect(screen.queryByText("Payments feature")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "All" }));
  expect(screen.getByText("Payments feature")).toBeInTheDocument();
});
