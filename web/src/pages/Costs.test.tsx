import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { api, type Me, type ProductCost } from "../api";
import { CostsPage } from "./Costs";

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
  lanes: [
    { id: "cln", name: "Clinic" },
    { id: "ops", name: "Ops" },
  ],
};

function costFixture(overrides: Record<string, unknown> = {}): ProductCost {
  return {
    product_id: "cln",
    month: "2026-11",
    provisional: true,
    totals: { run_usd: 150, build_usd: 40, total_usd: 190, build_to_date_usd: 400 },
    run: {
      by_category: [
        { category: "compute", label: "Compute", usd: 100, basis: ["metered"], provisional: false },
        { category: "storage", label: "Storage", usd: 50, basis: ["billed"], provisional: true },
      ],
      by_env: { shared: 50, production: 100 },
    },
    build: {
      by_category: [
        { category: "llm", label: "Model calls", usd: 40, basis: ["estimate"], provisional: false },
      ],
    },
    features: [
      {
        feature_id: "CLN-1",
        title: "Triage queue",
        status: "released",
        build_to_date_usd: 200,
        build_month_usd: 25,
        run_month_usd: 60,
        approved_monthly_usd: 80,
      },
      {
        feature_id: "CLN-2",
        title: "Slow feature",
        status: "in_progress",
        build_to_date_usd: 50,
        build_month_usd: 15,
        run_month_usd: 0,
        approved_monthly_usd: null,
      },
      {
        feature_id: "CLN-3",
        title: "Stopped feature",
        status: "stopped",
        build_to_date_usd: 10,
        build_month_usd: 0,
        run_month_usd: 0,
      },
    ],
    ...overrides,
  } as unknown as ProductCost;
}

function renderCosts(initial = "/costs?product=cln&month=2026-11", meOverride: Me = me) {
  return render(
    <MemoryRouter initialEntries={[initial]}>
      <CostsPage me={meOverride} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  defaultRejectAll();
});

afterEach(() => cleanup());

describe("CostsPage", () => {
  it("shows the no-lane message when the user has no lanes", () => {
    renderCosts("/costs", { ...me, lanes: [] });
    expect(screen.getByText("You aren't a member of any product's organization.")).toBeInTheDocument();
    expect(vi.mocked(api.productCost)).not.toHaveBeenCalled();
  });

  it("shows the totals, provisional note and category tables", async () => {
    vi.mocked(api.productCost).mockResolvedValue(costFixture());
    renderCosts();
    expect(await screen.findByText("$190.00")).toBeInTheDocument();
    // Each total also appears as its category table's footer total; $40.00
    // is also the single build category's own value.
    expect(screen.getAllByText("$150.00")).toHaveLength(2);
    expect(screen.getAllByText("$40.00")).toHaveLength(3);
    expect(screen.getByText("(recent days may still change)")).toBeInTheDocument();
    expect(screen.getByText("2026-11 in all")).toBeInTheDocument();
    expect(screen.getByText("Compute")).toBeInTheDocument();
    expect(screen.getByText("usage at list price")).toBeInTheDocument();
    expect(screen.getByText("Storage")).toBeInTheDocument();
    expect(screen.getByText("from the AWS bill, may still change")).toBeInTheDocument();
    expect(screen.getByText("Model calls")).toBeInTheDocument();
    expect(screen.getByText("estimate")).toBeInTheDocument();
    expect(screen.getByText("By environment: Shared $50.00; production $100.00")).toBeInTheDocument();
  });

  it("shows an error alert when the cost load fails", async () => {
    vi.mocked(api.productCost).mockRejectedValue(new Error("costs down"));
    renderCosts();
    expect(await screen.findByRole("alert")).toHaveTextContent("costs down");
  });

  it("renders the per-feature rows with statuses and approved spend", async () => {
    vi.mocked(api.productCost).mockResolvedValue(costFixture());
    renderCosts();
    expect(await screen.findByText("Triage queue")).toBeInTheDocument();
    expect(screen.getByText("Live")).toBeInTheDocument();
    expect(screen.getByText("In progress")).toBeInTheDocument();
    expect(screen.getByText("Stopped")).toBeInTheDocument();
    expect(screen.getByText("$200.00")).toBeInTheDocument();
    expect(screen.getByText("$80.00/month")).toBeInTheDocument();
    expect(screen.getAllByText("–")).not.toHaveLength(0);
    expect(screen.getByRole("link", { name: "Triage queue" })).toHaveAttribute("href", "/features/CLN-1");
  });

  it("shows the empty-features message and the nothing-recorded note", async () => {
    const fixture = costFixture({ features: [] });
    fixture.run.by_category = [];
    fixture.build.by_category = [];
    vi.mocked(api.productCost).mockResolvedValue(fixture);
    renderCosts();
    expect(await screen.findByText("No features yet.")).toBeInTheDocument();
    expect(screen.getAllByText("Nothing recorded yet.")).toHaveLength(2);
  });

  it("defaults the month picker to the current month", async () => {
    vi.mocked(api.productCost).mockResolvedValue(costFixture());
    renderCosts("/costs?product=cln");
    const month = new Date().toISOString().slice(0, 7);
    const input = await screen.findByLabelText("Month");
    expect(input).toHaveValue(month);
    expect(vi.mocked(api.productCost)).toHaveBeenCalledWith("cln", month);
  });

  it("changing the month refetches with the new month", async () => {
    vi.mocked(api.productCost).mockResolvedValue(costFixture());
    renderCosts();
    await screen.findByText("Triage queue");
    const input = screen.getByLabelText("Month");
    fireEvent.change(input, { target: { value: "2026-10" } });
    await waitFor(() => expect(vi.mocked(api.productCost)).toHaveBeenCalledWith("cln", "2026-10"));
  });

  it("switching the product refetches and keeps the month", async () => {
    const user = userEvent.setup();
    vi.mocked(api.productCost).mockResolvedValue(costFixture());
    renderCosts();
    await screen.findByText("Triage queue");
    await user.selectOptions(screen.getByLabelText("Product"), "ops");
    await waitFor(() => expect(vi.mocked(api.productCost)).toHaveBeenCalledWith("ops", "2026-11"));
  });

  it("links the FOCUS CSV with product and month", async () => {
    vi.mocked(api.productCost).mockResolvedValue(costFixture());
    renderCosts();
    expect(await screen.findByRole("link", { name: "Download FOCUS CSV" })).toHaveAttribute(
      "href",
      "/api/costs/cln/focus.csv?month=2026-11",
    );
  });

  it("hides the provisional note when the month is final", async () => {
    vi.mocked(api.productCost).mockResolvedValue(costFixture({ provisional: false }));
    renderCosts();
    await screen.findByText("Triage queue");
    expect(screen.queryByText("(recent days may still change)")).not.toBeInTheDocument();
  });
});
