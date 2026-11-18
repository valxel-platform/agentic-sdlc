import { beforeEach, describe, expect, it, vi } from "vitest";
import { configure, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import type { PendingItem } from "../api";
import { apiMock, resetApiMock } from "../test/apiMock";
import { Inbox } from "./Inbox";

// Multi-step user-event flows and async fetches exceed Vitest's 5s default
// under parallel load on the shared VM; give them room.
vi.setConfig({ testTimeout: 20000 });
configure({ asyncUtilTimeout: 5000 });

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock };
});
vi.mock("../live", () => ({ useLive: () => {}, useLiveStatus: () => true, closeLive: vi.fn() }));

function item(over: Partial<PendingItem> = {}): PendingItem {
  return {
    interrupt_id: "int-1",
    type: "gate",
    gate: "g1",
    approvers: ["product-council"],
    can_decide: true,
    feature_id: "CLN-1",
    product_id: "CLN",
    product_name: "Clinic",
    title: "Add a CSV export",
    ...over,
  };
}

function renderInbox() {
  render(
    <MemoryRouter>
      <Inbox />
    </MemoryRouter>,
  );
}

/** The inbox item button for a given title (scoped to the list: the title
 * also appears in the decision panel header once selected). */
function itemButton(title: string): HTMLElement {
  const el = within(screen.getByRole("list")).getByText(title).closest("button");
  if (!el) throw new Error(`no item button for ${title}`);
  return el;
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

describe("Inbox loading, error and empty states", () => {
  it("shows the loading subtitle while the inbox loads", () => {
    apiMock.inbox.mockReturnValue(new Promise(() => {}));
    renderInbox();
    expect(screen.getByRole("heading", { name: "Decisions" })).toBeInTheDocument();
    expect(screen.getByText("A stage only moves forward when a person says yes.")).toBeInTheDocument();
  });

  it("shows an error when the inbox fails to load", async () => {
    apiMock.inbox.mockRejectedValue(new Error("Network down"));
    renderInbox();
    expect(await screen.findByRole("alert")).toHaveTextContent("Network down");
  });

  it("shows the empty state for my decisions, with links to board and intake", async () => {
    apiMock.inbox.mockResolvedValue([]);
    renderInbox();
    expect(await screen.findByText("Nothing is waiting on you")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "See all features" })).toHaveAttribute("href", "/board");
    expect(screen.getByRole("link", { name: "start a new request" })).toHaveAttribute("href", "/intake");
  });

  it("shows the empty state for everyone when the toggle is switched", async () => {
    const user = userEvent.setup();
    apiMock.inbox.mockResolvedValue([]);
    renderInbox();
    await screen.findByText("Nothing is waiting on you");
    await user.click(screen.getByRole("button", { name: "Everyone" }));
    expect(apiMock.inbox).toHaveBeenLastCalledWith(false);
    expect(await screen.findByText("Nothing is waiting on anyone")).toBeInTheDocument();
  });
});

describe("Inbox grouping and selection", () => {
  const items = [
    item({
      interrupt_id: "int-1",
      product_id: "CLN",
      product_name: "Clinic",
      initiative_id: "CLN-INI-1",
      feature_id: "CLN-1",
      title: "Add a CSV export",
    }),
    item({
      interrupt_id: "int-2",
      product_id: "CLN",
      product_name: "Clinic",
      initiative_id: "CLN-INI-1",
      feature_id: "CLN-2",
      title: "Add a PDF export",
    }),
    item({
      interrupt_id: "int-3",
      product_id: "PAY",
      product_name: "Payments",
      initiative_id: undefined,
      feature_id: "PAY-1",
      title: "Refund flow",
      type: "escalation",
      phase: "security",
      reason: "Findings",
    }),
  ];

  it("groups by product then initiative and auto-selects the first item", async () => {
    apiMock.inbox.mockResolvedValue(items);
    renderInbox();
    expect(await screen.findByText("3 waiting")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Clinic" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Payments" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "CLN-INI-1" })).toHaveAttribute("href", "/initiatives/CLN-INI-1");
    expect(screen.getByText("Not in an initiative")).toBeInTheDocument();
    // First item auto-selected: its decision panel is shown.
    expect(screen.getByRole("heading", { name: "Is this worth building?" })).toBeInTheDocument();
    expect(itemButton("Add a CSV export")).toHaveAttribute("aria-current", "true");
  });

  it("selects another item when clicked and shows its panel", async () => {
    const user = userEvent.setup();
    apiMock.inbox.mockResolvedValue(items);
    renderInbox();
    await screen.findByText("3 waiting");
    await user.click(itemButton("Refund flow"));
    expect(
      await screen.findByRole("heading", { name: "Security findings block this change" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Findings")).toBeInTheDocument();
  });

  it("keeps the selection when the list reloads and still contains it", async () => {
    const user = userEvent.setup();
    apiMock.inbox.mockResolvedValue(items);
    renderInbox();
    await screen.findByText("3 waiting");
    await user.click(itemButton("Add a PDF export"));
    // Reload with the same items: selection stays on the second item.
    apiMock.inbox.mockResolvedValue(items);
    await user.click(screen.getByRole("button", { name: "Everyone" }));
    await user.click(screen.getByRole("button", { name: "Waiting on me" }));
    await screen.findByText("3 waiting");
    expect(itemButton("Add a PDF export")).toHaveAttribute("aria-current", "true");
  });

  it("shows a Low risk chip on low-risk items the person can decide", async () => {
    apiMock.inbox.mockResolvedValue([item({ low_risk: true })]);
    renderInbox();
    expect(await screen.findByText("Low risk")).toBeInTheDocument();
  });

  it("names the approvers on items the person cannot decide", async () => {
    apiMock.inbox.mockResolvedValue([item({ can_decide: false, approvers: ["ada", "bo"] })]);
    renderInbox();
    expect(await screen.findByText(/CLN-1 for ada, bo/)).toBeInTheDocument();
  });

  it("goes back to the list from the decision panel", async () => {
    const user = userEvent.setup();
    apiMock.inbox.mockResolvedValue(items);
    renderInbox();
    await screen.findByText("3 waiting");
    await user.click(itemButton("Add a CSV export"));
    await user.click(screen.getByRole("button", { name: "Back to all decisions" }));
    expect(itemButton("Add a CSV export")).toBeInTheDocument();
  });

  it("approving an item in the panel reloads the inbox", async () => {
    const user = userEvent.setup();
    apiMock.inbox.mockResolvedValue(items);
    renderInbox();
    await user.click(await screen.findByRole("button", { name: "Approve the idea" }));
    expect(apiMock.decide).toHaveBeenCalled();
    expect(apiMock.inbox).toHaveBeenCalledTimes(2);
  });
});

describe("low-risk batch approval", () => {
  const easy = [
    item({ interrupt_id: "int-1", low_risk: true, feature_id: "CLN-1" }),
    item({ interrupt_id: "int-2", low_risk: true, feature_id: "CLN-2", title: "Second" }),
    item({ interrupt_id: "int-3", low_risk: false, feature_id: "CLN-3", title: "Third" }),
  ];

  it("offers batch approval only when more than one low-risk item waits", async () => {
    apiMock.inbox.mockResolvedValue(easy);
    renderInbox();
    expect(await screen.findByText(/2 are low risk:/)).toBeInTheDocument();
    expect(screen.getByText(/CLN-1, CLN-2/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve these 2" })).toBeInTheDocument();
  });

  it("does not offer batch approval for a single low-risk item", async () => {
    apiMock.inbox.mockResolvedValue([easy[0]]);
    renderInbox();
    await screen.findByText("1 waiting");
    expect(screen.queryByRole("region", { name: "Low-risk decisions" })).not.toBeInTheDocument();
  });

  it("approves the batch and reports approved and skipped", async () => {
    const user = userEvent.setup();
    apiMock.inbox.mockResolvedValue(easy);
    apiMock.approveBatch.mockResolvedValue({
      approved: ["CLN-1", "CLN-2"],
      skipped: [{ feature_id: "CLN-3", why: "needs a person" }],
    });
    renderInbox();
    await user.click(await screen.findByRole("button", { name: "Approve these 2" }));
    expect(apiMock.approveBatch).toHaveBeenCalledWith(
      [
        { feature_id: "CLN-1", interrupt_id: "int-1" },
        { feature_id: "CLN-2", interrupt_id: "int-2" },
      ],
      expect.any(Number),
    );
    const note = await screen.findByRole("status");
    expect(note).toHaveTextContent("Approved 2.");
    expect(note).toHaveTextContent("Left for one at a time: CLN-3 (needs a person).");
  });

  it("shows a batch error as an alert", async () => {
    const user = userEvent.setup();
    apiMock.inbox.mockResolvedValue(easy);
    apiMock.approveBatch.mockRejectedValue(new Error("Batch failed"));
    renderInbox();
    await user.click(await screen.findByRole("button", { name: "Approve these 2" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Batch failed");
  });
});
