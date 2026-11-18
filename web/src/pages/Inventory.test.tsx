import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { api, type Inventory, type Me } from "../api";
import { InventoryPage } from "./Inventory";

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

function inventoryFixture(overrides: Record<string, unknown> = {}): Inventory {
  return {
    product_id: "cln",
    at: "2026-11-18T12:00:00Z",
    project: "cln-proj",
    namespace: "cln-ns",
    pipeline: ["dev", "staging", "production"],
    items: [
      {
        kind: "component",
        name: "triage",
        type: "web",
        env: "dev",
        state: "ready",
        release: "1.2.0",
        size: "small",
        feature_id: "CLN-1",
        cost_30d_usd: 12,
        planned_monthly_usd: 20,
        outputs: [{ name: "url", value: "https://example.com" }],
        secrets: [{ output: "db", name: "db-secret", key: "password" }],
        ids: {
          openchoreo: "triage-dev",
          binding: "b1",
          kubectl: "kubectl get pods",
          aws_account: "111111111111",
          aws_region: "us-east-1",
          arn: "arn:aws:ecs:1",
        },
      },
      {
        kind: "component",
        name: "triage",
        type: "web",
        env: "production",
        state: "released",
        release: "1.2.0",
        feature_id: "CLN-1",
        ids: { openchoreo: "triage-prod", binding: "b1" },
      },
      {
        kind: "resource",
        name: "triage-db",
        type: "postgres",
        env: "production",
        state: "failed",
        release: "v3",
        feature_id: "CLN-2",
        ids: { arn: "arn:aws:rds:2" },
      },
      {
        kind: "store_app",
        name: "clinic-app",
        env: "production",
        state: "progressing",
        release: "2.0.0",
        feature_id: "CLN-3",
        ids: {},
      },
    ],
    summary: {
      per_env: {
        dev: {
          components: 1,
          resources: 0,
          store_apps: 0,
          not_ready: 0,
          cost_30d_usd: 12,
          planned_monthly_usd: 20,
        },
        staging: {
          components: 0,
          resources: 0,
          store_apps: 0,
          not_ready: 1,
          cost_30d_usd: 0,
          planned_monthly_usd: 0,
        },
        production: {
          components: 1,
          resources: 1,
          store_apps: 1,
          not_ready: 0,
          cost_30d_usd: 40,
          planned_monthly_usd: 60,
        },
      },
      production: { components: 1, resources: 1, store_apps: 1, cost_30d_usd: 40, planned_monthly_usd: 60 },
      lower: { components: 1, resources: 0, store_apps: 0, cost_30d_usd: 12, planned_monthly_usd: 20 },
    },
    unmanaged: [
      {
        env: "production",
        account: "111111111111",
        arn: "arn:aws:s3:orphan",
        resource_tag: "cln",
        feature: "CLN-9",
      },
    ],
    aws: "checked",
    problems: ["could not read staging metrics"],
    ...overrides,
  } as unknown as Inventory;
}

function renderInventory(initial = "/inventory?product=cln", meOverride: Me = me) {
  return render(
    <MemoryRouter initialEntries={[initial]}>
      <InventoryPage me={meOverride} />
    </MemoryRouter>,
  );
}

let clipboardWriteText: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.clearAllMocks();
  defaultRejectAll();
  clipboardWriteText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText: clipboardWriteText },
    configurable: true,
  });
});

afterEach(() => cleanup());

describe("InventoryPage", () => {
  it("shows the no-lane message when the user has no lanes", () => {
    renderInventory("/inventory", { ...me, lanes: [] });
    expect(screen.getByText("You aren't a member of any product's organization.")).toBeInTheDocument();
    expect(vi.mocked(api.inventory)).not.toHaveBeenCalled();
  });

  it("shows the reading state until the inventory resolves", () => {
    let resolve!: (v: Inventory) => void;
    vi.mocked(api.inventory).mockReturnValue(
      new Promise<Inventory>((r) => {
        resolve = r;
      }),
    );
    renderInventory();
    expect(screen.getByText("Reading OpenChoreo…")).toBeInTheDocument();
    resolve(inventoryFixture());
  });

  it("shows an error alert when the inventory load fails", async () => {
    vi.mocked(api.inventory).mockRejectedValue(new Error("openchoreo down"));
    renderInventory();
    expect(await screen.findByRole("alert")).toHaveTextContent("openchoreo down");
  });

  it("renders the environment table with production highlighted and lower totals", async () => {
    vi.mocked(api.inventory).mockResolvedValue(inventoryFixture());
    renderInventory();
    const section = await screen.findByRole("heading", { name: "Production and the environments before it" });
    const table = section.closest("section")!;
    expect(within(table).getByText("production")).toBeInTheDocument();
    expect(within(table).getByText("Before production, together")).toBeInTheDocument();
    expect(within(table).getByText("$40.00")).toBeInTheDocument();
    // Dev row and the lower-totals row both cost $12.00.
    expect(within(table).getAllByText("$12.00")).toHaveLength(2);
    const prodRow = within(table).getByText("production").closest("tr")!;
    expect(prodRow).toHaveClass("row-strong");
    expect(within(prodRow).getAllByText("1")).toHaveLength(3);
    expect(screen.getByText(/OpenChoreo project/)).toBeInTheDocument();
    expect(screen.getByText("cln-proj")).toBeInTheDocument();
    expect(screen.getByText("cln-ns")).toBeInTheDocument();
    expect(screen.queryByText(/AWS accounts:/)).not.toBeInTheDocument();
  });

  it("notes the AWS accounts line when AWS was not checked", async () => {
    vi.mocked(api.inventory).mockResolvedValue(inventoryFixture({ aws: "not configured" }));
    renderInventory();
    expect(await screen.findByText(/AWS accounts: not configured/)).toBeInTheDocument();
    expect(screen.getByText(/read live from OpenChoreo, with what you need/)).toBeInTheDocument();
  });

  it("skips pipeline environments with no summary", async () => {
    const fixture = inventoryFixture();
    delete fixture.summary.per_env.staging;
    vi.mocked(api.inventory).mockResolvedValue(fixture);
    renderInventory();
    const section = await screen.findByRole("heading", { name: "Production and the environments before it" });
    const table = section.closest("section")!;
    const rows = within(table).getAllByRole("row");
    expect(rows.map((r) => r.textContent)).not.toContain(expect.stringContaining("staging"));
  });

  it("shows unmanaged resources with copy feedback and the problems note", async () => {
    vi.mocked(api.inventory).mockResolvedValue(inventoryFixture());
    renderInventory();
    expect(await screen.findByText("In AWS, but nobody manages it")).toBeInTheDocument();
    expect(screen.getByText("arn:aws:s3:orphan")).toBeInTheDocument();
    expect(screen.getByText(/production, account 111111111111, tagged with CLN-9/)).toBeInTheDocument();
    expect(
      screen.getByText(/Some of it couldn't be read: could not read staging metrics/),
    ).toBeInTheDocument();
    const copy = screen.getByRole("button", { name: "Copy ARN" });
    // fireEvent, not user-event: userEvent.setup() replaces navigator.clipboard
    // with its own stub, which would hide the stubbed writeText.
    fireEvent.click(copy);
    expect(clipboardWriteText).toHaveBeenCalledWith("arn:aws:s3:orphan");
    expect(await screen.findByRole("button", { name: "Copy ARN" })).toHaveTextContent("Copied");
  });

  it("renders item groups with state chips and expands for ids", async () => {
    const user = userEvent.setup();
    vi.mocked(api.inventory).mockResolvedValue(inventoryFixture());
    renderInventory();
    expect(await screen.findByText("triage")).toBeInTheDocument();
    expect(screen.getByText("triage-db")).toBeInTheDocument();
    expect(screen.getByText("clinic-app")).toBeInTheDocument();
    expect(screen.getByText("Component, web")).toBeInTheDocument();
    expect(screen.getByText("AWS or managed service, postgres")).toBeInTheDocument();
    expect(screen.getByText("Store app")).toBeInTheDocument();
    expect(screen.getByText("ready")).toBeInTheDocument();
    expect(screen.getByText("failed")).toBeInTheDocument();
    expect(screen.getByText("progressing")).toBeInTheDocument();
    // Expand the triage group
    await user.click(screen.getByRole("button", { name: "triage" }));
    expect(screen.getByRole("link", { name: "CLN-1" })).toHaveAttribute("href", "/features/CLN-1");
    expect(screen.getByText(/triage-dev, binding b1/)).toBeInTheDocument();
    expect(screen.getByText(/account 111111111111, us-east-1/)).toBeInTheDocument();
    expect(screen.getByText(/url=https:\/\/example.com/)).toBeInTheDocument();
    expect(screen.getByText(/db: in Secret/)).toBeInTheDocument();
    expect(screen.getByText(/kubectl get pods/)).toBeInTheDocument();
    // Env table (dev + lower rows) + the expanded ids cost line.
    expect(screen.getAllByText("$12.00")).toHaveLength(3);
    expect(screen.getByText("$20.00 a month")).toBeInTheDocument();
  });

  it("shows the empty-items message when nothing is deployed", async () => {
    vi.mocked(api.inventory).mockResolvedValue(inventoryFixture({ items: [], unmanaged: [], problems: [] }));
    renderInventory();
    expect(await screen.findByText("Nothing deployed yet.")).toBeInTheDocument();
    expect(screen.queryByText("In AWS, but nobody manages it")).not.toBeInTheDocument();
  });

  it("refreshes on demand and links the CSV export", async () => {
    const user = userEvent.setup();
    vi.mocked(api.inventory).mockResolvedValue(inventoryFixture());
    renderInventory();
    await screen.findByText("triage");
    expect(vi.mocked(api.inventory)).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(vi.mocked(api.inventory)).toHaveBeenCalledTimes(2));
    expect(screen.getByRole("link", { name: "Download CSV" })).toHaveAttribute(
      "href",
      "/api/inventory/cln/export.csv",
    );
  });

  it("switching the product refetches", async () => {
    const user = userEvent.setup();
    vi.mocked(api.inventory).mockResolvedValue(inventoryFixture());
    renderInventory();
    await screen.findByText("triage");
    await user.selectOptions(screen.getByLabelText("Product"), "ops");
    await waitFor(() => expect(vi.mocked(api.inventory)).toHaveBeenCalledWith("ops"));
  });
});
