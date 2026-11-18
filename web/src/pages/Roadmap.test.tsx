import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { api, type Me, type Roadmap } from "../api";
import { RoadmapPage } from "./Roadmap";

vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  const mocked = Object.fromEntries(Object.keys(actual.api).map((k) => [k, vi.fn()]));
  return { ...actual, api: mocked };
});

type MockFn = { mockRejectedValue: (e: unknown) => unknown; mockResolvedValue: (v: never) => unknown };

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

function goalOutcome() {
  return {
    metric: {
      goal_id: "goal-1",
      source: "manual",
      unit: "min",
      direction: "down",
      baseline: 30,
      target: 10,
      by: "2026-12",
    },
    latest: 20,
    status: "on_track",
    why: "Improving",
    trend: [
      { end: "2026-11-01", value: 25, status: "ok", note: "" },
      { end: "2026-11-08", value: 20, status: "ok", note: "" },
    ],
  };
}

function roadmapFixture(overrides: Record<string, unknown> = {}): Roadmap {
  return {
    product_id: "cln",
    record: { repo: "org/gov", sha: "abcdef1234567", error: "" },
    goals: [
      {
        id: "goal-1",
        title: "Faster triage",
        measure: "median triage minutes",
        period: "2026",
        outcome: goalOutcome(),
      },
      { id: "goal-2", title: "No goal data", measure: "", period: "", outcome: null },
    ],
    outcomes: {
      wanted: [
        {
          goal_id: "goal-1",
          feature_id: "CLN-1",
          window_days: 30,
          start: "2026-10-01",
          end: "2026-10-31",
          unit: "min",
        },
      ],
      windows: [30],
      readouts: {},
    },
    initiatives: [
      {
        id: "CLN-INI-1",
        title: "Draft initiative",
        goal: "goal-1",
        horizon: "next",
        status: "drafting",
        budget_usd: 100,
        lanes: ["cln"],
        release_model: "launch",
        record: "",
        spent_usd: 0,
        features: [],
        progress: { released: 0, total: 0, planned: 0 },
      },
      {
        id: "CLN-INI-2",
        title: "Live initiative",
        goal: "goal-1",
        horizon: "now",
        status: "active",
        budget_usd: 100,
        lanes: ["cln"],
        release_model: "train",
        record: "pull request",
        spent_usd: 120,
        features: [
          {
            feature_id: "CLN-1",
            title: "Triage queue",
            status: "in_progress",
            stage: "Build",
            initiative_id: "CLN-INI-2",
            cost_usd: 0,
            flag_on: null,
          },
        ],
        progress: { released: 1, total: 2, planned: 3 },
        outcome: { goal_id: "goal-1", status: "on_track", why: "On track" },
        review_by: "2026-12-01",
        reviews: [{ id: "CLN-REV-1", status: "waiting", why: "quarterly check", opened: [] }],
      },
      {
        id: "CLN-INI-3",
        title: "Done initiative",
        goal: "",
        horizon: "later",
        status: "done",
        budget_usd: 50,
        lanes: ["cln"],
        release_model: "launch",
        record: "",
        spent_usd: 50,
        features: [],
        progress: { released: 1, total: 1, planned: 1 },
      },
      {
        id: "CLN-INI-4",
        title: "Dropped initiative",
        goal: "",
        horizon: "later",
        status: "dropped",
        budget_usd: 50,
        lanes: ["cln"],
        release_model: "launch",
        record: "",
        spent_usd: 0,
        features: [],
        progress: { released: 0, total: 0, planned: 0 },
      },
    ],
    releases: [
      {
        id: "CLN-REL-1",
        title: "November release",
        model: "launch",
        status: "planned",
        target_date: "2026-11-30",
        features: ["CLN-1"],
        record: "",
      },
      {
        id: "CLN-REL-2",
        title: "Old release",
        model: "train",
        status: "shipped",
        target_date: "",
        features: ["CLN-2"],
        record: "",
      },
    ],
    unplanned: [
      {
        feature_id: "CLN-9",
        title: "Unplanned fix",
        status: "in_progress",
        stage: "Build",
        initiative_id: "",
        cost_usd: 0,
        flag_on: null,
      },
    ],
    ...overrides,
  } as unknown as Roadmap;
}

function renderRoadmap(initial = "/roadmap?product=cln", meOverride: Me = me) {
  return render(
    <MemoryRouter initialEntries={[initial]}>
      <Routes>
        <Route path="/roadmap" element={<RoadmapPage me={meOverride} />} />
        <Route path="/initiatives/:id" element={<div>initiative page</div>} />
        <Route path="/releases/:id" element={<div>release page</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  defaultRejectAll();
});

afterEach(() => cleanup());

describe("RoadmapPage", () => {
  it("shows the no-lane message when the user has no lanes", () => {
    renderRoadmap("/roadmap", { ...me, lanes: [] });
    expect(screen.getByText("You aren't a member of any product's organization.")).toBeInTheDocument();
    expect(vi.mocked(api.roadmap)).not.toHaveBeenCalled();
  });

  it("shows the loading state (header only) until the roadmap resolves", () => {
    let resolve!: (v: Roadmap) => void;
    vi.mocked(api.roadmap).mockReturnValue(
      new Promise<Roadmap>((r) => {
        resolve = r;
      }),
    );
    renderRoadmap();
    expect(screen.getByRole("heading", { name: "Roadmap" })).toBeInTheDocument();
    expect(screen.queryByText("Faster triage")).not.toBeInTheDocument();
    resolve(roadmapFixture());
  });

  it("shows an error alert when the roadmap load fails", async () => {
    vi.mocked(api.roadmap).mockRejectedValue(new Error("roadmap down"));
    renderRoadmap();
    expect(await screen.findByRole("alert")).toHaveTextContent("roadmap down");
  });

  it("renders goals with outcome chips and the not-measured note", async () => {
    vi.mocked(api.roadmap).mockResolvedValue(roadmapFixture());
    renderRoadmap();
    expect(await screen.findByText("Faster triage")).toBeInTheDocument();
    // Goal outcome chip + the live initiative's outcome chip.
    expect(screen.getAllByText("On track")).toHaveLength(2);
    expect(screen.getByText("No goal data")).toBeInTheDocument();
    expect(screen.getByText(/Not measured: add a metric/)).toBeInTheDocument();
    expect(screen.getByText("Roadmap record: org/gov@abcdef1, roadmap/")).toBeInTheDocument();
  });

  it("renders the record error instead of the record line", async () => {
    const fixture = roadmapFixture();
    fixture.record = { repo: "org/gov", sha: "abcdef1234567", error: "repo gone" };
    vi.mocked(api.roadmap).mockResolvedValue(fixture);
    renderRoadmap();
    expect(await screen.findByText("Couldn't read the roadmap record: repo gone")).toBeInTheDocument();
  });

  it("renders the empty-goals message", async () => {
    vi.mocked(api.roadmap).mockResolvedValue(roadmapFixture({ goals: [] }));
    renderRoadmap();
    expect(await screen.findByText(/No goals yet/)).toBeInTheDocument();
  });

  it("renders in-flight, horizon and release sections; done/dropped initiatives stay out of the live lists", async () => {
    vi.mocked(api.roadmap).mockResolvedValue(roadmapFixture());
    renderRoadmap();
    expect(await screen.findByText("Draft initiative")).toBeInTheDocument();
    expect(screen.getByText("Live initiative")).toBeInTheDocument();
    expect(screen.queryByText("Done initiative")).not.toBeInTheDocument();
    expect(screen.queryByText("Dropped initiative")).not.toBeInTheDocument();
    // In-flight section + horizon sections
    expect(screen.getByRole("heading", { name: "Being drafted or decided" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Now" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Later" })).toBeInTheDocument();
    expect(
      within(screen.getByRole("heading", { name: "Later" }).closest("section")!).getByText("No initiatives."),
    ).toBeInTheDocument();
    // Releases
    expect(screen.getByText("November release")).toBeInTheDocument();
    expect(screen.getByText("Old release")).toBeInTheDocument();
    // Draft initiative card + November release chip.
    expect(screen.getAllByText("Launch group")).toHaveLength(2);
    expect(screen.getByText("Release train")).toBeInTheDocument();
    // Unplanned
    expect(screen.getByText("Unplanned fix")).toBeInTheDocument();
  });

  it("shows progress bars, over-budget spend and review waiting on initiative cards", async () => {
    vi.mocked(api.roadmap).mockResolvedValue(roadmapFixture());
    renderRoadmap();
    const bar = await screen.findByRole("img", { name: "$120.00 spent of $100.00" });
    expect(bar.querySelector("span")).toHaveClass("over");
    expect(screen.getByText("1 of 2 started features live, 1 not started yet")).toBeInTheDocument();
    expect(screen.getByText("$120.00 of $100.00")).toBeInTheDocument();
    expect(screen.getByText("Review waiting")).toBeInTheDocument();
    expect(screen.getByText(/quarterly check/)).toBeInTheDocument();
    expect(screen.getByText(/record waiting for its pull request to merge/)).toBeInTheDocument();
  });

  it("shows the next-review date when no review is waiting", async () => {
    const fixture = roadmapFixture();
    const live = fixture.initiatives[1];
    live.reviews = [];
    vi.mocked(api.roadmap).mockResolvedValue(fixture);
    renderRoadmap();
    expect(await screen.findByText("Next review 2026-12-01")).toBeInTheDocument();
  });

  it("shows the empty releases message when there are none", async () => {
    vi.mocked(api.roadmap).mockResolvedValue(roadmapFixture({ releases: [] }));
    renderRoadmap();
    expect(await screen.findByText(/No releases planned/)).toBeInTheDocument();
  });

  it("switching the product selector refetches for the new product", async () => {
    const user = userEvent.setup();
    vi.mocked(api.roadmap).mockResolvedValue(roadmapFixture());
    renderRoadmap();
    await screen.findByText("Faster triage");
    expect(vi.mocked(api.roadmap)).toHaveBeenCalledWith("cln");
    await user.selectOptions(screen.getByLabelText("Product"), "ops");
    await waitFor(() => expect(vi.mocked(api.roadmap)).toHaveBeenCalledWith("ops"));
  });

  it("submits a wanted number via recordReading and reloads", async () => {
    const user = userEvent.setup();
    vi.mocked(api.roadmap).mockResolvedValue(roadmapFixture());
    vi.mocked(api.recordReading).mockResolvedValue({
      reading: { start: "2026-10-01", end: "2026-10-31", status: "ok", value: 12, n: null, note: "" },
      readouts: [],
    });
    renderRoadmap();
    await screen.findByText("Numbers to enter");
    await user.type(screen.getByLabelText(/Value/), "12");
    await user.click(screen.getByRole("button", { name: "Save number" }));
    await waitFor(() =>
      expect(vi.mocked(api.recordReading)).toHaveBeenCalledWith("cln", "goal-1", {
        value: 12,
        start: "2026-10-01",
        end: "2026-10-31",
      }),
    );
    expect(vi.mocked(api.roadmap).mock.calls.length).toBeGreaterThan(1);
  });

  it("shows a wanted-form error when recordReading fails", async () => {
    const user = userEvent.setup();
    vi.mocked(api.roadmap).mockResolvedValue(roadmapFixture());
    vi.mocked(api.recordReading).mockRejectedValue(new Error("bad value"));
    renderRoadmap();
    await screen.findByText("Numbers to enter");
    await user.type(screen.getByLabelText(/Value/), "12");
    await user.click(screen.getByRole("button", { name: "Save number" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("bad value");
  });

  it("creates an initiative and navigates to it", async () => {
    const user = userEvent.setup();
    vi.mocked(api.roadmap).mockResolvedValue(roadmapFixture());
    vi.mocked(api.createInitiative).mockResolvedValue({ feature_id: "CLN-INI-9" });
    renderRoadmap();
    await screen.findByText("Faster triage");
    await user.click(screen.getByRole("button", { name: "New initiative" }));
    await user.type(screen.getByLabelText("Title"), "Better triage");
    await user.type(
      screen.getByLabelText(/What outcome do you want/),
      "Cut triage time in half; add a queue view.",
    );
    await user.selectOptions(screen.getByLabelText("When"), "now");
    await user.click(screen.getByLabelText(/Thorough: more alternatives/));
    await user.click(screen.getByRole("button", { name: "Draft initiative" }));
    await waitFor(() =>
      expect(vi.mocked(api.createInitiative)).toHaveBeenCalledWith({
        product_id: "cln",
        title: "Better triage",
        request: "Cut triage time in half; add a queue view.",
        measure: "",
        horizon: "now",
        research: "thorough",
      }),
    );
    expect(await screen.findByText("initiative page")).toBeInTheDocument();
  });

  it("shows a create-initiative error and lets the user retry", async () => {
    const user = userEvent.setup();
    vi.mocked(api.roadmap).mockResolvedValue(roadmapFixture());
    vi.mocked(api.createInitiative).mockRejectedValue(new Error("duplicate title"));
    renderRoadmap();
    await screen.findByText("Faster triage");
    await user.click(screen.getByRole("button", { name: "New initiative" }));
    await user.type(screen.getByLabelText("Title"), "Better triage");
    await user.type(
      screen.getByLabelText(/What outcome do you want/),
      "Cut triage time in half; add a queue view.",
    );
    await user.click(screen.getByRole("button", { name: "Draft initiative" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("duplicate title");
    expect(screen.getByRole("button", { name: "Draft initiative" })).not.toBeDisabled();
  });

  it("cancels the new-initiative form", async () => {
    const user = userEvent.setup();
    vi.mocked(api.roadmap).mockResolvedValue(roadmapFixture());
    renderRoadmap();
    await screen.findByText("Faster triage");
    await user.click(screen.getByRole("button", { name: "New initiative" }));
    expect(screen.getByRole("heading", { name: "New initiative" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("heading", { name: "New initiative" })).not.toBeInTheDocument();
  });

  it("creates a release only after picking at least one feature", async () => {
    const user = userEvent.setup();
    vi.mocked(api.roadmap).mockResolvedValue(roadmapFixture());
    vi.mocked(api.createRelease).mockResolvedValue({ feature_id: "CLN-REL-9" });
    renderRoadmap();
    await screen.findByText("Faster triage");
    await user.click(screen.getByRole("button", { name: "New release" }));
    const plan = screen.getByRole("button", { name: "Plan release" });
    expect(plan).toBeDisabled();
    await user.type(screen.getByLabelText("Title"), "December release");
    await user.click(screen.getByLabelText(/Release train/));
    await user.click(screen.getByLabelText(/CLN-1 Triage queue/));
    expect(plan).not.toBeDisabled();
    await user.click(plan);
    await waitFor(() =>
      expect(vi.mocked(api.createRelease)).toHaveBeenCalledWith({
        product_id: "cln",
        title: "December release",
        model: "train",
        features: ["CLN-1"],
        target_date: "",
      }),
    );
    expect(await screen.findByText("release page")).toBeInTheDocument();
  });

  it("shows a release create error and re-enables the button", async () => {
    const user = userEvent.setup();
    vi.mocked(api.roadmap).mockResolvedValue(roadmapFixture());
    vi.mocked(api.createRelease).mockRejectedValue(new Error("release failed"));
    renderRoadmap();
    await screen.findByText("Faster triage");
    await user.click(screen.getByRole("button", { name: "New release" }));
    await user.type(screen.getByLabelText("Title"), "December release");
    await user.click(screen.getByLabelText(/CLN-1 Triage queue/));
    await user.click(screen.getByRole("button", { name: "Plan release" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("release failed");
    expect(screen.getByRole("button", { name: "Plan release" })).not.toBeDisabled();
  });

  it("sends the target date when one is picked", async () => {
    const user = userEvent.setup();
    vi.mocked(api.roadmap).mockResolvedValue(roadmapFixture());
    vi.mocked(api.createRelease).mockResolvedValue({ feature_id: "CLN-REL-9" });
    renderRoadmap();
    await screen.findByText("Faster triage");
    await user.click(screen.getByRole("button", { name: "New release" }));
    await user.type(screen.getByLabelText("Title"), "December release");
    fireEvent.change(screen.getByLabelText("Target date (optional)"), { target: { value: "2026-12-15" } });
    await user.click(screen.getByLabelText(/CLN-1 Triage queue/));
    await user.click(screen.getByRole("button", { name: "Plan release" }));
    await waitFor(() =>
      expect(vi.mocked(api.createRelease)).toHaveBeenCalledWith(
        expect.objectContaining({
          target_date: "2026-12-15",
        }),
      ),
    );
  });

  it("sends the measure when one is entered for an initiative", async () => {
    const user = userEvent.setup();
    vi.mocked(api.roadmap).mockResolvedValue(roadmapFixture());
    vi.mocked(api.createInitiative).mockResolvedValue({ feature_id: "CLN-INI-9" });
    renderRoadmap();
    await screen.findByText("Faster triage");
    await user.click(screen.getByRole("button", { name: "New initiative" }));
    await user.type(screen.getByLabelText("Title"), "Better triage");
    await user.type(
      screen.getByLabelText(/What outcome do you want/),
      "Cut triage time in half; add a queue view.",
    );
    await user.type(screen.getByLabelText("How will you measure it? (optional)"), "median minutes");
    await user.click(screen.getByRole("button", { name: "Draft initiative" }));
    await waitFor(() =>
      expect(vi.mocked(api.createInitiative)).toHaveBeenCalledWith(
        expect.objectContaining({
          measure: "median minutes",
        }),
      ),
    );
  });

  it("hides the product selector with a single lane and defaults to it", async () => {
    vi.mocked(api.roadmap).mockResolvedValue(roadmapFixture());
    renderRoadmap("/roadmap", { ...me, lanes: [{ id: "cln", name: "Clinic" }] });
    expect(await screen.findByText("Faster triage")).toBeInTheDocument();
    expect(screen.queryByLabelText("Product")).not.toBeInTheDocument();
    expect(vi.mocked(api.roadmap)).toHaveBeenCalledWith("cln");
  });

  it("falls back to raw status/model text and handles a zero budget and one feature", async () => {
    const fixture = roadmapFixture({
      outcomes: undefined,
      initiatives: [
        {
          id: "CLN-INI-5",
          title: "Odd initiative",
          goal: "",
          horizon: "now",
          status: "weird",
          budget_usd: 0,
          lanes: ["cln"],
          release_model: "mystery",
          record: "",
          spent_usd: 0,
          features: [],
          progress: { released: 0, total: 0, planned: 0 },
          outcome: { goal_id: "goal-1", status: "off_track", why: "Off track" },
        },
        {
          id: "CLN-INI-6",
          title: "Tiny initiative",
          goal: "",
          horizon: "next",
          status: "approved",
          budget_usd: 0,
          lanes: ["cln"],
          release_model: "launch",
          record: "",
          spent_usd: 0,
          features: [
            {
              feature_id: "CLN-1",
              title: "Only feature",
              status: "in_progress",
              stage: "Build",
              initiative_id: "CLN-INI-6",
              cost_usd: 0,
              flag_on: null,
            },
          ],
          progress: { released: 0, total: 1, planned: 1 },
        },
      ],
    });
    vi.mocked(api.roadmap).mockResolvedValue(fixture);
    renderRoadmap();
    expect(await screen.findByText("Odd initiative")).toBeInTheDocument();
    expect(screen.getByText("weird")).toBeInTheDocument();
    expect(screen.getByText("mystery")).toBeInTheDocument();
    // Outcome chip only shows for approved/active initiatives.
    expect(screen.queryByText("Off track")).not.toBeInTheDocument();
    expect(screen.getByText("0 of 1 started feature live")).toBeInTheDocument();
    expect(screen.getByText("$0.000 of $0.000")).toBeInTheDocument();
    // No wanted numbers without outcomes.
    expect(screen.queryByRole("heading", { name: "Numbers to enter" })).not.toBeInTheDocument();
  });

  it("shows the no-features note when nothing is in progress", async () => {
    const user = userEvent.setup();
    const fixture = roadmapFixture({ unplanned: [] });
    fixture.initiatives[1].features = [];
    vi.mocked(api.roadmap).mockResolvedValue(fixture);
    renderRoadmap();
    await screen.findByText("Faster triage");
    await user.click(screen.getByRole("button", { name: "New release" }));
    expect(screen.getByText("No features in progress.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Plan release" })).toBeDisabled();
  });

  it("falls back on review data: no reviews, decided reviews, no review date", async () => {
    const fixture = roadmapFixture({
      initiatives: [
        {
          id: "CLN-INI-7",
          title: "No reviews at all",
          goal: "",
          horizon: "now",
          status: "approved",
          budget_usd: 10,
          lanes: ["cln"],
          release_model: "launch",
          record: "",
          spent_usd: 0,
          features: [],
          progress: { released: 0, total: 0, planned: 0 },
          review_by: "",
          reviews: undefined,
        },
        {
          id: "CLN-INI-8",
          title: "Decided review only",
          goal: "",
          horizon: "now",
          status: "approved",
          budget_usd: 10,
          lanes: ["cln"],
          release_model: "launch",
          record: "",
          spent_usd: 0,
          features: [],
          progress: { released: 0, total: 0, planned: 0 },
          review_by: "",
          reviews: [{ id: "CLN-REV-9", status: "decided", why: "done", opened: [] }],
        },
        {
          id: "CLN-INI-9",
          title: "Next review only",
          goal: "",
          horizon: "now",
          status: "approved",
          budget_usd: 10,
          lanes: ["cln"],
          release_model: "launch",
          record: "",
          spent_usd: 0,
          features: [],
          progress: { released: 0, total: 0, planned: 0 },
          review_by: "2026-12-01",
          reviews: undefined,
        },
      ],
    });
    vi.mocked(api.roadmap).mockResolvedValue(fixture);
    renderRoadmap();
    expect(await screen.findByText("No reviews at all")).toBeInTheDocument();
    expect(screen.queryByText("Review waiting")).not.toBeInTheDocument();
    expect(screen.getByText("Next review 2026-12-01")).toBeInTheDocument();
  });

  it("lets the release form uncheck, switch model and show a stage-less feature", async () => {
    const user = userEvent.setup();
    const fixture = roadmapFixture({ unplanned: [] });
    fixture.initiatives[1].features = [
      {
        feature_id: "CLN-1",
        title: "Triage queue",
        status: "in_progress",
        stage: "Build",
        initiative_id: "CLN-INI-2",
        cost_usd: 0,
        flag_on: null,
      },
      {
        feature_id: "CLN-2",
        title: "Alerts",
        status: "in_progress",
        stage: "",
        initiative_id: "CLN-INI-2",
        cost_usd: 0,
        flag_on: null,
      },
    ];
    vi.mocked(api.roadmap).mockResolvedValue(fixture);
    vi.mocked(api.createRelease).mockResolvedValue({ feature_id: "CLN-REL-9" });
    renderRoadmap();
    await screen.findByText("Faster triage");
    await user.click(screen.getByRole("button", { name: "New release" }));
    expect(screen.getByText("(starting)")).toBeInTheDocument();
    const plan = screen.getByRole("button", { name: "Plan release" });
    expect(plan).toBeDisabled();
    await user.click(screen.getByLabelText(/CLN-1 Triage queue/));
    expect(plan).not.toBeDisabled();
    await user.click(screen.getByLabelText(/CLN-1 Triage queue/));
    expect(plan).toBeDisabled();
    await user.click(screen.getByLabelText(/Release train/));
    // Back to Launch: the default radio's onChange only fires after another
    // option was picked.
    await user.click(screen.getByLabelText(/Launch group/));
    await user.type(screen.getByLabelText("Title"), "Launch release");
    await user.click(screen.getByLabelText(/CLN-2 Alerts/));
    await user.click(plan);
    await waitFor(() =>
      expect(vi.mocked(api.createRelease)).toHaveBeenCalledWith(
        expect.objectContaining({
          model: "launch",
          features: ["CLN-2"],
        }),
      ),
    );
  });

  it("cancels the new-release form", async () => {
    const user = userEvent.setup();
    vi.mocked(api.roadmap).mockResolvedValue(roadmapFixture());
    renderRoadmap();
    await screen.findByText("Faster triage");
    await user.click(screen.getByRole("button", { name: "New release" }));
    expect(screen.getByRole("heading", { name: "New release" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("heading", { name: "New release" })).not.toBeInTheDocument();
  });

  it("sends the research depth picked in the initiative form", async () => {
    const user = userEvent.setup();
    vi.mocked(api.roadmap).mockResolvedValue(roadmapFixture());
    vi.mocked(api.createInitiative).mockResolvedValue({ feature_id: "CLN-INI-9" });
    renderRoadmap();
    await screen.findByText("Faster triage");
    await user.click(screen.getByRole("button", { name: "New initiative" }));
    await user.type(screen.getByLabelText("Title"), "Better triage");
    await user.type(
      screen.getByLabelText(/What outcome do you want/),
      "Cut triage time in half; add a queue view.",
    );
    await user.click(screen.getByLabelText(/Thorough/));
    await user.click(screen.getByLabelText(/I've done my own/));
    // Back to Quick: the default radio's onChange only fires after another
    // option was picked.
    await user.click(screen.getByLabelText(/Quick:/));
    await user.click(screen.getByRole("button", { name: "Draft initiative" }));
    await waitFor(() =>
      expect(vi.mocked(api.createInitiative)).toHaveBeenCalledWith(
        expect.objectContaining({
          research: "quick",
        }),
      ),
    );
  });

  it("shows Starting for an unplanned feature with no stage", async () => {
    const fixture = roadmapFixture({
      unplanned: [
        {
          feature_id: "CLN-9",
          title: "Unplanned fix",
          status: "in_progress",
          stage: "",
          initiative_id: "",
          cost_usd: 0,
          flag_on: null,
        },
      ],
    });
    vi.mocked(api.roadmap).mockResolvedValue(fixture);
    renderRoadmap();
    expect(await screen.findByText("Unplanned fix")).toBeInTheDocument();
    expect(screen.getByText("Starting")).toBeInTheDocument();
  });

  it("falls back to raw model and status text on release cards", async () => {
    const fixture = roadmapFixture({
      releases: [
        {
          id: "CLN-REL-3",
          title: "Mystery release",
          model: "mystery",
          status: "cancelled",
          target_date: "2026-11-30",
          features: ["CLN-1"],
          record: "",
        },
        {
          id: "CLN-REL-4",
          title: "Weird release",
          model: "launch",
          status: "weird",
          target_date: "",
          features: ["CLN-2"],
          record: "",
        },
      ],
    });
    vi.mocked(api.roadmap).mockResolvedValue(fixture);
    renderRoadmap();
    expect(await screen.findByText("Mystery release")).toBeInTheDocument();
    expect(screen.getByText("mystery")).toBeInTheDocument();
    expect(screen.getByText("Cancelled")).toBeInTheDocument();
    expect(screen.getByText("weird")).toBeInTheDocument();
    // The dateless release shows no target date.
    expect(screen.getByText("CLN-REL-4: CLN-2")).toBeInTheDocument();
  });
});
