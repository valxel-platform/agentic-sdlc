import { beforeEach, describe, expect, it, vi } from "vitest";
import { configure, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { apiMock, resetApiMock } from "../test/apiMock";
import { ApiError, type InceptionSummary, type InceptionView, type Me } from "../api";
import { InceptionPage, ProductsPage } from "./Products";

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

function summary(over: Partial<InceptionSummary> = {}): InceptionSummary {
  return {
    id: "CLN-PRD-1",
    title: "Clinic product",
    org: "acme-water",
    existing: false,
    product_id: "CLN",
    status: "researching",
    created_by: "ada",
    cost_usd: 1.5,
    running: true,
    ...over,
  };
}

function renderProducts() {
  render(
    <MemoryRouter initialEntries={["/products"]}>
      <Routes>
        <Route path="/products" element={<ProductsPage me={me} />} />
        <Route path="/products/:id" element={<div>Product detail page</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

function renderInception(id = "CLN-PRD-1") {
  render(
    <MemoryRouter initialEntries={[`/products/${id}`]}>
      <Routes>
        <Route path="/products/:id" element={<InceptionPage />} />
        <Route path="/features/:id" element={<div>Feature detail page</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

function inceptionView(over: Partial<InceptionView> = {}): InceptionView {
  return {
    feature_id: "CLN-PRD-1",
    kind: "inception",
    product_id: "CLN",
    title: "Clinic product",
    created_by: "ada",
    status: "brief_gate",
    running: false,
    error: null,
    pending: [],
    events: [],
    cost_usd: 2.5,
    record_pr: {},
    vision: {
      name: "Clinic product",
      org: "acme-water",
      vision: "Clinics book online",
      goals: ["Fewer no-shows"],
      users: ["Clinic managers"],
      constraints: [],
      budget_usd: 10000,
      product_id: "CLN",
      existing: false,
    },
    brief: "## Brief\n\nA clinic product.",
    architecture: "## Architecture\n\nTwo services.",
    roadmap: "## Roadmap\n\nPhase one.",
    reports: {
      brief: {
        doc: "brief",
        status: "passed",
        attempts: 1,
        findings: ["Checked the market."],
        checklist: [],
        cost_usd: 1.25,
        monthly_usd: {},
        data_class: "general",
      },
    },
    research: "## Research\n\nThe market is big.",
    setup: {},
    council: ["product-council"],
    ...over,
  };
}

beforeEach(() => {
  resetApiMock();
  apiMock.artifact.mockResolvedValue({
    id: "PRD",
    content: "# Product spec\n\nBody.",
    version: 1,
    constitution_version: "v1",
  });
});

describe("ProductsPage list", () => {
  it("shows the loading state with no rows yet", () => {
    apiMock.inceptions.mockReturnValue(new Promise(() => {}));
    renderProducts();
    expect(screen.getByRole("heading", { name: "Products" })).toBeInTheDocument();
    expect(screen.queryByText("No products started here yet")).not.toBeInTheDocument();
  });

  it("shows an error when the list fails to load", async () => {
    apiMock.inceptions.mockRejectedValue(new Error("List failed"));
    renderProducts();
    expect(await screen.findByRole("alert")).toHaveTextContent("List failed");
  });

  it("shows the empty state", async () => {
    apiMock.inceptions.mockResolvedValue([]);
    renderProducts();
    expect(await screen.findByText("No products started here yet")).toBeInTheDocument();
  });

  it("renders rows with status, cost and a link", async () => {
    apiMock.inceptions.mockResolvedValue([
      summary(),
      summary({
        id: "PAY-PRD-1",
        title: "Payments product",
        status: "brief_gate",
        running: false,
        cost_usd: 0.5,
        existing: true,
        product_id: "PAY",
      }),
      summary({ id: "CLN-PRD-2", title: "Dropped product", status: "dropped", running: false, cost_usd: 0 }),
      summary({ id: "CLN-PRD-3", title: "Set up product", status: "set_up", running: false, cost_usd: 3 }),
    ]);
    renderProducts();
    expect(await screen.findByText("Clinic product")).toBeInTheDocument();
    expect(screen.getByText("Researching the problem and the market")).toBeInTheDocument();
    expect(screen.getByText("Waiting for the brief gate")).toBeInTheDocument();
    expect(screen.getByText("Stopped")).toBeInTheDocument();
    expect(screen.getByText("Set up: pull requests open")).toBeInTheDocument();
    expect(screen.getByText("$1.50")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Clinic product/ })).toHaveAttribute(
      "href",
      "/products/CLN-PRD-1",
    );
    expect(screen.getByText(/describing PAY/)).toBeInTheDocument();
  });
});

describe("NewProduct form", () => {
  it("opens the form with the inception wizard steps", async () => {
    const user = userEvent.setup();
    apiMock.inceptions.mockResolvedValue([]);
    renderProducts();
    await user.click(await screen.findByRole("button", { name: "Start a product" }));
    expect(screen.getByRole("heading", { name: "Start a product" })).toBeInTheDocument();
    const steps = screen.getByLabelText("Product inception steps");
    expect(steps).toHaveTextContent("1. Start");
    expect(steps).toHaveTextContent("2. Product inception");
    expect(steps).toHaveTextContent("3. First feature");
    expect(steps).toHaveTextContent("4. Review");
    expect(screen.getByText(/No repos are created during inception/)).toBeInTheDocument();
  });

  it("cancels out of the form", async () => {
    const user = userEvent.setup();
    apiMock.inceptions.mockResolvedValue([]);
    renderProducts();
    await user.click(await screen.findByRole("button", { name: "Start a product" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("heading", { name: "Start a product" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start a product" })).toBeInTheDocument();
  });

  it("auto-slugs the product id from the name and sanitises manual input", async () => {
    const user = userEvent.setup();
    apiMock.inceptions.mockResolvedValue([]);
    renderProducts();
    await user.click(await screen.findByRole("button", { name: "Start a product" }));
    const name = screen.getByLabelText("Name");
    await user.type(name, "Clinic");
    // The slug keeps the first letters typed, upper-cased.
    expect(screen.getByLabelText(/Product ID/)).toHaveValue("C");
    const pid = screen.getByLabelText(/Product ID/);
    await user.clear(pid);
    await user.type(pid, "cln-9!");
    expect(pid).toHaveValue("CLN");
  });

  it("validates name, org and vision with HTML constraints", async () => {
    const user = userEvent.setup();
    apiMock.inceptions.mockResolvedValue([]);
    renderProducts();
    await user.click(await screen.findByRole("button", { name: "Start a product" }));
    expect(screen.getByLabelText("Name")).toHaveAttribute("required");
    expect(screen.getByLabelText("Name")).toHaveAttribute("minLength", "2");
    expect(screen.getByLabelText(/GitHub org/)).toHaveAttribute("pattern", "[A-Za-z0-9][A-Za-z0-9-]*");
    expect(screen.getByLabelText(/The idea/)).toHaveAttribute("required");
    expect(screen.getByLabelText(/The idea/)).toHaveAttribute("minLength", "20");
    expect(screen.getByLabelText(/Product ID/)).toHaveAttribute("pattern", "[A-Z]{2,6}");
    // Too short a name does not submit.
    await user.type(screen.getByLabelText("Name"), "C");
    await user.type(screen.getByLabelText(/GitHub org/), "acme-water");
    await user.type(screen.getByLabelText(/The idea/), "Clinics need online booking for visits.");
    await user.click(screen.getByRole("button", { name: "Write the brief" }));
    expect(apiMock.createInception).not.toHaveBeenCalled();
  });

  it("submits a new product and navigates to it", async () => {
    const user = userEvent.setup();
    apiMock.inceptions.mockResolvedValue([]);
    apiMock.createInception.mockResolvedValue({ feature_id: "CLN-PRD-1" });
    renderProducts();
    await user.click(await screen.findByRole("button", { name: "Start a product" }));
    await user.type(screen.getByLabelText("Name"), "Clinic");
    await user.type(screen.getByLabelText(/GitHub org/), "acme-water");
    const pid = screen.getByLabelText(/Product ID/);
    await user.clear(pid);
    await user.type(pid, "CLN");
    await user.type(screen.getByLabelText(/The idea/), "Clinics need online booking for visits.");
    await user.type(
      screen.getByLabelText(/Goals, one per line/),
      "Fewer no-shows (measure: under 5 a month)\nFaster booking",
    );
    await user.type(screen.getByLabelText(/Users, one per line/), "Clinic managers");
    await user.type(screen.getByLabelText(/Budget for the first year/), "5000");
    await user.type(screen.getByLabelText(/Constraints, one per line/), "Must run on AWS.");
    await user.click(screen.getByLabelText(/I've done my own:/));
    await user.click(screen.getByLabelText(/Thorough: more alternatives/));
    await user.click(screen.getByRole("button", { name: "Write the brief" }));
    expect(apiMock.createInception).toHaveBeenCalledWith({
      name: "Clinic",
      org: "acme-water",
      vision: "Clinics need online booking for visits.",
      goals: ["Fewer no-shows (measure: under 5 a month)", "Faster booking"],
      users: ["Clinic managers"],
      constraints: ["Must run on AWS."],
      budget_usd: 5000,
      product_id: "CLN",
      research: "thorough",
    });
    expect(await screen.findByText("Product detail page")).toBeInTheDocument();
  });

  it("submits for an existing product, hiding name and org", async () => {
    const user = userEvent.setup();
    apiMock.inceptions.mockResolvedValue([]);
    apiMock.createInception.mockResolvedValue({ feature_id: "CLN-PRD-9" });
    renderProducts();
    await user.click(await screen.findByRole("button", { name: "Start a product" }));
    await user.selectOptions(screen.getByLabelText("Product"), "CLN");
    expect(screen.queryByLabelText("Name")).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/GitHub org/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/Product ID/)).not.toBeInTheDocument();
    await user.type(screen.getByLabelText(/The idea/), "Describe the clinic product and plan what's next.");
    await user.click(screen.getByRole("button", { name: "Write the brief" }));
    expect(apiMock.createInception).toHaveBeenCalledWith(
      expect.objectContaining({ name: "Clinic", org: "", product_id: "CLN" }),
    );
  });

  it("shows a server error and keeps the form open", async () => {
    const user = userEvent.setup();
    apiMock.inceptions.mockResolvedValue([]);
    apiMock.createInception.mockRejectedValue(new Error("product id CLN is already in use"));
    renderProducts();
    await user.click(await screen.findByRole("button", { name: "Start a product" }));
    await user.type(screen.getByLabelText("Name"), "Clinic");
    await user.type(screen.getByLabelText(/GitHub org/), "acme-water");
    await user.type(screen.getByLabelText(/The idea/), "Clinics need online booking for visits.");
    await user.clear(screen.getByLabelText(/Product ID/));
    await user.type(screen.getByLabelText(/Product ID/), "CLN");
    await user.click(screen.getByRole("button", { name: "Write the brief" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("product id CLN is already in use");
    expect(screen.getByRole("heading", { name: "Start a product" })).toBeInTheDocument();
  });

  it("disables submit while the creation is in flight", async () => {
    const user = userEvent.setup();
    apiMock.inceptions.mockResolvedValue([]);
    let resolveCreate!: (v: { feature_id: string }) => void;
    apiMock.createInception.mockReturnValue(
      new Promise((r) => {
        resolveCreate = r;
      }),
    );
    renderProducts();
    await user.click(await screen.findByRole("button", { name: "Start a product" }));
    await user.type(screen.getByLabelText("Name"), "Clinic");
    await user.type(screen.getByLabelText(/GitHub org/), "acme-water");
    await user.type(screen.getByLabelText(/The idea/), "Clinics need online booking for visits.");
    await user.clear(screen.getByLabelText(/Product ID/));
    await user.type(screen.getByLabelText(/Product ID/), "CLN");
    await user.click(screen.getByRole("button", { name: "Write the brief" }));
    expect(screen.getByRole("button", { name: "Write the brief" })).toBeDisabled();
    resolveCreate({ feature_id: "CLN-PRD-1" });
    expect(await screen.findByText("Product detail page")).toBeInTheDocument();
  });
});

describe("InceptionPage", () => {
  it("shows the loading state", () => {
    apiMock.inception.mockReturnValue(new Promise(() => {}));
    renderInception();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("shows the not-found message for a 404", async () => {
    apiMock.inception.mockRejectedValue(new ApiError(404, "not found"));
    renderInception();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This product inception doesn't exist, or it's still starting.",
    );
  });

  it("shows a generic load error", async () => {
    apiMock.inception.mockRejectedValue(new Error("Server broke"));
    renderInception();
    expect(await screen.findByRole("alert")).toHaveTextContent("Server broke");
  });

  it("renders the header, meta and the brief tab by default", async () => {
    apiMock.inception.mockResolvedValue(inceptionView());
    renderInception();
    expect(await screen.findByRole("heading", { name: "Clinic product" })).toBeInTheDocument();
    expect(screen.getByText("Waiting for the brief gate")).toBeInTheDocument();
    expect(screen.getByText("New product in acme-water")).toBeInTheDocument();
    expect(screen.getByText("From ada")).toBeInTheDocument();
    expect(screen.getByText("Decided by product-council")).toBeInTheDocument();
    expect(screen.getByText("$2.50 spent")).toBeInTheDocument();
    expect(screen.getByText("A clinic product.")).toBeInTheDocument();
    expect(screen.getByText("Checks passed")).toBeInTheDocument();
    expect(screen.getByText(/after 1 attempt, \$1\.25, as general data/)).toBeInTheDocument();
    expect(screen.getByText("Checked the market.")).toBeInTheDocument();
  });

  it("switches tabs and shows the empty-document notes", async () => {
    const user = userEvent.setup();
    apiMock.inception.mockResolvedValue(
      inceptionView({ brief: "", architecture: "", roadmap: "", research: "" }),
    );
    renderInception();
    await user.click(await screen.findByRole("tab", { name: "Research" }));
    expect(screen.getByText("An agent is researching the problem and the market.")).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Brief" }));
    expect(screen.getByText("Agents are writing the brief.")).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Architecture" }));
    expect(screen.getByText("Written after the brief gate approves the brief.")).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Roadmap" }));
    expect(screen.getByText("Written after the brief gate approves the brief.")).toBeInTheDocument();
  });

  it("shows the setup tab when the product is set up", async () => {
    apiMock.inception.mockResolvedValue(
      inceptionView({
        status: "set_up",
        setup: {
          steps: [
            { step: "governance_repo", ok: true, repo: "org/governance" },
            {
              step: "governance_pr",
              ok: true,
              skipped: true,
              url: "https://example.invalid/pr/1",
              line: "gh pr create",
            },
            { step: "teams", ok: false, error: "GitHub App missing" },
          ],
        },
      }),
    );
    renderInception();
    expect(await screen.findByText("Governance repo")).toBeInTheDocument();
    expect(screen.getByText("Done")).toBeInTheDocument();
    expect(screen.getByText("To do by hand")).toBeInTheDocument();
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(screen.getByText("GitHub App missing")).toBeInTheDocument();
    expect(screen.getByText("gh pr create")).toBeInTheDocument();
  });

  it("says setup runs after the product gate when there are no steps", async () => {
    const user = userEvent.setup();
    apiMock.inception.mockResolvedValue(inceptionView({ status: "product_gate" }));
    renderInception();
    await screen.findByRole("heading", { name: "Clinic product" });
    await user.click(screen.getByRole("tab", { name: "Setup" }));
    expect(screen.getByText("Runs when the product gate approves.")).toBeInTheDocument();
  });

  it("jumps to the architecture tab at the product gate", async () => {
    apiMock.inception.mockResolvedValue(inceptionView({ status: "product_gate" }));
    renderInception();
    expect(await screen.findByText("Two services.")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Architecture" })).toHaveAttribute("aria-selected", "true");
  });

  it("renders a pending decision inline", async () => {
    apiMock.inception.mockResolvedValue(
      inceptionView({
        pending: [
          {
            interrupt_id: "int-1",
            type: "gate",
            gate: "brief",
            approvers: ["product-council"],
            can_decide: true,
            feature_id: "CLN-PRD-1",
            product_id: "CLN",
          },
        ],
      }),
    );
    renderInception();
    expect(await screen.findByRole("heading", { name: "Is this the product we want?" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve brief" })).toBeInTheDocument();
  });

  it("starts the first feature from the setup tab", async () => {
    const user = userEvent.setup();
    apiMock.inception.mockResolvedValue(inceptionView({ status: "set_up", setup: { steps: [] } }));
    apiMock.firstFeature.mockResolvedValue({ feature_id: "CLN-1" });
    renderInception();
    await user.click(await screen.findByRole("button", { name: "Start the first feature" }));
    expect(apiMock.firstFeature).toHaveBeenCalledWith("CLN-PRD-1");
    expect(await screen.findByText("Feature detail page")).toBeInTheDocument();
  });

  it("shows a first-feature error", async () => {
    const user = userEvent.setup();
    apiMock.inception.mockResolvedValue(inceptionView({ status: "set_up", setup: { steps: [] } }));
    apiMock.firstFeature.mockRejectedValue(new Error("Roadmap not ready"));
    renderInception();
    await user.click(await screen.findByRole("button", { name: "Start the first feature" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Roadmap not ready");
  });

  it("merges a record pull request from the setup tab", async () => {
    const user = userEvent.setup();
    apiMock.inception.mockResolvedValue(inceptionView({ status: "set_up", setup: { steps: [] } }));
    apiMock.records.mockResolvedValue([
      {
        key: "org/governance#1",
        label: "Governance",
        url: "https://example.invalid/pr/1",
        merged: false,
        repo: "org/governance",
        number: 1,
      },
    ]);
    renderInception();
    await user.click(await screen.findByRole("button", { name: "Merge it" }));
    expect(apiMock.mergeRecord).toHaveBeenCalledWith("CLN-PRD-1", "org/governance#1");
  });
});

it("shows every setup step variant", async () => {
  apiMock.inception.mockResolvedValue(
    inceptionView({
      status: "set_up",
      setup: {
        steps: [
          { step: "registry_pr", ok: true, url: "https://example.invalid/pr/2" },
          { step: "mystery_step", ok: true, why: "Not needed here", teams: ["backend", "frontend"] },
        ],
      },
    }),
  );
  renderInception();
  expect(await screen.findByText("Registry pull request")).toBeInTheDocument();
  expect(screen.getByText("mystery_step")).toBeInTheDocument();
  expect(screen.getByText("Not needed here")).toBeInTheDocument();
  expect(screen.getByText("backend, frontend")).toBeInTheDocument();
});

it("shows the existing-product wording and an unknown status as-is", async () => {
  apiMock.inception.mockResolvedValue(
    inceptionView({
      status: "mystery",
      vision: {
        name: "Clinic",
        org: "",
        vision: "Clinics book online",
        goals: ["Fewer no-shows"],
        users: ["Clinic managers"],
        constraints: [],
        budget_usd: 10000,
        product_id: "CLN",
        existing: true,
      },
    }),
  );
  renderInception();
  expect(await screen.findByText("Describing CLN")).toBeInTheDocument();
  expect(screen.getByText("mystery")).toBeInTheDocument();
});
