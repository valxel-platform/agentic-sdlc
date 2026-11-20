import { beforeEach, describe, expect, it, vi } from "vitest";
import { configure, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { apiMock, resetApiMock } from "../test/apiMock";
import { ApiError, type Feature, type PendingItem } from "../api";
import { FeaturePage } from "./Feature";

// Multi-step user-event flows and async fetches exceed Vitest's 5s default
// under parallel load on the shared VM; give them room.
vi.setConfig({ testTimeout: 20000 });
configure({ asyncUtilTimeout: 5000 });

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock };
});
vi.mock("../live", () => ({ useLive: () => {}, useLiveStatus: () => true, closeLive: vi.fn() }));

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

function feature(over: Partial<Feature> = {}): Feature {
  return {
    feature_id: "CLN-1",
    product_id: "CLN",
    title: "Add a CSV export",
    created_by: "ada",
    status: "running",
    running: true,
    rail: [
      { key: "discovery", kind: "phase", state: "done" },
      { key: "g1", kind: "gate", state: "waiting" },
      { key: "spec", kind: "phase", state: "current" },
    ],
    risk_tier: "low",
    cost_usd: 25,
    budget_usd: 100,
    pending: [pending()],
    updated_at: "2026-11-18T00:00:00Z",
    description: "Add a CSV export to the visits list",
    constitution_version: "v1+2",
    artifacts: { OPP: "opp", PRD: "prd" },
    pull_requests: {
      "org/svc#12": {
        repo: "org/svc",
        number: 12,
        url: "https://example.invalid/pr/12",
        state: "merged",
        merged_by: "ada",
        review_score: 0.9,
      },
    },
    change_set: {},
    release: {},
    gate_decisions: [
      {
        gate: "g1",
        phase: "spec",
        decision: "approve",
        approver: "ada",
        feedback: "Looks good",
        active_minutes: 5,
        at: "2026-11-01T00:00:00Z",
      },
    ],
    scores: {},
    loop_count: {},
    human_active_minutes: 3.5,
    usage: { calls: 10, cache_hit_rate: 0.5, est_cost_usd: 1, cost_by_phase: {} },
    events: [
      { at: "2026-11-01T00:00:00Z", node: "discovery", kind: "passed" },
      { at: "2026-11-01T01:00:00Z", node: "g1", kind: "approved", approver: "ada" },
    ],
    diagrams: [],
    delivery: {},
    deployments: {},
    quality: {},
    waivers: [],
    new_repos: [],
    runs: {},
    work_tracking: null,
    ...over,
  };
}

function renderFeature(id = "CLN-1") {
  render(
    <MemoryRouter initialEntries={[`/features/${id}`]}>
      <Routes>
        <Route path="/features/:id" element={<FeaturePage canSync={() => true} />} />
        <Route path="/initiatives/:id" element={<div>Initiative page</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

function renderFeatureNoSync(id = "CLN-1") {
  render(
    <MemoryRouter initialEntries={[`/features/${id}`]}>
      <Routes>
        <Route path="/features/:id" element={<FeaturePage canSync={() => false} />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  resetApiMock();
  apiMock.artifact.mockResolvedValue({
    id: "PRD",
    content: "# Product spec\n\nBody.",
    version: 1,
    constitution_version: "v1",
  });
  apiMock.featureCost.mockRejectedValue(new Error("no cost"));
  apiMock.featureOutcome.mockRejectedValue(new Error("no outcome"));
  apiMock.trace.mockResolvedValue({
    feature_id: "CLN-1",
    title: "Add a CSV export",
    profile: "feature",
    metrics: [],
    goals: [],
    criteria: [],
    nfrs: [],
    deferred: [],
    complete: true,
    decisions: [],
    repos: [],
    release: { flag: "", percent: null, production: false, merged: false },
    incidents: [],
    gaps: [],
  });
  apiMock.notifications.mockResolvedValue({
    email: "ada@example.com",
    needed: true,
    staging: false,
    watch: [],
    channels: { email: true, chat: false },
    sent: [],
  });
});

describe("FeaturePage loading and errors", () => {
  it("shows Loading… while the feature loads", () => {
    apiMock.feature.mockReturnValue(new Promise(() => {}));
    renderFeature();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("shows the not-found message for a 404", async () => {
    apiMock.feature.mockRejectedValue(new ApiError(404, "not found"));
    renderFeature();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This feature doesn't exist, or it's still starting.",
    );
  });

  it("shows a generic load error", async () => {
    apiMock.feature.mockRejectedValue(new Error("Server broke"));
    renderFeature();
    expect(await screen.findByRole("alert")).toHaveTextContent("Server broke");
  });
});

describe("FeaturePage header and rail", () => {
  it("renders the header meta", async () => {
    apiMock.feature.mockResolvedValue(feature());
    renderFeature();
    expect(await screen.findByRole("heading", { name: "Add a CSV export" })).toBeInTheDocument();
    expect(screen.getByText("Intent from ada")).toBeInTheDocument();
    expect(screen.getByText("Low risk")).toBeInTheDocument();
    expect(screen.getByText("Unplanned work")).toBeInTheDocument();
    expect(screen.getByText("Governed by 2 constitutions")).toBeInTheDocument();
  });

  it("renders the initiative, release flag and profile", async () => {
    apiMock.feature.mockResolvedValue(
      feature({
        initiative_id: "CLN-INI-1",
        profile: "express",
        release: {
          production: true,
          flag: "csv-export",
          flag_on: true,
          flag_percent: 50,
          release_id: "CLN-REL-1",
        },
        flag: "csv-export",
        lanes_touched: ["PAY"],
        config_variant: "canary",
      }),
    );
    renderFeature();
    expect(await screen.findByRole("link", { name: "CLN-INI-1" })).toHaveAttribute(
      "href",
      "/initiatives/CLN-INI-1",
    );
    expect(screen.getByText("Also changes PAY")).toBeInTheDocument();
    expect(screen.getByText("Express workflow")).toBeInTheDocument();
    expect(screen.getByText("Gradual release: on for 50% of users")).toBeInTheDocument();
    expect(screen.getByText("csv-export")).toBeInTheDocument();
    expect(
      screen.getByText(/Candidate \(canary\): compared with the config in use in the pilot report/),
    ).toBeInTheDocument();
  });

  it("shows Live when the flag is fully on", async () => {
    apiMock.feature.mockResolvedValue(
      feature({ release: { production: true, flag: "csv-export", flag_on: true, flag_percent: 100 } }),
    );
    renderFeature();
    await screen.findByRole("heading", { name: "Add a CSV export" });
    const head = document.querySelector(".feature-head") as HTMLElement;
    expect(within(head).getByText("Live")).toBeInTheDocument();
  });

  it("shows a waiting chip when the flag is off in production", async () => {
    apiMock.feature.mockResolvedValue(
      feature({ release: { production: true, flag: "csv-export", flag_on: false } }),
    );
    renderFeature();
    expect(await screen.findByText("In production, flag off")).toBeInTheDocument();
  });

  it("opens the detailed rail disclosure", async () => {
    const user = userEvent.setup();
    apiMock.feature.mockResolvedValue(feature());
    renderFeature();
    const summary = await screen.findByText("Every phase and gate, in detail");
    await user.click(summary);
    const rail = screen.getByRole("img", { name: /Discovery: done/ });
    expect(rail).toHaveTextContent("G1");
  });
});

describe("FeaturePage decisions, resize and change request", () => {
  it("renders the pending decision inline when the person can decide", async () => {
    apiMock.feature.mockResolvedValue(feature());
    renderFeature();
    expect(await screen.findByRole("heading", { name: "Is this worth building?" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve the idea" })).toBeInTheDocument();
  });

  it("says who is awaited when someone else must decide", async () => {
    apiMock.feature.mockResolvedValue(
      feature({ pending: [pending({ can_decide: false, approvers: ["ada"] })] }),
    );
    renderFeature();
    expect(await screen.findByText(/Waiting for Idea approval \(G1\) \(ada\)\./)).toBeInTheDocument();
  });

  it("offers to make the feature an initiative while at G1", async () => {
    const user = userEvent.setup();
    apiMock.feature.mockResolvedValue(feature());
    apiMock.resize.mockResolvedValue({ feature_id: "CLN-INI-1" });
    renderFeature();
    await user.click(await screen.findByRole("button", { name: "Make this an initiative" }));
    expect(apiMock.resize).toHaveBeenCalledWith("CLN-1");
    expect(await screen.findByText("Initiative page")).toBeInTheDocument();
  });

  it("does not offer resize once G1 has passed", async () => {
    apiMock.feature.mockResolvedValue(feature({ pending: [pending({ gate: "g2" })] }));
    renderFeature();
    await screen.findByRole("heading", { name: "Add a CSV export" });
    expect(screen.queryByRole("button", { name: "Make this an initiative" })).not.toBeInTheDocument();
  });

  it("submits a change request", async () => {
    const user = userEvent.setup();
    apiMock.feature.mockResolvedValue(feature());
    apiMock.changeRequest.mockResolvedValue({ feature_id: "CLN-1", sent_back: "", note: "" });
    renderFeature();
    await user.click(await screen.findByRole("button", { name: "Change the request" }));
    const box = screen.getByLabelText("What you're asking for");
    expect(box).toHaveValue("Add a CSV export to the visits list");
    const save = screen.getByRole("button", { name: "Save and update the work" });
    expect(save).toBeDisabled();
    await user.clear(box);
    await user.type(box, "short");
    expect(save).toBeDisabled();
    await user.clear(box);
    await user.type(box, "Add a CSV export and a PDF export too");
    await user.click(save);
    expect(apiMock.changeRequest).toHaveBeenCalledWith("CLN-1", "Add a CSV export and a PDF export too");
    expect(await screen.findByText(/Changed\. The next step uses it\./)).toBeInTheDocument();
  });

  it("reports a sent-back change request and a server error", async () => {
    const user = userEvent.setup();
    apiMock.feature.mockResolvedValue(feature());
    apiMock.changeRequest.mockResolvedValue({ feature_id: "CLN-1", sent_back: "spec", note: "" });
    renderFeature();
    await user.click(await screen.findByRole("button", { name: "Change the request" }));
    const box = screen.getByLabelText("What you're asking for");
    await user.clear(box);
    await user.type(box, "Add a CSV export and a PDF export too");
    await user.click(screen.getByRole("button", { name: "Save and update the work" }));
    expect(
      await screen.findByText(/Changed\. SPEC went back to the agents with your change\./),
    ).toBeInTheDocument();

    apiMock.changeRequest.mockRejectedValue(new Error("Request failed"));
    await user.click(await screen.findByRole("button", { name: "Change the request" }));
    await user.clear(screen.getByLabelText("What you're asking for"));
    await user.type(screen.getByLabelText("What you're asking for"), "Add a CSV export and a PDF export too");
    await user.click(screen.getByRole("button", { name: "Save and update the work" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Request failed");
  });

  it("says a change is a new request once the build has started", async () => {
    apiMock.feature.mockResolvedValue(
      feature({
        rail: [
          { key: "g4", kind: "gate", state: "done" },
          { key: "build", kind: "phase", state: "current" },
        ],
      }),
    );
    renderFeature();
    expect(
      await screen.findByText(/The build has started, so a change to the request is a/),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "new request" })).toHaveAttribute("href", "/intake");
  });

  it("resumes a stopped feature", async () => {
    const user = userEvent.setup();
    apiMock.feature.mockResolvedValue(feature({ error: "The builder crashed" }));
    renderFeature();
    expect(
      await screen.findByText(/The pipeline stopped with an error: The builder crashed/),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Resume" }));
    expect(apiMock.resume).toHaveBeenCalledWith("CLN-1");
    expect(apiMock.feature).toHaveBeenCalledTimes(2);
  });
});

describe("FeaturePage path, notify, timeline and cost", () => {
  it("renders the path panel with the expected date", async () => {
    apiMock.feature.mockResolvedValue(
      feature({
        path: {
          stations: [
            { key: "discovery", kind: "phase", state: "done", eta_minutes: 30, from_history: true },
            { key: "g1", kind: "gate", state: "waiting", eta_minutes: 60, from_history: false, mine: true },
            { key: "spec", kind: "phase", state: "todo", eta_minutes: 120, from_history: false },
          ],
          expected_done: new Date(Date.now() + 3 * 86400 * 1000).toISOString(),
          minutes_left: 180,
        },
      }),
    );
    renderFeature();
    expect(await screen.findByRole("heading", { name: "The path" })).toBeInTheDocument();
    expect(screen.getByText(/Expected to reach everyone/)).toBeInTheDocument();
    expect(screen.getByText("Needs you now")).toBeInTheDocument();
    expect(screen.getByText(/if you decide your step on time/)).toBeInTheDocument();
  });

  it("renders the notify panel and saves preferences", async () => {
    const user = userEvent.setup();
    apiMock.feature.mockResolvedValue(
      feature({
        path: {
          stations: [{ key: "discovery", kind: "phase", state: "done", eta_minutes: 30, from_history: true }],
          expected_done: null,
          minutes_left: null,
        },
        watching: false,
      }),
    );
    apiMock.watch.mockResolvedValue({ watching: true });
    renderFeature();
    expect(await screen.findByRole("heading", { name: "Tell me when I'm needed" })).toBeInTheDocument();
    await user.click(screen.getByLabelText("Watch this one"));
    expect(apiMock.watch).toHaveBeenCalledWith("CLN-1", true);
    await user.click(screen.getByLabelText(/When a decision waits for me/));
    expect(apiMock.setNotifications).toHaveBeenCalledWith({ needed: false });
    const email = screen.getByLabelText("Email");
    await user.clear(email);
    await user.type(email, "bo@example.com");
    fireEvent.blur(email);
    expect(apiMock.setNotifications).toHaveBeenCalledWith({ email: "bo@example.com" });
  });

  it("hides the notify panel when preferences fail to load", async () => {
    apiMock.notifications.mockRejectedValue(new Error("no prefs"));
    apiMock.feature.mockResolvedValue(
      feature({
        path: {
          stations: [{ key: "discovery", kind: "phase", state: "done", eta_minutes: 30, from_history: true }],
          expected_done: null,
          minutes_left: null,
        },
      }),
    );
    renderFeature();
    await screen.findByRole("heading", { name: "Add a CSV export" });
    expect(screen.queryByRole("heading", { name: "Tell me when I'm needed" })).not.toBeInTheDocument();
  });

  it("renders the event timeline with the running marker", async () => {
    apiMock.feature.mockResolvedValue(feature());
    renderFeature();
    expect(await screen.findByRole("heading", { name: "What happened" })).toBeInTheDocument();
    expect(screen.getByText("Discovery finished")).toBeInTheDocument();
    expect(screen.getByText("Idea approval (G1) approved by ada")).toBeInTheDocument();
    expect(screen.getByText("Agents working…")).toBeInTheDocument();
  });

  it("renders cost and effort, and survives cost/outcome failures", async () => {
    apiMock.feature.mockResolvedValue(
      feature({
        run_cost: {
          approved_monthly_usd: { production: 50 },
          projected_monthly_usd: { production: 60 },
          reviewed_at: "2026-11-01T00:00:00Z",
          findings: ["Running hotter than approved."],
          incidents: ["CLN-INC-1"],
        },
      }),
    );
    renderFeature();
    expect(await screen.findByText("Model spend")).toBeInTheDocument();
    expect(screen.getByText("$25.00 of $100.00 budget")).toBeInTheDocument();
    expect(screen.getByText("10")).toBeInTheDocument();
    expect(screen.getByText("50%")).toBeInTheDocument();
    expect(screen.getByText("3.5 min across 1 decisions")).toBeInTheDocument();
    expect(screen.getByText("$50.00/month in production")).toBeInTheDocument();
    expect(screen.getByText("Running hotter than approved.")).toBeInTheDocument();
    expect(screen.getByText("CLN-INC-1")).toBeInTheDocument();
    // BuildCost and Outcome sections stay hidden when their fetches fail.
    expect(screen.queryByText("Cost to build")).not.toBeInTheDocument();
    expect(screen.queryByText("Outcome")).not.toBeInTheDocument();
  });

  it("renders build cost when it loads", async () => {
    apiMock.feature.mockResolvedValue(feature());
    apiMock.featureCost.mockResolvedValue({
      feature_id: "CLN-1",
      product_id: "CLN",
      title: "Add a CSV export",
      status: "running",
      build: {
        total_usd: 5,
        by_category: [
          { category: "llm", label: "Model calls", usd: 5, basis: ["metered"], provisional: false },
        ],
        provisional: false,
      },
      run_30d: { total_usd: 2, by_category: [] },
    });
    renderFeature();
    expect(await screen.findByText("Cost to build")).toBeInTheDocument();
    expect(screen.getAllByText("Model calls")).toHaveLength(2);
    expect(screen.getByText(/Its own AWS services since release, last 30 days: \$2\.00/)).toBeInTheDocument();
  });

  it("renders the outcome section when it loads", async () => {
    apiMock.feature.mockResolvedValue(feature());
    apiMock.featureOutcome.mockResolvedValue({
      feature_id: "CLN-1",
      product_id: "CLN",
      goals: ["G1"],
      metrics: {},
      released_at: "2026-11-10T00:00:00Z",
      rollout: [{ percent: 50, goal: "No-shows down 5%" }],
      instrumentation: { checked: true, environment: "staging", fired: {}, missing: [], events: [] },
      readouts: [],
      upcoming: [{ goal_id: "G1", window_days: 7, due: "2026-11-17" }],
      record: {
        repo: "acme/cln-governance",
        path: "features/CLN-1/outcome.md",
        url: "https://github.com/acme/cln-governance/blob/HEAD/features/CLN-1/outcome.md",
      },
    });
    renderFeature();
    expect(await screen.findByRole("heading", { name: "Outcome" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "features/CLN-1/outcome.md" })).toHaveAttribute(
      "href",
      "https://github.com/acme/cln-governance/blob/HEAD/features/CLN-1/outcome.md",
    );
    expect(screen.getByText(/Moves G1\./)).toBeInTheDocument();
    expect(screen.queryByText(/Readouts start once the feature reaches everyone/)).not.toBeInTheDocument();
    expect(screen.getByText("At 50%")).toBeInTheDocument();
    expect(screen.getByText(/No-shows down 5%/)).toBeInTheDocument();
    expect(screen.getByText(/Next: 7-day readout 2026-11-17/)).toBeInTheDocument();
  });
});

describe("FeaturePage artifacts, quality, trace and extras", () => {
  it("switches artifact tabs, defaulting to the last present", async () => {
    const user = userEvent.setup();
    apiMock.feature.mockResolvedValue(feature());
    renderFeature();
    expect(await screen.findByRole("tab", { name: "Product spec" })).toHaveAttribute("aria-selected", "true");
    await user.click(screen.getByRole("tab", { name: "Opportunity brief" }));
    expect(apiMock.artifact).toHaveBeenCalledWith("CLN-1", "OPP");
    expect(await screen.findByText("Body.")).toBeInTheDocument();
  });

  it("says when no artifacts exist yet", async () => {
    apiMock.feature.mockResolvedValue(feature({ artifacts: {} }));
    renderFeature();
    expect(await screen.findByText("Agents haven't produced anything yet.")).toBeInTheDocument();
  });

  it("renders quality, trace, new repos, delivery and diagrams", async () => {
    apiMock.feature.mockResolvedValue(
      feature({
        quality: {
          pr: {
            stage: "pr",
            status: "passed",
            suites: [
              {
                stage: "pr",
                suite: "unit",
                kind: "test",
                repo: "org/svc",
                state: "passed",
                passed: 12,
                failed: 0,
                coverage: 91,
              },
            ],
            at: "2026-11-01T00:00:00Z",
          },
        },
        waivers: [],
        new_repos: [
          {
            repo: "org/visits-svc",
            role: "service",
            purpose: "Serves visits",
            status: "provisioned",
            steps: [{ step: "create", ok: true }],
          },
        ],
        delivery: {
          ci: {
            status: "success",
            wait: "ci",
            at: "2026-11-01T00:00:00Z",
            results: [{ repo: "org/svc", state: "success" }],
          },
        },
        diagrams: [
          {
            provider: "lucid",
            title: "Architecture",
            version: 2,
            url: "https://example.invalid/d/2",
            file: "arch.png",
            file_url: "https://example.invalid/arch.png",
            at: "2026-11-01T00:00:00Z",
          },
          {
            provider: "lucid",
            title: "Architecture",
            version: 1,
            url: "https://example.invalid/d/1",
            at: "2026-11-01T00:00:00Z",
          },
        ],
      }),
    );
    apiMock.trace.mockResolvedValue({
      feature_id: "CLN-1",
      title: "Add a CSV export",
      profile: "feature",
      metrics: [],
      goals: [{ id: "G1", text: "Fewer no-shows", metrics: [], requirements: ["R1"] }],
      criteria: [
        {
          id: "R1",
          type: "functional",
          text: "Export visits",
          goals: ["G1"],
          design: {},
          deferred: null,
          tasks: ["T1"],
          cases: [
            { id: "C1", req: "R1", kind: "unit", what: "exports", ref: "t", via: "ci", status: "passed" },
          ],
          test: "",
          prs: [
            {
              key: "org/svc#12",
              repo: "org/svc",
              number: 12,
              url: "https://example.invalid/pr/12",
              task: "T1",
              merged: true,
            },
          ],
          repos: ["org/svc"],
          acceptance: { staging: "passed" },
          deployed: { "org/svc": "production" },
        },
      ],
      nfrs: [],
      deferred: [],
      complete: false,
      decisions: [{ id: "D1", text: "Approved" }],
      repos: [],
      release: { flag: "", percent: null, production: false, merged: false },
      incidents: [],
      gaps: ["R2 has no test"],
    });
    renderFeature();
    expect(await screen.findByRole("heading", { name: "Tests and security" })).toBeInTheDocument();
    expect(screen.getByText("Unit tests")).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Traceability" })).toBeInTheDocument();
    expect(screen.getByText("1 gap below.")).toBeInTheDocument();
    const csv = screen.getByRole("link", { name: "Export the matrix (CSV)" });
    expect(csv).toHaveAttribute("href", "/api/features/CLN-1/trace.csv");
    expect(screen.getByText("R2 has no test")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "New repositories" })).toBeInTheDocument();
    expect(screen.getByText("Ready")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Delivery" })).toBeInTheDocument();
    expect(screen.getAllByText("Passed")).toHaveLength(2);
    expect(screen.getByRole("heading", { name: "Architecture diagrams" })).toBeInTheDocument();
    expect(screen.getByText("Latest")).toBeInTheDocument();
  });

  it("renders work tracking with sync only when allowed", async () => {
    const user = userEvent.setup();
    apiMock.feature.mockResolvedValue(
      feature({
        work_tracking: {
          provider: "jira",
          project: "CLN",
          items: [
            {
              item: "1",
              kind: "epic",
              key: "CLN-1",
              url: "https://example.invalid/jira/CLN-1",
              summary: "[CLN] Export epic",
              status: "todo",
            },
          ],
          error: null,
        },
      }),
    );
    renderFeature();
    expect(await screen.findByRole("heading", { name: /Jira items/ })).toBeInTheDocument();
    expect(screen.getByText("Export epic")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Sync now" }));
    expect(apiMock.syncWork).toHaveBeenCalledWith("CLN-1");
  });

  it("hides the sync button when sync is not allowed", async () => {
    apiMock.feature.mockResolvedValue(
      feature({
        work_tracking: {
          provider: "jira",
          project: "CLN",
          items: [{ item: "1", kind: "epic", key: "CLN-1", url: "", summary: "Epic", status: "todo" }],
          error: null,
        },
      }),
    );
    renderFeatureNoSync();
    expect(await screen.findByRole("heading", { name: /Jira items/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sync now" })).not.toBeInTheDocument();
  });

  it("renders pull requests and gate decisions", async () => {
    apiMock.feature.mockResolvedValue(feature());
    renderFeature();
    expect(await screen.findByRole("heading", { name: "Pull requests" })).toBeInTheDocument();
    expect(screen.getByText(/Merged by ada, review 0\.90/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Decisions" })).toBeInTheDocument();
    expect(screen.getByText(/approve by ada: “Looks good”/)).toBeInTheDocument();
  });

  it("renders attachments and agent sessions", async () => {
    apiMock.feature.mockResolvedValue(
      feature({
        attachments: [{ id: "a1", name: "shot.png", kind: "image", size: 2048, read_by_agents: false }],
        agent_sessions: [
          {
            trace_id: "t1",
            url: "https://example.invalid/t1",
            at: "2026-11-01T00:00:00Z",
            phase: "build",
            role: "coder",
            runtime: "x",
            model: "org/model-1",
            task: "Write the export",
            attempt: "1",
            calls: 4,
            duration_s: 120,
            tokens: 100,
            cost_usd: 0.5,
          },
        ],
      }),
    );
    renderFeature();
    expect(await screen.findByRole("heading", { name: "Attached at intake" })).toBeInTheDocument();
    expect(screen.getByText("shot.png")).toBeInTheDocument();
    const sessions = screen.getByText(/Agent sessions \(1\)/);
    await userEvent.setup().click(sessions);
    expect(await screen.findByText("Open trace")).toBeInTheDocument();
  });

  it("describes the main event kinds", async () => {
    apiMock.feature.mockResolvedValue(
      feature({
        events: [
          { at: "2026-11-01T00:00:00Z", node: "spec", kind: "started" },
          { at: "2026-11-01T00:00:00Z", node: "spec", kind: "revise", approver: "ada" },
          { at: "2026-11-01T00:00:00Z", node: "spec", kind: "escalated" },
          { at: "2026-11-01T00:00:00Z", node: "spec", kind: "aborted" },
          { at: "2026-11-01T00:00:00Z", node: "spec", kind: "merged" },
          { at: "2026-11-01T00:00:00Z", node: "spec", kind: "done" },
          { at: "2026-11-01T00:00:00Z", node: "provision", kind: "retry", approver: "ada" },
          { at: "2026-11-01T00:00:00Z", node: "spec", kind: "mystery_kind" },
        ],
        running: false,
        pending: [],
      }),
    );
    renderFeature();
    expect(await screen.findByText("Intent received")).toBeInTheDocument();
    expect(screen.getByText("Spec sent back by ada")).toBeInTheDocument();
    expect(screen.getByText("Spec escalated to a person")).toBeInTheDocument();
    expect(screen.getByText("Stopped at Spec")).toBeInTheDocument();
    expect(screen.getByText("Pull requests merged")).toBeInTheDocument();
    expect(screen.getByText("Released")).toBeInTheDocument();
    expect(screen.getByText("Repository setup retried by ada")).toBeInTheDocument();
    expect(screen.getByText("Spec: mystery kind")).toBeInTheDocument();
  });
});

it("warns when outcome events never fired in staging", async () => {
  apiMock.feature.mockResolvedValue(feature());
  apiMock.featureOutcome.mockResolvedValue({
    feature_id: "CLN-1",
    product_id: "CLN",
    goals: ["G1"],
    metrics: {},
    released_at: "2026-11-10T00:00:00Z",
    rollout: [],
    instrumentation: {
      checked: true,
      environment: "staging",
      fired: {},
      missing: ["signup"],
      events: [{ event: "signup", goals: ["G1"], repo: "org/svc" }],
    },
    readouts: [],
    upcoming: [],
  });
  renderFeature();
  expect(await screen.findByText(/In staging, signup never fired\./)).toBeInTheDocument();
});

it("renders every timeline event kind, the waiting note, run cost, attachments, quality, repos, diagrams, PRs and escalation decisions", async () => {
  apiMock.feature.mockResolvedValue(
    feature({
      pending: [pending({ can_decide: false })],
      rail: [
        { key: "discovery", kind: "phase", state: "done" },
        { key: "g1", kind: "gate", state: "waiting" },
        { key: "spec", kind: "phase", state: "current" },
        { key: "g4", kind: "gate", state: "done" },
      ],
      run_cost: {
        approved_monthly_usd: { production: 50 },
        projected_monthly_usd: { production: 60 },
        reviewed_at: "2026-11-01T00:00:00Z",
        findings: ["Cost spike in staging"],
        incidents: ["CLN-INC-1"],
      },
      attachments: [{ id: "a1", name: "shot.png", kind: "image", size: 10, read_by_agents: false }],
      quality: {
        pr: {
          stage: "pr",
          status: "passed",
          suites: [
            {
              stage: "pr",
              suite: "unit",
              kind: "test",
              repo: "org/svc",
              state: "passed",
              passed: 12,
              failed: 0,
              coverage: 91,
            },
          ],
          at: "2026-11-01T00:00:00Z",
        },
      },
      new_repos: [
        {
          repo: "org/visits-svc",
          role: "service",
          purpose: "Serves visits",
          status: "provisioned",
          steps: [{ step: "create", ok: true }],
        },
      ],
      diagrams: [
        {
          provider: "lucid",
          title: "Architecture",
          version: 2,
          url: "https://example.invalid/d/2",
          file: "arch.png",
          file_url: "https://example.invalid/arch.png",
          at: "2026-11-01T00:00:00Z",
        },
        {
          provider: "lucid",
          title: "Architecture",
          version: 1,
          url: "https://example.invalid/d/1",
          at: "2026-11-01T00:00:00Z",
        },
      ],
      pull_requests: {
        "org/svc#12": {
          repo: "org/svc",
          number: 12,
          url: "https://example.invalid/pr/12",
          state: "merged",
          merged_by: "ada",
          review_score: 0.9,
        },
        "org/other#3": { repo: "org/other", number: 3, url: "https://example.invalid/pr/3", state: "open" },
      },
      gate_decisions: [
        {
          gate: "g1",
          phase: "spec",
          decision: "approve",
          approver: "ada",
          feedback: "Looks good",
          active_minutes: 5,
          at: "2026-11-01T00:00:00Z",
        },
        {
          gate: "escalation_build",
          phase: "build",
          decision: "resolved",
          approver: "ada",
          feedback: "",
          active_minutes: 1,
          at: "2026-11-01T00:00:00Z",
          lane: "PAY",
        },
      ],
      events: [
        { at: "2026-11-01T00:00:00Z", node: "discovery", kind: "started" },
        { at: "2026-11-01T00:01:00Z", node: "build_wait", kind: "retry", approver: "ada" },
        { at: "2026-11-01T00:02:00Z", node: "build_wait", kind: "passed" },
        { at: "2026-11-01T00:03:00Z", node: "build_wait", kind: "passed", approver: "ada" },
        { at: "2026-11-01T00:04:00Z", node: "discovery", kind: "passed", attempts: 2 },
        { at: "2026-11-01T00:05:00Z", node: "discovery", kind: "passed", attempts: 1 },
        { at: "2026-11-01T00:06:00Z", node: "g1", kind: "approved" },
        { at: "2026-11-01T00:07:00Z", node: "g2", kind: "revise" },
        { at: "2026-11-01T00:08:00Z", node: "g2", kind: "escalated" },
        { at: "2026-11-01T00:09:00Z", node: "g3", kind: "resolved", approver: "ada" },
        { at: "2026-11-01T00:10:00Z", node: "g5", kind: "resolved" },
        { at: "2026-11-01T00:11:00Z", node: "build", kind: "retry" },
        { at: "2026-11-01T00:12:00Z", node: "build", kind: "aborted" },
        { at: "2026-11-01T00:13:00Z", node: "build", kind: "merged" },
        { at: "2026-11-01T00:14:00Z", node: "x", kind: "capability_registered" },
        { at: "2026-11-01T00:15:00Z", node: "x", kind: "design_and_architecture_approved" },
        { at: "2026-11-01T00:16:00Z", node: "x", kind: "waiting_for_other_gate" },
        { at: "2026-11-01T00:17:00Z", node: "x", kind: "done" },
        { at: "2026-11-01T00:18:00Z", node: "x", kind: "diagram_published", version: 3 },
        { at: "2026-11-01T00:19:00Z", node: "x", kind: "diagram_published" },
        { at: "2026-11-01T00:20:00Z", node: "x", kind: "diagram_withheld" },
        { at: "2026-11-01T00:21:00Z", node: "x", kind: "diagram_failed" },
        { at: "2026-11-01T00:22:00Z", node: "x", kind: "flag" },
        { at: "2026-11-01T00:23:00Z", node: "x", kind: "flag_failed" },
        { at: "2026-11-01T00:24:00Z", node: "g4", kind: "approved", approver: "ada", detail: "Looks solid" },
        { at: "2026-11-01T00:25:00Z", node: "mystery_node", kind: "approved" },
        { at: "2026-11-01T00:26:00Z", node: "build_wait", kind: "aborted" },
        { at: "2026-11-01T00:27:00Z", node: "x", kind: "custom_kind" },
      ],
    }),
  );
  apiMock.featureOutcome.mockResolvedValue({
    feature_id: "CLN-1",
    product_id: "CLN",
    goals: ["G1"],
    metrics: {
      G1: { goal_id: "G1", source: "sql", unit: "%", direction: "up", baseline: 10, target: 5, by: "" },
    },
    released_at: null,
    rollout: [],
    instrumentation: { checked: true, environment: "staging", fired: {}, missing: [], events: [] },
    readouts: [
      {
        feature_id: "CLN-1",
        goal_id: "G1",
        window_days: 7,
        released_at: "2026-11-10",
        source: "sql",
        unit: "%",
        direction: "up",
        before: { start: "", end: "", status: "ok", value: 10, n: null, note: "" },
        after: { start: "", end: "", status: "ok", value: 7, n: null, note: "" },
        change: -3,
        toward: true,
        caveat: "Small sample",
      },
    ],
    upcoming: [],
  });
  renderFeature();
  expect(await screen.findByText("Intent received")).toBeInTheDocument();
  expect(screen.getByText("Rebuild requested by ada")).toBeInTheDocument();
  // The passed build-wait renders the same text with and without an approver.
  expect(screen.getAllByText("OpenChoreo builds succeeded")).toHaveLength(2);
  expect(screen.getByText("Discovery finished after 2 attempts")).toBeInTheDocument();
  expect(screen.getByText("Discovery finished")).toBeInTheDocument();
  expect(screen.getByText("Idea approval (G1) approved")).toBeInTheDocument();
  expect(screen.getByText("Scope approval (G2) sent back")).toBeInTheDocument();
  expect(screen.getByText("Scope approval (G2) escalated to a person")).toBeInTheDocument();
  expect(screen.getByText("Design approval (G3) resolved by ada")).toBeInTheDocument();
  expect(screen.getByText("Code approval (G5) resolved by a person")).toBeInTheDocument();
  expect(screen.getByText("Build: agents retrying")).toBeInTheDocument();
  expect(screen.getByText("Stopped at Build")).toBeInTheDocument();
  expect(screen.getByText("Pull requests merged")).toBeInTheDocument();
  expect(screen.getByText("Capability added to the catalog")).toBeInTheDocument();
  expect(screen.getByText("Design and architecture both approved")).toBeInTheDocument();
  expect(screen.getByText("Waiting for the other design gate")).toBeInTheDocument();
  expect(screen.getByText("Released")).toBeInTheDocument();
  expect(screen.getByText("Architecture diagram v3 published")).toBeInTheDocument();
  expect(screen.getByText("Architecture diagram v published")).toBeInTheDocument();
  expect(screen.getByText("Architecture diagram kept off the diagram tool")).toBeInTheDocument();
  expect(screen.getByText("Architecture diagram couldn't be published")).toBeInTheDocument();
  expect(screen.getByText("Feature flag")).toBeInTheDocument();
  expect(screen.getByText("Feature flag couldn't be changed")).toBeInTheDocument();
  expect(screen.getByText("Architecture approval (G4) approved by ada")).toBeInTheDocument();
  expect(screen.getByText("Looks solid")).toBeInTheDocument();
  expect(screen.getByText("mystery node approved")).toBeInTheDocument();
  expect(screen.getByText("Stopped after a failed build")).toBeInTheDocument();
  expect(screen.getByText("x: custom kind")).toBeInTheDocument();
  // Nobody here can decide, so the page says who it is waiting for.
  expect(screen.getByText(/Waiting for Idea approval \(G1\) \(product-council\)\./)).toBeInTheDocument();
  // Run cost, findings and the incident link.
  expect(screen.getByText(/\$50\.00\/month in production/)).toBeInTheDocument();
  expect(screen.getByText(/\$60\.00\/month/)).toBeInTheDocument();
  expect(screen.getByText("Cost spike in staging")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "CLN-INC-1" })).toHaveAttribute("href", "/incidents/CLN-INC-1");
  // Attachments, quality, repos, diagrams, PRs and decisions.
  expect(screen.getByText("Attached at intake")).toBeInTheDocument();
  expect(screen.getByText("shot.png")).toBeInTheDocument();
  expect(screen.getByText("Unit tests")).toBeInTheDocument();
  expect(screen.getByText("org/visits-svc")).toBeInTheDocument();
  expect(screen.getByText("Version 2 in Lucid")).toBeInTheDocument();
  expect(screen.getByText("Merged by ada, review 0.90")).toBeInTheDocument();
  expect(screen.getByText("Open")).toBeInTheDocument();
  expect(screen.getByText("Build escalation (PAY)")).toBeInTheDocument();
  expect(screen.getByText("resolved by ada")).toBeInTheDocument();
  // The outcome has no release yet, so readouts wait — but the readout
  // table still shows the caveat.
  expect(await screen.findByText(/Readouts start once the feature reaches everyone/)).toBeInTheDocument();
  expect(screen.getByText("Small sample")).toBeInTheDocument();
});

it("shows a zero budget, no constitutions, no projected run cost, and missing optional sections", async () => {
  apiMock.feature.mockResolvedValue(
    feature({
      budget_usd: 0,
      cost_usd: 25,
      constitution_version: undefined as never,
      quality: undefined as never,
      new_repos: undefined as never,
      diagrams: undefined as never,
      attachments: undefined as never,
      // A g4 station that was skipped (not done) still counts as built,
      // and a pending item with no gate falls back to its phase name.
      rail: [
        { key: "discovery", kind: "phase", state: "done" },
        { key: "g4", kind: "gate", state: "skipped" },
      ],
      pending: [
        pending({ can_decide: false, gate: undefined as never, phase: "build" }),
        // No gate and no phase: the note falls back to the raw phase lookup.
        pending({
          can_decide: false,
          gate: undefined as never,
          phase: undefined as never,
          feature_id: "CLN-2",
        }),
      ],
      run_cost: {
        approved_monthly_usd: {},
        projected_monthly_usd: null,
        reviewed_at: null,
        findings: [],
        incidents: [],
      },
    }),
  );
  renderFeature();
  // money() uses three decimals below $1, so a zero budget is $0.000.
  expect(await screen.findByText(/\$25\.00 of \$0\.000 budget/)).toBeInTheDocument();
  expect(screen.getByText("Governed by 0 constitutions")).toBeInTheDocument();
  expect(screen.getByText("Reviewed 7 and 30 days after release")).toBeInTheDocument();
  expect(screen.queryByText("Attached at intake")).not.toBeInTheDocument();
  expect(screen.getByText(/\$0\.000\/month in production/)).toBeInTheDocument();
  expect(
    screen.getByText(/Waiting for Build \(product-council\); undefined \(product-council\)\./),
  ).toBeInTheDocument();
});

it("shows a projected run cost with no production figure and no review date", async () => {
  apiMock.feature.mockResolvedValue(
    feature({
      // No cost recorded yet: the spend percentage falls back to zero.
      cost_usd: undefined as never,
      run_cost: {
        approved_monthly_usd: { production: 50 },
        projected_monthly_usd: {},
        reviewed_at: null,
        findings: [],
        incidents: [],
      },
    }),
  );
  renderFeature();
  expect(await screen.findByText(/\$50\.00\/month in production/)).toBeInTheDocument();
  expect(screen.getByText(/\$0\.000\/month/)).toBeInTheDocument();
});

it("shows the next candidate when the feature was released from an initiative", async () => {
  apiMock.feature.mockResolvedValue(feature({ status: "released", initiative_id: "CLN-INI-1", pending: [] }));
  apiMock.initiative.mockResolvedValue({
    feature_id: "CLN-INI-1",
    kind: "initiative" as const,
    product_id: "CLN",
    title: "Cut no-shows",
    created_by: "ada",
    status: "approved",
    running: false,
    error: null,
    pending: [],
    events: [],
    cost_usd: 0,
    record_pr: {},
    request: "",
    content: "",
    draft: {
      goal: "",
      new_goal: null,
      measure: "",
      horizon: "now",
      budget_usd: 0,
      lanes: [],
      release_model: "launch" as const,
      candidates: [],
    },
    findings: [],
    features: [],
    candidates: [{ title: "Second", description: "Follow-up" }],
    budget_usd: 0,
    spent_usd: 0,
    progress: { released: 0, total: 0 },
  });
  renderFeature();
  expect(await screen.findByText("Second")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Start it" })).toBeInTheDocument();
});
