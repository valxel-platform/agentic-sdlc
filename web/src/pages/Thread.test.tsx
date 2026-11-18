import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import {
  api,
  ApiError,
  type InitiativeView,
  type PendingItem,
  type ReleaseView,
  type ReviewView,
} from "../api";
import { InitiativePage, ReleasePage, ReviewPage } from "./Thread";

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

function pending(overrides: Partial<PendingItem> = {}): PendingItem {
  return {
    interrupt_id: "int-1",
    type: "gate",
    gate: "g0",
    approvers: ["approvers-group"],
    can_decide: false,
    ...overrides,
  } as PendingItem;
}

function initiativeFixture(overrides: Record<string, unknown> = {}): InitiativeView {
  return {
    feature_id: "CLN-INI-1",
    kind: "initiative",
    product_id: "cln",
    title: "Faster triage",
    created_by: "ada",
    status: "drafting",
    running: false,
    error: null,
    pending: [],
    events: [],
    cost_usd: 0,
    record_pr: {},
    request: "Cut triage time.",
    content: "# Draft\n\nSome draft text.",
    draft: {
      goal: "goal-1",
      new_goal: null,
      measure: "median minutes",
      horizon: "next",
      budget_usd: 1000,
      lanes: ["cln"],
      release_model: "launch",
      candidates: [{ title: "Triage queue", description: "Queue view" }],
    },
    findings: [],
    features: [],
    candidates: [{ title: "Triage queue", description: "Queue view" }],
    budget_usd: 1000,
    spent_usd: 100,
    progress: { released: 0, total: 1 },
    research: "# Research\n\nResearch findings here.",
    ...overrides,
  } as unknown as InitiativeView;
}

function releaseFixture(overrides: Record<string, unknown> = {}): ReleaseView {
  return {
    feature_id: "CLN-REL-1",
    kind: "release",
    product_id: "cln",
    title: "November release",
    created_by: "ada",
    status: "ready",
    running: false,
    error: null,
    pending: [],
    events: [],
    cost_usd: 0,
    record_pr: {},
    model: "launch",
    target_date: "2026-11-30",
    initiative: "CLN-INI-1",
    notes: "# Notes\n\nRelease notes here.",
    evidence: "Evidence text.",
    approved_by: "",
    dropped: ["CLN-3"],
    features: [
      {
        feature_id: "CLN-1",
        title: "Triage queue",
        status: "released",
        stage: "Live",
        initiative_id: "CLN-INI-1",
        cost_usd: 0,
        flag_on: true,
      },
      {
        feature_id: "CLN-2",
        title: "Flagged feature",
        status: "released",
        stage: "Live",
        initiative_id: "CLN-INI-1",
        cost_usd: 0,
        flag_on: false,
      },
      {
        feature_id: "CLN-4",
        title: "Stopped feature",
        status: "stopped",
        stage: "Build",
        initiative_id: "CLN-INI-1",
        cost_usd: 0,
        flag_on: null,
      },
    ],
    ...overrides,
  } as unknown as ReleaseView;
}

function reviewFixture(overrides: Record<string, unknown> = {}): ReviewView {
  return {
    feature_id: "CLN-REV-1",
    kind: "review",
    product_id: "cln",
    title: "Quarterly review",
    created_by: "ada",
    status: "waiting",
    running: false,
    error: null,
    pending: [],
    events: [],
    cost_usd: 0,
    record_pr: {},
    initiative_id: "CLN-INI-2",
    why: "Goal check due",
    preview: "# Evidence\n\nReview evidence here.",
    decision: {},
    opened: [],
    ...overrides,
  } as unknown as ReviewView;
}

function renderThread(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/initiatives/:id" element={<InitiativePage />} />
        <Route path="/releases/:id" element={<ReleasePage />} />
        <Route path="/reviews/:id" element={<ReviewPage />} />
        <Route path="/features/:id" element={<div>feature page</div>} />
        <Route path="/roadmap" element={<div>roadmap page</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  defaultRejectAll();
});

afterEach(() => cleanup());

describe("InitiativePage", () => {
  it("shows Loading… then the draft, research and meta", async () => {
    vi.mocked(api.initiative).mockResolvedValue(initiativeFixture());
    renderThread("/initiatives/CLN-INI-1");
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Faster triage" })).toBeInTheDocument();
    expect(screen.getByText("Drafting")).toBeInTheDocument();
    expect(screen.getByText("From ada")).toBeInTheDocument();
    expect(screen.getByText("Next")).toBeInTheDocument();
    expect(screen.getByText("Launch group")).toBeInTheDocument();
    expect(screen.getByText("Some draft text.")).toBeInTheDocument();
    expect(screen.getByText("Triage queue")).toBeInTheDocument();
    expect(screen.getByText("Queue view")).toBeInTheDocument();
    // Research is behind a <details>
    const summary = screen.getByText("The research behind it");
    await userEvent.setup().click(summary);
    expect(await screen.findByText("Research findings here.")).toBeInTheDocument();
  });

  it("shows the 404 message for a missing initiative", async () => {
    vi.mocked(api.initiative).mockRejectedValue(new ApiError(404, "not found"));
    renderThread("/initiatives/CLN-INI-9");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This initiative doesn't exist, or it's still starting.",
    );
  });

  it("shows a generic load error", async () => {
    vi.mocked(api.initiative).mockRejectedValue(new Error("server blew up"));
    renderThread("/initiatives/CLN-INI-1");
    expect(await screen.findByRole("alert")).toHaveTextContent("server blew up");
  });

  it("shows the waiting note when someone else must decide", async () => {
    vi.mocked(api.initiative).mockResolvedValue(initiativeFixture({ pending: [pending()] }));
    renderThread("/initiatives/CLN-INI-1");
    expect(
      await screen.findByText("Waiting for Initiative approval (G0): approvers-group."),
    ).toBeInTheDocument();
  });

  it("shows the external waiting label when the blocker is external", async () => {
    vi.mocked(api.initiative).mockResolvedValue(
      initiativeFixture({
        pending: [pending({ type: "external", label: "Waiting on the vendor", gate: undefined })],
      }),
    );
    renderThread("/initiatives/CLN-INI-1");
    expect(await screen.findByText("Waiting on the vendor.")).toBeInTheDocument();
  });

  it("renders the decision panel and approves when the user can decide", async () => {
    const user = userEvent.setup();
    vi.mocked(api.initiative).mockResolvedValue(
      initiativeFixture({
        pending: [pending({ can_decide: true })],
        draft: undefined,
      }),
    );
    vi.mocked(api.decide).mockResolvedValue({ accepted: true });
    renderThread("/initiatives/CLN-INI-1");
    expect(await screen.findByRole("heading", { name: "Commit to this initiative?" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Approve initiative" }));
    await waitFor(() =>
      expect(vi.mocked(api.decide)).toHaveBeenCalledWith(
        "CLN-INI-1",
        "int-1",
        expect.objectContaining({ decision: "approve" }),
      ),
    );
  });

  it("hides the draft section while a G0 decision waits on the user", async () => {
    vi.mocked(api.initiative).mockResolvedValue(
      initiativeFixture({ pending: [pending({ can_decide: true })] }),
    );
    renderThread("/initiatives/CLN-INI-1");
    await screen.findByRole("heading", { name: "Commit to this initiative?" });
    expect(screen.queryByRole("heading", { name: "The initiative" })).not.toBeInTheDocument();
  });

  it("shows the stopped banner and resumes", async () => {
    const user = userEvent.setup();
    vi.mocked(api.initiative).mockResolvedValue(initiativeFixture({ error: "build exploded" }));
    vi.mocked(api.resume).mockResolvedValue({ accepted: true });
    renderThread("/initiatives/CLN-INI-1");
    expect(await screen.findByText(/The workflow stopped with an error: build exploded/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Resume" }));
    await waitFor(() => expect(vi.mocked(api.resume)).toHaveBeenCalledWith("CLN-INI-1"));
    expect(vi.mocked(api.initiative).mock.calls.length).toBeGreaterThan(1);
  });

  it("shows a resume error and stays on the page", async () => {
    const user = userEvent.setup();
    vi.mocked(api.initiative).mockResolvedValue(initiativeFixture({ error: "build exploded" }));
    vi.mocked(api.resume).mockRejectedValue(new Error("still broken"));
    renderThread("/initiatives/CLN-INI-1");
    await screen.findByText(/The workflow stopped with an error/);
    await user.click(screen.getByRole("button", { name: "Resume" }));
    expect(await screen.findByText("Couldn't resume: still broken")).toBeInTheDocument();
  });

  it("offers the resize link at G0 and follows it", async () => {
    const user = userEvent.setup();
    vi.mocked(api.initiative).mockResolvedValue(initiativeFixture({ pending: [pending()] }));
    vi.mocked(api.resize).mockResolvedValue({ feature_id: "CLN-2" });
    renderThread("/initiatives/CLN-INI-1");
    await screen.findByText("Faster triage");
    await user.click(screen.getByRole("button", { name: "Make it one feature" }));
    await waitFor(() => expect(vi.mocked(api.resize)).toHaveBeenCalledWith("CLN-INI-1"));
    expect(await screen.findByText("feature page")).toBeInTheDocument();
  });

  it("starts a candidate feature and navigates to it", async () => {
    const user = userEvent.setup();
    vi.mocked(api.initiative).mockResolvedValue(initiativeFixture({ status: "approved" }));
    vi.mocked(api.startCandidate).mockResolvedValue({ feature_id: "CLN-1" });
    renderThread("/initiatives/CLN-INI-1");
    expect(await screen.findByText(/0 of 1 started live\./)).toBeInTheDocument();
    expect(screen.getByText(/Budget: \$100.00 spent of \$1000.00/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Start feature" }));
    await waitFor(() => expect(vi.mocked(api.startCandidate)).toHaveBeenCalledWith("CLN-INI-1", 0));
    expect(await screen.findByText("feature page")).toBeInTheDocument();
  });

  it("shows a start-candidate error in place", async () => {
    const user = userEvent.setup();
    vi.mocked(api.initiative).mockResolvedValue(initiativeFixture({ status: "approved" }));
    vi.mocked(api.startCandidate).mockRejectedValue(new Error("already started"));
    renderThread("/initiatives/CLN-INI-1");
    await user.click(await screen.findByRole("button", { name: "Start feature" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("already started");
  });

  it("links a started candidate and marks later-added features", async () => {
    const fixture = initiativeFixture({
      status: "approved",
      features: [
        {
          feature_id: "CLN-1",
          title: "Triage queue",
          status: "released",
          stage: "Live",
          initiative_id: "CLN-INI-1",
          cost_usd: 0,
          flag_on: true,
        },
        {
          feature_id: "CLN-7",
          title: "Extra work",
          status: "in_progress",
          stage: "Build",
          initiative_id: "CLN-INI-1",
          cost_usd: 0,
          flag_on: null,
        },
      ],
    });
    vi.mocked(api.initiative).mockResolvedValue(fixture);
    renderThread("/initiatives/CLN-INI-1");
    await screen.findByText("Faster triage");
    expect(screen.getByRole("link", { name: "Triage queue" })).toHaveAttribute("href", "/features/CLN-1");
    expect(screen.getByText("Live")).toBeInTheDocument();
    expect(screen.getByText(/\(added later\)/)).toBeInTheDocument();
  });

  it("starts several features together and shows the started list", async () => {
    const user = userEvent.setup();
    const fixture = initiativeFixture({
      status: "approved",
      candidates: [
        { title: "Triage queue", description: "Queue view" },
        { title: "Alerts", description: "Alert view" },
      ],
    });
    vi.mocked(api.initiative).mockResolvedValue(fixture);
    vi.mocked(api.startFeatures).mockResolvedValue({ started: ["CLN-1", "CLN-2"], queued: [] });
    renderThread("/initiatives/CLN-INI-1");
    expect(await screen.findByRole("heading", { name: "What next?" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Start these 2 now" }));
    await waitFor(() =>
      expect(vi.mocked(api.startFeatures)).toHaveBeenCalledWith("CLN-INI-1", [0, 1], "together"),
    );
    expect(await screen.findByRole("heading", { name: "Started" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "CLN-1" })).toBeInTheDocument();
  });

  it("starts features in sequence and reports queued ones", async () => {
    const user = userEvent.setup();
    const fixture = initiativeFixture({
      status: "approved",
      candidates: [
        { title: "Triage queue", description: "" },
        { title: "Alerts", description: "" },
      ],
    });
    vi.mocked(api.initiative).mockResolvedValue(fixture);
    vi.mocked(api.startFeatures).mockResolvedValue({ started: ["CLN-1"], queued: ["CLN-2"] });
    renderThread("/initiatives/CLN-INI-1");
    await screen.findByRole("heading", { name: "What next?" });
    await user.click(screen.getByRole("button", { name: "Start in order, one at a time" }));
    await waitFor(() =>
      expect(vi.mocked(api.startFeatures)).toHaveBeenCalledWith("CLN-INI-1", [0, 1], "sequence"),
    );
    expect(
      await screen.findByText(/Then, one at a time as each reaches everyone: CLN-2/),
    ).toBeInTheDocument();
  });

  it("shows a start-features error", async () => {
    const user = userEvent.setup();
    vi.mocked(api.initiative).mockResolvedValue(initiativeFixture({ status: "approved" }));
    vi.mocked(api.startFeatures).mockRejectedValue(new Error("quota hit"));
    renderThread("/initiatives/CLN-INI-1");
    await screen.findByRole("heading", { name: "What next?" });
    await user.click(screen.getByRole("button", { name: "Start it now" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("quota hit");
  });

  it("shows open record PRs and merges one", async () => {
    const user = userEvent.setup();
    vi.mocked(api.initiative).mockResolvedValue(initiativeFixture({ status: "approved" }));
    vi.mocked(api.records).mockResolvedValue([
      {
        key: "r1",
        label: "Roadmap record",
        url: "https://example.com/pr/1",
        merged: false,
        repo: "org/gov",
        number: 1,
      },
      {
        key: "r2",
        label: "Old record",
        url: "https://example.com/pr/2",
        merged: true,
        repo: "org/gov",
        number: 2,
      },
    ]);
    vi.mocked(api.mergeRecord).mockResolvedValue({
      key: "r1",
      label: "Roadmap record",
      url: "https://example.com/pr/1",
      merged: true,
      repo: "org/gov",
      number: 1,
    });
    renderThread("/initiatives/CLN-INI-1");
    expect(await screen.findByText("Open")).toBeInTheDocument();
    expect(screen.getByText("Merged")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Roadmap record pull request #1" })).toHaveAttribute(
      "href",
      "https://example.com/pr/1",
    );
    await user.click(screen.getByRole("button", { name: "Merge it" }));
    await waitFor(() => expect(vi.mocked(api.mergeRecord)).toHaveBeenCalledWith("CLN-INI-1", "r1"));
  });

  it("links the record pull request and reports a record error", async () => {
    vi.mocked(api.initiative).mockResolvedValue(
      initiativeFixture({
        record_pr: { error: "private repo" },
      }),
    );
    renderThread("/initiatives/CLN-INI-1");
    expect(
      await screen.findByText("Couldn't open the record pull request: private repo"),
    ).toBeInTheDocument();
  });
});

describe("ReleasePage", () => {
  it("shows Loading… then features, notes, evidence and dropped items", async () => {
    vi.mocked(api.release).mockResolvedValue(releaseFixture());
    renderThread("/releases/CLN-REL-1");
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "November release" })).toBeInTheDocument();
    expect(screen.getByText("Launch group")).toBeInTheDocument();
    expect(screen.getByText("Ready for approval")).toBeInTheDocument();
    expect(screen.getByText("Target 2026-11-30")).toBeInTheDocument();
    expect(screen.getByText(/Each feature goes to production with its flag off/)).toBeInTheDocument();
    expect(screen.getByText("Live")).toBeInTheDocument();
    expect(screen.getByText("In production, flag off")).toBeInTheDocument();
    expect(screen.getByText("Stopped")).toBeInTheDocument();
    expect(screen.getByText("Release notes here.")).toBeInTheDocument();
    expect(screen.getByText("Evidence")).toBeInTheDocument();
    expect(screen.getByText("Evidence text.")).toBeInTheDocument();
    expect(screen.getByText("CLN-3: stopped, dropped from the release")).toBeInTheDocument();
  });

  it("shows the train copy for a release train and the 404 message", async () => {
    vi.mocked(api.release).mockResolvedValue(releaseFixture({ model: "train" }));
    const first = renderThread("/releases/CLN-REL-1");
    expect(await screen.findByText(/Each feature waits after staging/)).toBeInTheDocument();
    first.unmount();

    vi.mocked(api.release).mockRejectedValue(new ApiError(404, "gone"));
    renderThread("/releases/CLN-REL-9");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This release doesn't exist, or it's still starting.",
    );
  });

  it("shows the waiting note and the approved-by line", async () => {
    vi.mocked(api.release).mockResolvedValue(
      releaseFixture({
        pending: [pending({ gate: "release" })],
        approved_by: "sam",
        notes: "",
        evidence: "",
      }),
    );
    renderThread("/releases/CLN-REL-1");
    expect(await screen.findByText("Waiting for Release: approvers-group.")).toBeInTheDocument();
    expect(screen.getByText("Approved by sam")).toBeInTheDocument();
    expect(screen.getByText("Written once every feature is ready.")).toBeInTheDocument();
  });
});

describe("ReviewPage", () => {
  it("shows Loading… then the review status, why and preview", async () => {
    vi.mocked(api.review).mockResolvedValue(reviewFixture());
    renderThread("/reviews/CLN-REV-1");
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Quarterly review" })).toBeInTheDocument();
    expect(screen.getByText("Waiting for the product council")).toBeInTheDocument();
    expect(screen.getByText("Why now: Goal check due")).toBeInTheDocument();
    expect(screen.getByText("Review evidence here.")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Decided" })).not.toBeInTheDocument();
  });

  it("shows the decided block with approver, feedback and opened features", async () => {
    vi.mocked(api.review).mockResolvedValue(
      reviewFixture({
        status: "continued",
        decision: { approver: "sam", feedback: "Keep going" },
        opened: ["CLN-1", "CLN-2"],
      }),
    );
    renderThread("/reviews/CLN-REV-1");
    expect(await screen.findByRole("heading", { name: "Decided" })).toBeInTheDocument();
    // Status chip in the header + the bold word in the decided block.
    expect(screen.getAllByText("Continued")).toHaveLength(2);
    expect(screen.getByText(/by sam: Keep going/)).toBeInTheDocument();
    const decided = screen.getByRole("heading", { name: "Decided" }).closest("section")!;
    expect(within(decided).getByRole("link", { name: "CLN-1" })).toHaveAttribute("href", "/features/CLN-1");
    expect(within(decided).getByRole("link", { name: "CLN-2" })).toBeInTheDocument();
  });

  it("hides the preview while a decision waits on the user", async () => {
    vi.mocked(api.review).mockResolvedValue(
      reviewFixture({
        pending: [pending({ gate: "review", can_decide: true })],
      }),
    );
    renderThread("/reviews/CLN-REV-1");
    await screen.findByRole("heading", { name: "Quarterly review" });
    expect(screen.queryByText("Review evidence here.")).not.toBeInTheDocument();
  });

  it("shows the 404 message for a missing review", async () => {
    vi.mocked(api.review).mockRejectedValue(new ApiError(404, "gone"));
    renderThread("/reviews/CLN-REV-9");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This review doesn't exist, or it's still starting.",
    );
  });

  it("falls back to the raw status for an unknown review status", async () => {
    vi.mocked(api.review).mockResolvedValue(reviewFixture({ status: "mystery" }));
    renderThread("/reviews/CLN-REV-1");
    expect(await screen.findByText("mystery")).toBeInTheDocument();
  });

  it("shows a stopped review with no feedback and no opened features", async () => {
    vi.mocked(api.review).mockResolvedValue(
      reviewFixture({
        status: "stopped",
        decision: { approver: "sam" },
        opened: [],
      }),
    );
    renderThread("/reviews/CLN-REV-1");
    expect(await screen.findByRole("heading", { name: "Decided" })).toBeInTheDocument();
    // Status chip in the header + the bold word in the decided block.
    expect(screen.getAllByText("Stopped")).toHaveLength(2);
    expect(screen.getByText(/by sam$/)).toBeInTheDocument();
    expect(screen.queryByText(/Opened/)).not.toBeInTheDocument();
  });
});

describe("InitiativePage extra branches", () => {
  it("marks a flag-off started candidate and a stopped one", async () => {
    const fixture = initiativeFixture({
      status: "approved",
      candidates: [
        { title: "Triage queue", description: "", started: true },
        { title: "Alerts", description: "", started: true },
      ],
      features: [
        {
          feature_id: "CLN-1",
          title: "Triage queue",
          status: "released",
          stage: "Live",
          initiative_id: "CLN-INI-1",
          cost_usd: 0,
          flag_on: false,
        },
        {
          feature_id: "CLN-2",
          title: "Alerts",
          status: "stopped",
          stage: "Build",
          initiative_id: "CLN-INI-1",
          cost_usd: 0,
          flag_on: null,
        },
      ],
    });
    vi.mocked(api.initiative).mockResolvedValue(fixture);
    renderThread("/initiatives/CLN-INI-1");
    await screen.findByText("Faster triage");
    expect(screen.getByText("In production, flag off")).toBeInTheDocument();
    expect(screen.getByText("Stopped")).toBeInTheDocument();
    // Both candidates are started, so StartFeatures renders nothing.
    expect(screen.queryByRole("heading", { name: "What next?" })).not.toBeInTheDocument();
  });

  it("shows the drafting note without research when the agent has written nothing yet", async () => {
    vi.mocked(api.initiative).mockResolvedValue(initiativeFixture({ content: "", research: undefined }));
    renderThread("/initiatives/CLN-INI-1");
    expect(await screen.findByText("The agent is drafting it.")).toBeInTheDocument();
    expect(screen.queryByText("The research behind it")).not.toBeInTheDocument();
  });
});
