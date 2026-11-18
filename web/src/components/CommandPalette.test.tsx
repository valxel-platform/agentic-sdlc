import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { cleanup, configure, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { useState } from "react";
import { CommandPalette } from "./CommandPalette";
import type { FeatureSummary, Me, PendingItem } from "../api";

// RTL does not auto-cleanup under vitest with globals: false.
afterEach(cleanup);

// Async waits and the test timeout get room: this VM is shared with other
// agents running the full suite at the same time.
configure({ asyncUtilTimeout: 5000 });
vi.setConfig({ testTimeout: 30000 });

const apiMock = vi.hoisted(() => ({
  features: vi.fn(),
  inbox: vi.fn(),
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
  lanes: [
    { id: "CLN", name: "Clean" },
    { id: "ACME", name: "Acme" },
  ],
  roles: { acme: "admin" },
};

const feature: FeatureSummary = {
  feature_id: "CLN-1",
  product_id: "CLN",
  title: "Add search",
  status: "in_progress",
  running: true,
  rail: [],
  pending: [],
};

const pending: PendingItem[] = [
  {
    interrupt_id: "i1",
    feature_id: "CLN-INI-7",
    title: "Approve the idea",
    type: "gate",
    gate: "g1",
    approvers: ["ada"],
    can_decide: true,
  },
  {
    interrupt_id: "i2",
    title: "Stuck gate",
    type: "escalation",
    approvers: ["ada"],
    can_decide: true,
  },
];

function LocationProbe() {
  const { pathname } = useLocation();
  return <div data-testid="loc">{pathname}</div>;
}

function Harness({ initialOpen = true }: { initialOpen?: boolean }) {
  const [open, setOpen] = useState(initialOpen);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Open palette
      </button>
      <CommandPalette open={open} onClose={() => setOpen(false)} me={me} />
      <LocationProbe />
    </>
  );
}

function renderPalette(initialOpen = true) {
  return render(
    <MemoryRouter initialEntries={["/today"]}>
      <Harness initialOpen={initialOpen} />
    </MemoryRouter>,
  );
}

const input = () => screen.getByLabelText("Search projects, approvals, apps");
const options = () => screen.queryAllByRole("option");

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.features.mockResolvedValue([feature]);
  apiMock.inbox.mockResolvedValue(pending);
});

describe("opening and closing", () => {
  it("renders nothing when closed", () => {
    renderPalette(false);
    expect(screen.queryByLabelText("Search projects, approvals, apps")).not.toBeInTheDocument();
    expect(apiMock.features).not.toHaveBeenCalled();
  });

  it("loads features and approvals on open and focuses the input", async () => {
    renderPalette();
    expect(input()).toHaveFocus();
    expect(apiMock.features).toHaveBeenCalledTimes(1);
    expect(apiMock.inbox).toHaveBeenCalledWith(true);
    expect(await screen.findByRole("option", { name: /Today/ })).toBeInTheDocument();
  });

  it("still shows the route entries when the data loads fail", async () => {
    apiMock.features.mockRejectedValue(new Error("down"));
    apiMock.inbox.mockRejectedValue(new Error("down"));
    renderPalette();
    expect(await screen.findByRole("option", { name: /Today/ })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Costs/ })).toBeInTheDocument();
  });

  it("closes on Escape", async () => {
    const user = userEvent.setup();
    renderPalette();
    await screen.findByRole("option", { name: /Today/ });
    await user.keyboard("{Escape}");
    expect(screen.queryByLabelText("Search projects, approvals, apps")).not.toBeInTheDocument();
  });

  it("closes when the overlay is clicked, but not when the palette itself is clicked", async () => {
    const user = userEvent.setup();
    renderPalette();
    await screen.findByRole("option", { name: /Today/ });
    await user.click(screen.getByRole("dialog"));
    expect(screen.queryByLabelText("Search projects, approvals, apps")).not.toBeInTheDocument();
    await user.click(screen.getByText("Open palette"));
    await screen.findByRole("option", { name: /Today/ });
    await user.click(screen.getByRole("listbox"));
    expect(screen.getByLabelText("Search projects, approvals, apps")).toBeInTheDocument();
  });

  it("resets the query when reopened", async () => {
    const user = userEvent.setup();
    renderPalette();
    await screen.findByRole("option", { name: /Today/ });
    await user.type(input(), "cost");
    expect(options()).toHaveLength(1);
    await user.keyboard("{Escape}");
    await user.click(screen.getByText("Open palette"));
    expect(input()).toHaveValue("");
    expect(options().length).toBeGreaterThan(1);
  });
});

describe("filtering", () => {
  it("shows every entry with an empty query, grouped", async () => {
    renderPalette();
    await screen.findByRole("option", { name: /Today/ });
    const groups = [...document.querySelectorAll(".palette-group")].map((el) => el.textContent);
    expect(groups).toEqual(["Go to", "Actions", "Features", "Approvals", "Products"]);
    expect(screen.getByRole("option", { name: /Add search/ })).toBeInTheDocument();
    expect(screen.getByText("CLN-1")).toBeInTheDocument();
  });

  it("filters live as the query is typed", async () => {
    const user = userEvent.setup();
    renderPalette();
    await screen.findByRole("option", { name: /Today/ });
    await user.type(input(), "road");
    expect(options()).toHaveLength(1);
    expect(options()[0]).toHaveTextContent("Roadmap");
  });

  it("matches on the hint as well as the label", async () => {
    const user = userEvent.setup();
    renderPalette();
    await screen.findByRole("option", { name: /Today/ });
    await user.type(input(), "cln-ini-7");
    expect(options()).toHaveLength(1);
    expect(options()[0]).toHaveTextContent("Approve the idea");
  });

  it("labels an approval by feature id when it has no title, and Decision when it has neither", async () => {
    const user = userEvent.setup();
    apiMock.inbox.mockResolvedValue([
      {
        interrupt_id: "i3",
        feature_id: "CLN-2",
        type: "gate",
        gate: "g1",
        approvers: ["ada"],
        can_decide: true,
      },
      { interrupt_id: "i4", type: "gate", gate: "g1", approvers: ["ada"], can_decide: true },
    ]);
    renderPalette();
    await screen.findByRole("option", { name: /Today/ });
    await user.type(input(), "cln-2");
    expect(options()).toHaveLength(1);
    expect(options()[0]).toHaveTextContent("CLN-2");
    await user.clear(input());
    await user.type(input(), "decision");
    expect(options()).toHaveLength(1);
    expect(options()[0]).toHaveTextContent("Decision");
  });

  it("matches a product lane by name", async () => {
    const user = userEvent.setup();
    renderPalette();
    await screen.findByRole("option", { name: /Today/ });
    await user.type(input(), "clean");
    const found = options();
    expect(found).toHaveLength(1);
    expect(found[0]).toHaveTextContent("Clean");
  });

  it("shows the empty state when nothing matches", async () => {
    const user = userEvent.setup();
    renderPalette();
    await screen.findByRole("option", { name: /Today/ });
    await user.type(input(), "zzzz");
    expect(options()).toHaveLength(0);
    expect(screen.getByText("Nothing matches “zzzz”.")).toBeInTheDocument();
  });

  it("trims and lower-cases the query", async () => {
    const user = userEvent.setup();
    renderPalette();
    await screen.findByRole("option", { name: /Today/ });
    await user.type(input(), "  COSTS ");
    expect(options()).toHaveLength(1);
    expect(options()[0]).toHaveTextContent("Costs");
  });
});

describe("keyboard navigation", () => {
  it("moves the selection with ArrowDown and ArrowUp, clamped at the ends", async () => {
    const user = userEvent.setup();
    renderPalette();
    const today = await screen.findByRole("option", { name: /Today/ });
    expect(today).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("option", { name: /Approvals/ })).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("option", { name: /Features/ })).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{ArrowUp}");
    expect(screen.getByRole("option", { name: /Approvals/ })).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{ArrowUp}");
    await user.keyboard("{ArrowUp}");
    expect(screen.getByRole("option", { name: /Today/ })).toHaveAttribute("aria-selected", "true");
  });

  it("does not move past the last entry", async () => {
    const user = userEvent.setup();
    renderPalette();
    await screen.findByRole("option", { name: /Today/ });
    const count = options().length;
    for (let i = 0; i < count + 3; i++) await user.keyboard("{ArrowDown}");
    expect(options()[count - 1]).toHaveAttribute("aria-selected", "true");
  });

  it("resets the selection when the query changes", async () => {
    const user = userEvent.setup();
    renderPalette();
    await screen.findByRole("option", { name: /Today/ });
    await user.keyboard("{ArrowDown}");
    await user.keyboard("{ArrowDown}");
    await user.type(input(), "cost");
    expect(options()[0]).toHaveAttribute("aria-selected", "true");
  });

  it("Enter navigates to the selected route and closes", async () => {
    const user = userEvent.setup();
    renderPalette();
    await screen.findByRole("option", { name: /Today/ });
    await user.keyboard("{ArrowDown}");
    await user.keyboard("{ArrowDown}");
    await user.keyboard("{Enter}");
    expect(await screen.findByText("/board")).toBeInTheDocument();
    expect(screen.queryByLabelText("Search projects, approvals, apps")).not.toBeInTheDocument();
  });

  it("Enter on a feature entry opens that feature", async () => {
    const user = userEvent.setup();
    renderPalette();
    await screen.findByRole("option", { name: /Today/ });
    await user.type(input(), "search");
    await user.keyboard("{Enter}");
    expect(await screen.findByText("/features/CLN-1")).toBeInTheDocument();
  });

  it("Enter on an approval entry opens its thread", async () => {
    const user = userEvent.setup();
    renderPalette();
    await screen.findByRole("option", { name: /Today/ });
    await user.type(input(), "approve the idea");
    await user.keyboard("{Enter}");
    expect(await screen.findByText("/initiatives/CLN-INI-7")).toBeInTheDocument();
  });

  it("Enter on an approval without a feature id falls back to the inbox", async () => {
    const user = userEvent.setup();
    renderPalette();
    await screen.findByRole("option", { name: /Today/ });
    await user.type(input(), "stuck gate");
    await user.keyboard("{Enter}");
    expect(await screen.findByText("/inbox")).toBeInTheDocument();
  });

  it("Enter on a lane entry opens the products page", async () => {
    const user = userEvent.setup();
    renderPalette();
    await screen.findByRole("option", { name: /Today/ });
    await user.type(input(), "acme");
    await user.keyboard("{Enter}");
    expect(await screen.findByText("/products")).toBeInTheDocument();
  });

  it("Enter with no matches does nothing", async () => {
    const user = userEvent.setup();
    renderPalette();
    await screen.findByRole("option", { name: /Today/ });
    await user.type(input(), "zzzz");
    await user.keyboard("{Enter}");
    expect(screen.getByTestId("loc")).toHaveTextContent("/today");
    expect(screen.getByLabelText("Search projects, approvals, apps")).toBeInTheDocument();
  });

  it("hovering an entry selects it", async () => {
    renderPalette();
    const roadmap = await screen.findByRole("option", { name: /Roadmap/ });
    fireEvent.mouseEnter(roadmap);
    expect(roadmap).toHaveAttribute("aria-selected", "true");
  });
});

describe("mouse navigation", () => {
  it("clicking an entry navigates and closes", async () => {
    const user = userEvent.setup();
    renderPalette();
    await user.click(await screen.findByRole("option", { name: /Costs/ }));
    expect(await screen.findByText("/costs")).toBeInTheDocument();
    expect(screen.queryByLabelText("Search projects, approvals, apps")).not.toBeInTheDocument();
  });

  it("clicking an action entry navigates to its route", async () => {
    const user = userEvent.setup();
    renderPalette();
    await user.click(await screen.findByRole("option", { name: /Start a new request/ }));
    expect(await screen.findByText("/intake")).toBeInTheDocument();
  });
});
