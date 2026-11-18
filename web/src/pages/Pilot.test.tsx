import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { api, type Me, type PilotReport } from "../api";
import { PilotPage } from "./Pilot";

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

function reportFixture(overrides: Record<string, unknown> = {}): PilotReport {
  return {
    product_id: "cln",
    count: 5,
    done: 2,
    features: [
      {
        feature_id: "CLN-1",
        title: "Triage queue",
        status: "released",
        cost_usd: 12.5,
        shadow_cost_usd: 1,
        human_minutes: 30,
        agent_minutes: 120,
        human_share: 0.2,
        first_time_approvals: 4,
        gates: 5,
        loops: { g5: 3, g6: 1 },
        ci_retries: 2,
        escalations: 1,
      },
      {
        feature_id: "CLN-2",
        title: "Alerts",
        status: "released",
        cost_usd: 8,
        shadow_cost_usd: 0.5,
        human_minutes: 10,
        agent_minutes: 90,
        human_share: 0.1,
        first_time_approvals: 5,
        gates: 5,
        loops: {},
        ci_retries: 0,
        escalations: 0,
      },
    ],
    critic_accuracy: {
      design: {
        passed_then_approved: 3,
        passed_then_revised: 1,
        failed_then_fixed: 2,
        failed_then_vouched: 0,
        false_pass_rate: 0.25,
        false_fail_rate: 0,
        missed_because: {},
      },
    },
    by_config: {
      "cfg-1": {
        features: 3,
        done: 2,
        first_time_approval_rate: 0.8,
        loops_per_feature: 1.5,
        critic_false_pass_rate: 0.1,
        pre_merge_fixes: 4,
        post_merge_fixes: 1,
        incidents: 0,
        traceability_overrides: 0,
        cost_per_feature_usd: 10,
        human_share: 0.15,
      },
    },
    estimated_minutes: { pr_review: 15, pr_comment: 5 },
    totals: {
      cost_usd: 20.5,
      shadow_cost_usd: 1.5,
      human_minutes: 40,
      agent_minutes: 210,
      human_share: 0.125,
      target_human_share: 0.15,
      first_time_approval_rate: 0.9,
      escalations: 1,
      ci_retries: 2,
      revise_reasons: { missing_requirement: 4, wrong_scope: 2, other: 1 },
      shadow_auto_approval: { gates: 10, eligible: 3, agreed: 2, would_have_missed: 1, minutes_saved: 45 },
      shadow_critic: { compared: 8, agreed: 6 },
      shadow_reviewer: { compared: 5, agreed: 5 },
      old_score_agreed: { compared: 4, agreed: 3 },
    },
    ...overrides,
  } as unknown as PilotReport;
}

function renderPilot(initial = "/pilot?product=cln", meOverride: Me = me) {
  return render(
    <MemoryRouter initialEntries={[initial]}>
      <PilotPage me={meOverride} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  defaultRejectAll();
});

afterEach(() => cleanup());

describe("PilotPage", () => {
  it("shows the no-lane message when the user has no lanes", () => {
    renderPilot("/pilot", { ...me, lanes: [] });
    expect(screen.getByText("You aren't a member of any product's organization.")).toBeInTheDocument();
    expect(vi.mocked(api.pilotReport)).not.toHaveBeenCalled();
  });

  it("shows an error alert when the pilot report fails", async () => {
    vi.mocked(api.pilotReport).mockRejectedValue(new Error("pilot down"));
    renderPilot();
    expect(await screen.findByRole("alert")).toHaveTextContent("pilot down");
  });

  it("shows the empty state when there are no pilot features", async () => {
    vi.mocked(api.pilotReport).mockResolvedValue(reportFixture({ count: 0, done: 0, features: [] }));
    renderPilot();
    expect(await screen.findByRole("heading", { name: "No pilot features yet" })).toBeInTheDocument();
    expect(screen.getByText(/Tick "Pilot feature" when you submit an intent/)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Where the time went" })).not.toBeInTheDocument();
  });

  it("renders the share totals, target and estimated-minutes note", async () => {
    vi.mocked(api.pilotReport).mockResolvedValue(reportFixture());
    renderPilot();
    expect(await screen.findByText(/against the target of 15% people\./)).toBeInTheDocument();
    expect(screen.getByText("12.5%")).toBeInTheDocument();
    expect(screen.getByText("40 min")).toBeInTheDocument();
    expect(screen.getByText("210 min")).toBeInTheDocument();
    expect(screen.getByText("2 released of 5")).toBeInTheDocument();
    expect(screen.getByText("90.0%")).toBeInTheDocument();
    expect(screen.getByText("$20.50")).toBeInTheDocument();
    expect(screen.getByText("(+$1.50 shadow)")).toBeInTheDocument();
    expect(
      screen.getByText(/People's minutes are decisions timed in this app, plus 15 min per/),
    ).toBeInTheDocument();
  });

  it("marks the share with the error style when it is over target", async () => {
    const fixture = reportFixture();
    fixture.totals.human_share = 0.25;
    vi.mocked(api.pilotReport).mockResolvedValue(fixture);
    renderPilot();
    await screen.findByText("Where the time went");
    const section = screen.getByRole("heading", { name: "Where the time went" }).closest("section")!;
    const share = within(section).getByText("25.0%");
    expect(share).toHaveClass("error-note");
  });

  it("renders the features table with computed retries", async () => {
    vi.mocked(api.pilotReport).mockResolvedValue(reportFixture());
    renderPilot();
    expect(await screen.findByText("Triage queue")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "CLN-1" })).toHaveAttribute("href", "/features/CLN-1");
    expect(screen.getByText("$12.50")).toBeInTheDocument();
    expect(screen.getByText("30 min")).toBeInTheDocument();
    expect(screen.getByText("120 min")).toBeInTheDocument();
    expect(screen.getByText("20.0%")).toBeInTheDocument();
    expect(screen.getByText("4/5")).toBeInTheDocument();
    // Loops {g5: 3, g6: 1} -> (3-1) + (1-1) = 2 agent retries; CI retries are 2 too.
    const row = screen.getByRole("link", { name: "CLN-1" }).closest("tr")!;
    expect(within(row).getAllByText("2")).toHaveLength(2);
    expect(within(row).getAllByText("1")).toHaveLength(1);
  });

  it("renders the shadow sections", async () => {
    vi.mocked(api.pilotReport).mockResolvedValue(reportFixture());
    renderPilot();
    expect(await screen.findByText("What the trial rules would have decided")).toBeInTheDocument();
    expect(screen.getByText(/Eligible at 3 of 10 gates\. People approved 2 of/)).toBeInTheDocument();
    expect(screen.getByText(/saving 45 min\) and sent back 1/)).toBeInTheDocument();
    expect(screen.getByText(/Agreed with the critic on 6 of 8 documents/)).toBeInTheDocument();
    expect(screen.getByText(/Agreed with the reviewer on 5 of 5 pull requests/)).toBeInTheDocument();
    expect(
      screen.getByText(/Would have decided the same as the checklist on 3 of 4 documents/),
    ).toBeInTheDocument();
  });

  it("renders the critic accuracy table", async () => {
    vi.mocked(api.pilotReport).mockResolvedValue(reportFixture());
    renderPilot();
    expect(await screen.findByText("How far the critic can be trusted")).toBeInTheDocument();
    const row = screen.getByText("design").closest("tr")!;
    expect(within(row).getByText("25.0%")).toBeInTheDocument();
    expect(within(row).getByText("0.0%")).toBeInTheDocument();
  });

  it("shows the no-documents note when the critic has no data", async () => {
    vi.mocked(api.pilotReport).mockResolvedValue(reportFixture({ critic_accuracy: {} }));
    renderPilot();
    expect(await screen.findByText("No documents have been decided yet.")).toBeInTheDocument();
  });

  it("renders the by-config table", async () => {
    vi.mocked(api.pilotReport).mockResolvedValue(reportFixture());
    renderPilot();
    expect(await screen.findByText("By configuration")).toBeInTheDocument();
    expect(screen.getByText("cfg-1")).toBeInTheDocument();
    expect(screen.getByText("80.0%")).toBeInTheDocument();
    expect(screen.getByText("1.5")).toBeInTheDocument();
    expect(screen.getByText("$10.00")).toBeInTheDocument();
  });

  it("renders revise reasons sorted with the most common first", async () => {
    vi.mocked(api.pilotReport).mockResolvedValue(reportFixture());
    renderPilot();
    expect(await screen.findByText("Why people sent work back")).toBeInTheDocument();
    const items = screen.getAllByText(/Something required was missing|Scope was wrong|Something else/);
    expect(items[0]).toHaveTextContent("Something required was missing");
    expect(items[1]).toHaveTextContent("Scope was wrong");
    const section = screen.getByRole("heading", { name: "Why people sent work back" }).closest("section")!;
    expect(within(section).getByText("4")).toBeInTheDocument();
  });

  it("shows the nothing-sent-back note when there are no revise reasons", async () => {
    const fixture = reportFixture();
    fixture.totals.revise_reasons = {};
    vi.mocked(api.pilotReport).mockResolvedValue(fixture);
    renderPilot();
    expect(await screen.findByText("Nothing was sent back.")).toBeInTheDocument();
  });

  it("links the Markdown download and switches product", async () => {
    const user = userEvent.setup();
    vi.mocked(api.pilotReport).mockResolvedValue(reportFixture());
    renderPilot();
    expect(await screen.findByRole("link", { name: "Download as Markdown" })).toHaveAttribute(
      "href",
      "/api/reports/pilot?product_id=cln&format=markdown",
    );
    await user.selectOptions(screen.getByLabelText("Product"), "ops");
    await waitFor(() => expect(vi.mocked(api.pilotReport)).toHaveBeenCalledWith("ops"));
  });
});
