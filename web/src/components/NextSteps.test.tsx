import { beforeEach, describe, expect, it, vi } from "vitest";
import { configure, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { apiMock, resetApiMock } from "../test/apiMock";
import { FirstFeature, NextInInitiative, RecordPrs, StartFeatures } from "./NextSteps";

// Multi-step user-event flows and async fetches exceed Vitest's 5s default
// under parallel load on the shared VM; give them room.
vi.setConfig({ testTimeout: 20000 });
configure({ asyncUtilTimeout: 5000 });

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock };
});

function renderWithRoutes(ui: React.ReactElement, route = "/") {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <Routes>
        <Route path="*" element={ui} />
        <Route path="/features/:id" element={<div>Feature page</div>} />
        <Route path="/initiatives/:id" element={<div>Initiative page</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  resetApiMock();
});

describe("RecordPrs", () => {
  it("renders nothing when there are no record PRs", async () => {
    apiMock.records.mockResolvedValue([]);
    const { container } = renderWithRoutes(<RecordPrs id="CLN-PRD-1" />);
    await new Promise((r) => setTimeout(r, 50));
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when the records fetch fails", async () => {
    apiMock.records.mockRejectedValue(new Error("gone"));
    const { container } = renderWithRoutes(<RecordPrs id="CLN-PRD-1" />);
    await new Promise((r) => setTimeout(r, 50));
    expect(container).toBeEmptyDOMElement();
  });

  it("shows open and merged PRs and merges an open one", async () => {
    const user = userEvent.setup();
    apiMock.records.mockResolvedValue([
      {
        key: "org/governance#1",
        label: "Governance",
        url: "https://example.invalid/pr/1",
        merged: false,
        repo: "org/governance",
        number: 1,
      },
      {
        key: "org/registry#2",
        label: "Registry",
        url: "https://example.invalid/pr/2",
        merged: true,
        repo: "org/registry",
        number: 2,
      },
    ]);
    renderWithRoutes(<RecordPrs id="CLN-PRD-1" />);
    expect(await screen.findByText("Open")).toBeInTheDocument();
    expect(screen.getByText("Merged")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "Governance pull request #1" });
    expect(link).toHaveAttribute("href", "https://example.invalid/pr/1");
    expect(screen.getByText(/You approved it, so there's no second click on GitHub/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Merge it" }));
    expect(apiMock.mergeRecord).toHaveBeenCalledWith("CLN-PRD-1", "org/governance#1");
    expect(apiMock.records).toHaveBeenCalledTimes(2);
  });

  it("shows a merge error", async () => {
    const user = userEvent.setup();
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
    apiMock.mergeRecord.mockRejectedValue(new Error("Branch protection"));
    renderWithRoutes(<RecordPrs id="CLN-PRD-1" />);
    await user.click(await screen.findByRole("button", { name: "Merge it" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Branch protection");
  });

  it("hides the merge button when merging is not allowed", async () => {
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
    renderWithRoutes(<RecordPrs id="CLN-PRD-1" canMerge={false} />);
    await screen.findByText("Open");
    expect(screen.queryByRole("button", { name: "Merge it" })).not.toBeInTheDocument();
    expect(screen.queryByText(/You approved it/)).not.toBeInTheDocument();
  });
});

describe("StartFeatures", () => {
  const candidates = [
    { title: "Reminder texts", description: "Text patients a day ahead" },
    { title: "Online booking", description: "Book from the web" },
    { title: "Waitlist", description: "Fill cancelled slots", started: true },
  ];

  it("renders nothing when every candidate has started", () => {
    const { container } = renderWithRoutes(
      <StartFeatures id="CLN-INI-1" candidates={[{ title: "Done", description: "", started: true }]} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("starts the picked features together and navigates when one starts", async () => {
    const user = userEvent.setup();
    apiMock.startFeatures.mockResolvedValue({ started: ["CLN-1"], queued: [] });
    renderWithRoutes(<StartFeatures id="CLN-INI-1" candidates={candidates} />);
    expect(await screen.findByText("Reminder texts")).toBeInTheDocument();
    expect(screen.getByText("2 features not started yet. Pick which to start.")).toBeInTheDocument();
    // The first two open candidates are picked by default.
    await user.click(screen.getByRole("button", { name: "Start these 2 now" }));
    expect(apiMock.startFeatures).toHaveBeenCalledWith("CLN-INI-1", [0, 1], "together");
    expect(await screen.findByText("Feature page")).toBeInTheDocument();
  });

  it("starts in sequence and shows the started and queued lists", async () => {
    const user = userEvent.setup();
    apiMock.startFeatures.mockResolvedValue({ started: ["CLN-1"], queued: ["CLN-2"] });
    renderWithRoutes(<StartFeatures id="CLN-INI-1" candidates={candidates} />);
    await user.click(await screen.findByRole("button", { name: "Start in order, one at a time" }));
    expect(apiMock.startFeatures).toHaveBeenCalledWith("CLN-INI-1", [0, 1], "sequence");
    expect(await screen.findByRole("heading", { name: "Started" })).toBeInTheDocument();
    expect(screen.getByText(/Then, one at a time as each reaches everyone: CLN-2\./)).toBeInTheDocument();
  });

  it("disables start when nothing is picked and shows an error", async () => {
    const user = userEvent.setup();
    apiMock.startFeatures.mockRejectedValue(new Error("Start failed"));
    renderWithRoutes(<StartFeatures id="CLN-INI-1" candidates={candidates} />);
    await user.click(await screen.findByLabelText(/Reminder texts/));
    await user.click(screen.getByLabelText(/Online booking/));
    expect(screen.getByRole("button", { name: /Start/ })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Start in order, one at a time" })).not.toBeInTheDocument();
    await user.click(screen.getByLabelText(/Reminder texts/));
    await user.click(screen.getByRole("button", { name: "Start it now" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Start failed");
  });
});

describe("FirstFeature", () => {
  it("starts the first feature and navigates", async () => {
    const user = userEvent.setup();
    apiMock.firstFeature.mockResolvedValue({ feature_id: "CLN-1" });
    renderWithRoutes(<FirstFeature id="CLN-PRD-1" />);
    await user.click(screen.getByRole("button", { name: "Start the first feature" }));
    expect(apiMock.firstFeature).toHaveBeenCalledWith("CLN-PRD-1");
    expect(await screen.findByText("Feature page")).toBeInTheDocument();
  });

  it("shows an error when the first feature cannot start", async () => {
    const user = userEvent.setup();
    apiMock.firstFeature.mockRejectedValue(new Error("Roadmap not ready"));
    renderWithRoutes(<FirstFeature id="CLN-PRD-1" />);
    await user.click(screen.getByRole("button", { name: "Start the first feature" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Roadmap not ready");
  });
});

describe("NextInInitiative", () => {
  function initiativeView(
    status: string,
    candidates: { title: string; description: string; started?: boolean }[],
  ) {
    return {
      feature_id: "CLN-INI-1",
      kind: "initiative" as const,
      product_id: "CLN",
      title: "Cut no-shows",
      created_by: "ada",
      status,
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
      candidates,
      budget_usd: 0,
      spent_usd: 0,
      progress: { released: 0, total: 0 },
    };
  }

  it("renders nothing when there is no next candidate or the initiative is not approved", async () => {
    apiMock.initiative.mockResolvedValue(initiativeView("drafting", [{ title: "A", description: "" }]));
    const { container } = renderWithRoutes(<NextInInitiative initiativeId="CLN-INI-1" />);
    await new Promise((r) => setTimeout(r, 50));
    expect(container).toBeEmptyDOMElement();

    apiMock.initiative.mockResolvedValue(
      initiativeView("approved", [{ title: "A", description: "", started: true }]),
    );
    const second = renderWithRoutes(<NextInInitiative initiativeId="CLN-INI-1" />);
    await new Promise((r) => setTimeout(r, 50));
    expect(second.container).toBeEmptyDOMElement();
  });

  it("renders nothing when the initiative fails to load", async () => {
    apiMock.initiative.mockRejectedValue(new Error("gone"));
    const { container } = renderWithRoutes(<NextInInitiative initiativeId="CLN-INI-1" />);
    await new Promise((r) => setTimeout(r, 50));
    expect(container).toBeEmptyDOMElement();
  });

  it("starts the next feature", async () => {
    const user = userEvent.setup();
    apiMock.initiative.mockResolvedValue(
      initiativeView("approved", [
        { title: "First", description: "", started: true },
        { title: "Second", description: "" },
      ]),
    );
    apiMock.startFeatures.mockResolvedValue({ started: ["CLN-2"], queued: [] });
    renderWithRoutes(<NextInInitiative initiativeId="CLN-INI-1" />);
    expect(await screen.findByText("Second")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "CLN-INI-1" })).toHaveAttribute("href", "/initiatives/CLN-INI-1");
    await user.click(screen.getByRole("button", { name: "Start it" }));
    expect(apiMock.startFeatures).toHaveBeenCalledWith("CLN-INI-1", [1], "together");
    expect(await screen.findByText("Feature page")).toBeInTheDocument();
  });

  it("shows a start error", async () => {
    const user = userEvent.setup();
    apiMock.initiative.mockResolvedValue(initiativeView("approved", [{ title: "Second", description: "" }]));
    apiMock.startFeatures.mockRejectedValue(new Error("Cannot start"));
    renderWithRoutes(<NextInInitiative initiativeId="CLN-INI-1" />);
    await user.click(await screen.findByRole("button", { name: "Start it" }));
    expect(await screen.findByText("Cannot start")).toBeInTheDocument();
  });
});
