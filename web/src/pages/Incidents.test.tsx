import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { api, ApiError, type IncidentRow, type IncidentView, type Me, type PendingItem } from "../api";
import { IncidentPage, IncidentsPage } from "./Incidents";

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

function row(overrides: Record<string, unknown> = {}): IncidentRow {
  return {
    feature_id: "CLN-INC-1",
    product_id: "cln",
    title: "Triage is down",
    severity: "sev1",
    status: "triaging",
    component: "services/triage",
    opened_at: "2026-11-10T10:00:00Z",
    firing: 2,
    links: { "CLN-1": "hotfix" },
    ...overrides,
  } as IncidentRow;
}

function pending(overrides: Partial<PendingItem> = {}): PendingItem {
  return {
    interrupt_id: "int-1",
    type: "gate",
    gate: "incident",
    approvers: ["oncall"],
    can_decide: false,
    ...overrides,
  } as PendingItem;
}

function incidentFixture(overrides: Record<string, unknown> = {}): IncidentView {
  return {
    feature_id: "CLN-INC-1",
    kind: "incident",
    product_id: "cln",
    title: "Triage is down",
    created_by: "sentry",
    status: "mitigating",
    running: false,
    error: null,
    pending: [],
    events: [],
    cost_usd: 0,
    record_pr: { url: "https://example.com/pr/9" },
    severity: "sev1",
    component: "services/triage",
    opened_at: "2026-11-10T10:00:00Z",
    triage: "# Triage\n\nTriage notes here.",
    actions: [],
    taken: [
      {
        id: "a1",
        type: "flag_off",
        text: "Turned the flag off",
        ok: true,
        result: "Flag off",
        by: "sam",
        at: "2026-11-10T10:30:00Z",
      },
      {
        id: "a2",
        type: "rollback",
        text: "Rolled back",
        ok: false,
        result: "Rollback failed",
        by: "agent:remediate",
        at: "2026-11-10T10:40:00Z",
      },
    ],
    links: { "CLN-5": "hotfix" },
    postmortem: "",
    signals: [
      {
        source: "sentry",
        status: "firing",
        severity: "sev1",
        title: "Error spike",
        detail: "",
        fingerprint: "fp-1",
        url: "https://example.com/signal/1",
        at: "2026-11-10T10:00:00Z",
        component: "triage",
        labels: { joined_by: "same trace" },
      },
      {
        source: "pagerduty",
        status: "resolved",
        severity: "sev2",
        title: "Old alert",
        detail: "",
        fingerprint: "fp-2",
        url: "",
        at: "2026-11-10T09:00:00Z",
        component: "triage",
      },
    ],
    firing: 2,
    suspects: [],
    root: { component: "triage", why: "bad deploy" },
    paging: [
      { at: "2026-11-10T10:00:00Z", action: "urgent", ok: true, detail: "", dedup_key: "d1" },
      { at: "2026-11-10T10:05:00Z", action: "acknowledge", ok: true, detail: "", dedup_key: "d1" },
    ],
    analysis: {
      kind: "rca",
      status: "completed",
      report: {
        summary: "Root cause found.",
        result: {
          type: "root_cause",
          root_causes: [{ summary: "Bad config", confidence: "high", analysis: "Config changed" }],
          excluded_causes: [{ description: "Not the database" }],
          recommendations: { recommended_actions: [{ description: "Revert the config" }] },
        },
        overprovisioning: {
          analysis: "Overprovisioned.",
          recommendation: {
            cpu_request: "100m",
            cpu_limit: "200m",
            memory_request: "256Mi",
            memory_limit: "512Mi",
            estimated_savings: 42,
            currency: "USD",
            rationale: "Usage is low",
          },
        },
      },
    },
    ...overrides,
  } as unknown as IncidentView;
}

function renderList(meOverride: Me = me) {
  return render(
    <MemoryRouter initialEntries={["/incidents"]}>
      <Routes>
        <Route path="/incidents" element={<IncidentsPage me={meOverride} />} />
        <Route path="/incidents/:id" element={<div>incident detail page</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

function renderDetail(path = "/incidents/CLN-INC-1") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/incidents/CLN-INC-5" element={<div>new incident page</div>} />
        <Route path="/incidents/:id" element={<IncidentPage />} />
        <Route path="/features/:id" element={<div>feature page</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  defaultRejectAll();
  vi.mocked(api.unmatchedSignals).mockResolvedValue({ count: 0, signals: [] });
});

afterEach(() => cleanup());

describe("IncidentsPage", () => {
  it("renders open and closed sections with severity and status", async () => {
    vi.mocked(api.incidents).mockResolvedValue([
      row(),
      row({
        feature_id: "CLN-INC-2",
        title: "Minor glitch",
        severity: "sev4",
        status: "closed",
        firing: 0,
        links: {},
      }),
      row({
        feature_id: "CLN-INC-3",
        title: "Slow page",
        severity: "sev3",
        status: "monitoring",
        component: "",
        firing: 0,
        links: {},
      }),
    ]);
    renderList();
    expect(await screen.findByText("Triage is down")).toBeInTheDocument();
    expect(screen.getByText("SEV1")).toBeInTheDocument();
    expect(screen.getByText("SEV4")).toBeInTheDocument();
    expect(screen.getByText("SEV3")).toBeInTheDocument();
    expect(screen.getByText("Triage in progress")).toBeInTheDocument();
    // Status chip on the closed row + the Closed section heading.
    expect(screen.getAllByText("Closed")).toHaveLength(2);
    expect(screen.getByText("Watching for recovery")).toBeInTheDocument();
    expect(screen.getByText(/CLN-INC-1, triage, 2 still firing, started CLN-1/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Closed" })).toBeInTheDocument();
    expect(screen.getByText("Minor glitch")).toBeInTheDocument();
  });

  it("shows the empty-open message and an error alert", async () => {
    vi.mocked(api.incidents).mockResolvedValue([]);
    renderList();
    expect(await screen.findByText("No open incidents.")).toBeInTheDocument();

    vi.mocked(api.incidents).mockRejectedValue(new Error("incidents down"));
    const second = renderList();
    expect(await screen.findByRole("alert")).toHaveTextContent("incidents down");
    second.unmount();
  });

  it("shows unmatched signals with count, link and reason", async () => {
    vi.mocked(api.incidents).mockResolvedValue([row()]);
    vi.mocked(api.unmatchedSignals).mockResolvedValue({
      count: 1,
      signals: [
        {
          source: "sentry",
          title: "Mystery alert",
          fingerprint: "fp-x",
          url: "https://example.com/alert",
          reason: "no product matched",
          received_at: "2026-11-10T08:00:00Z",
        },
      ],
    });
    renderList();
    expect(await screen.findByText("Signals that matched no product (1)")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Mystery alert" })).toHaveAttribute(
      "href",
      "https://example.com/alert",
    );
    expect(screen.getByText(/Didn't match because no product matched/)).toBeInTheDocument();
  });

  it("hides the unmatched section when the call fails (403) or is empty", async () => {
    vi.mocked(api.incidents).mockResolvedValue([row()]);
    vi.mocked(api.unmatchedSignals).mockRejectedValue(new Error("forbidden"));
    renderList();
    await screen.findByText("Triage is down");
    expect(screen.queryByText(/Signals that matched no product/)).not.toBeInTheDocument();
  });

  it("reports a problem and navigates to the new incident", async () => {
    const user = userEvent.setup();
    vi.mocked(api.incidents).mockResolvedValue([row()]);
    vi.mocked(api.reportProblem).mockResolvedValue({ feature_id: "CLN-INC-9" });
    renderList();
    await screen.findByText("Triage is down");
    await user.click(screen.getByRole("button", { name: "Report a problem" }));
    await user.type(screen.getByLabelText("What's wrong"), "Login fails");
    await user.type(
      screen.getByLabelText("What happened, and what did you expect?"),
      "Users cannot log in at all.",
    );
    await user.selectOptions(screen.getByLabelText("How bad is it"), "sev1");
    await user.click(screen.getByRole("button", { name: "Report problem" }));
    await waitFor(() =>
      expect(vi.mocked(api.reportProblem)).toHaveBeenCalledWith({
        product_id: "cln",
        title: "Login fails",
        description: "Users cannot log in at all.",
        severity: "sev1",
      }),
    );
    expect(await screen.findByText("incident detail page")).toBeInTheDocument();
  });

  it("offers the product picker when the user has several lanes", async () => {
    const user = userEvent.setup();
    vi.mocked(api.incidents).mockResolvedValue([row()]);
    vi.mocked(api.reportProblem).mockResolvedValue({ feature_id: "CLN-INC-9" });
    renderList();
    await screen.findByText("Triage is down");
    await user.click(screen.getByRole("button", { name: "Report a problem" }));
    await user.type(screen.getByLabelText("What's wrong"), "Login fails");
    await user.selectOptions(screen.getByLabelText("Product"), "ops");
    await user.click(screen.getByRole("button", { name: "Report problem" }));
    await waitFor(() =>
      expect(vi.mocked(api.reportProblem)).toHaveBeenCalledWith(
        expect.objectContaining({ product_id: "ops" }),
      ),
    );
  });

  it("shows a report error and cancels the form", async () => {
    const user = userEvent.setup();
    vi.mocked(api.incidents).mockResolvedValue([row()]);
    vi.mocked(api.reportProblem).mockRejectedValue(new Error("title too short"));
    renderList();
    await screen.findByText("Triage is down");
    await user.click(screen.getByRole("button", { name: "Report a problem" }));
    await user.type(screen.getByLabelText("What's wrong"), "Login fails");
    await user.click(screen.getByRole("button", { name: "Report problem" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("title too short");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("heading", { name: "Report a problem" })).not.toBeInTheDocument();
  });
});

describe("IncidentPage", () => {
  it("shows Loading… then the incident header, actions, signals and triage", async () => {
    vi.mocked(api.incident).mockResolvedValue(incidentFixture());
    renderDetail();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Triage is down" })).toBeInTheDocument();
    expect(screen.getByText("SEV1")).toBeInTheDocument();
    expect(screen.getByText("Acting")).toBeInTheDocument();
    expect(screen.getByText("Starts in triage")).toBeInTheDocument();
    expect(screen.getByText("2 still firing")).toBeInTheDocument();
    expect(screen.getByText("Page acknowledged")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "this pull request" })).toHaveAttribute(
      "href",
      "https://example.com/pr/9",
    );
    // Taken actions
    expect(screen.getByText("Turn the feature's flag off")).toBeInTheDocument();
    expect(screen.getByText("Done")).toBeInTheDocument();
    expect(screen.getByText("Roll back to the previous release")).toBeInTheDocument();
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(screen.getByText(/Flag off, approved by sam/)).toBeInTheDocument();
    expect(screen.getByText(/Rollback failed, by the tool \(remediation policy\)/)).toBeInTheDocument();
    // Links + signals
    expect(screen.getByText("Started from this incident")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "CLN-5" })).toBeInTheDocument();
    expect(screen.getByText("Error spike")).toBeInTheDocument();
    expect(screen.getByText("Old alert")).toBeInTheDocument();
    expect(screen.getByText("Not the same failure")).toBeInTheDocument();
    // Triage (no pending decision, no postmortem): the section heading and
    // the "# Triage" heading inside the markdown both read "Triage".
    expect(screen.getAllByRole("heading", { name: "Triage" })).toHaveLength(2);
    expect(screen.getByText("Triage notes here.")).toBeInTheDocument();
  });

  it("shows the 404 message and a generic error", async () => {
    vi.mocked(api.incident).mockRejectedValue(new ApiError(404, "gone"));
    renderDetail("/incidents/CLN-INC-9");
    expect(await screen.findByRole("alert")).toHaveTextContent("This incident doesn't exist.");
  });

  it("shows the workflow error banner and the page-failure banner", async () => {
    vi.mocked(api.incident).mockResolvedValue(
      incidentFixture({
        error: "agent crashed",
        paging: [
          {
            at: "2026-11-10T10:00:00Z",
            action: "urgent",
            ok: false,
            detail: "not paged: provider down",
            dedup_key: "d1",
          },
        ],
      }),
    );
    renderDetail();
    expect(await screen.findByText("The workflow stopped with an error: agent crashed")).toBeInTheDocument();
    expect(screen.getByText("On-call wasn't paged: provider down")).toBeInTheDocument();
  });

  it("shows the waiting note when someone else must decide", async () => {
    vi.mocked(api.incident).mockResolvedValue(incidentFixture({ pending: [pending()] }));
    renderDetail();
    expect(await screen.findByText("Waiting for On-call decision: oncall.")).toBeInTheDocument();
  });

  it("renders the decision panel when the user can decide and hides triage", async () => {
    const user = userEvent.setup();
    vi.mocked(api.incident).mockResolvedValue(
      incidentFixture({
        pending: [
          pending({
            can_decide: true,
            actions: [{ id: "a1", type: "flag_off", text: "Turn the flag off" }],
          }),
        ],
      }),
    );
    vi.mocked(api.decide).mockResolvedValue({ accepted: true });
    renderDetail();
    expect(
      await screen.findByRole("heading", { name: "What should we do about this incident?" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Triage" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Approve 1 action" }));
    await waitFor(() =>
      expect(vi.mocked(api.decide)).toHaveBeenCalledWith(
        "CLN-INC-1",
        "int-1",
        expect.objectContaining({ decision: "approve", action_ids: ["a1"] }),
      ),
    );
  });

  it("merges into another incident and reloads", async () => {
    const user = userEvent.setup();
    vi.mocked(api.incident).mockResolvedValue(incidentFixture());
    vi.mocked(api.mergeIncident).mockResolvedValue({ merged: "CLN-INC-1", into: "CLN-INC-4" });
    renderDetail();
    await screen.findByText("Triage is down");
    await user.type(screen.getByLabelText("Same failure as incident"), "CLN-INC-4");
    await user.click(screen.getByRole("button", { name: "Merge into it" }));
    await waitFor(() =>
      expect(vi.mocked(api.mergeIncident)).toHaveBeenCalledWith("CLN-INC-1", "CLN-INC-4", "Same failure"),
    );
    expect(vi.mocked(api.incident).mock.calls.length).toBeGreaterThan(1);
  });

  it("shows a merge error", async () => {
    const user = userEvent.setup();
    vi.mocked(api.incident).mockResolvedValue(incidentFixture());
    vi.mocked(api.mergeIncident).mockRejectedValue(new Error("no such incident"));
    renderDetail();
    await screen.findByText("Triage is down");
    await user.type(screen.getByLabelText("Same failure as incident"), "CLN-INC-4");
    await user.click(screen.getByRole("button", { name: "Merge into it" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("no such incident");
  });

  it("splits a signal into a new incident and navigates", async () => {
    const user = userEvent.setup();
    vi.mocked(api.incident).mockResolvedValue(incidentFixture());
    vi.mocked(api.splitSignal).mockResolvedValue({ new_incident: "CLN-INC-5" });
    renderDetail();
    await screen.findByText("Triage is down");
    await user.click(screen.getByRole("button", { name: "Not the same failure" }));
    await waitFor(() => expect(vi.mocked(api.splitSignal)).toHaveBeenCalledWith("CLN-INC-1", "fp-1"));
    expect(await screen.findByText("new incident page")).toBeInTheDocument();
  });

  it("reloads when a split returns no new incident", async () => {
    const user = userEvent.setup();
    vi.mocked(api.incident).mockResolvedValue(incidentFixture());
    vi.mocked(api.splitSignal).mockResolvedValue({ new_incident: null });
    renderDetail();
    await screen.findByText("Triage is down");
    await user.click(screen.getByRole("button", { name: "Not the same failure" }));
    await waitFor(() => expect(vi.mocked(api.incident).mock.calls.length).toBeGreaterThan(1));
  });

  it("shows the postmortem heading when a postmortem exists", async () => {
    vi.mocked(api.incident).mockResolvedValue(
      incidentFixture({ postmortem: "# Postmortem\n\nWhat happened." }),
    );
    renderDetail();
    expect(await screen.findByText("What happened.")).toBeInTheDocument();
    // Section heading + the "# Postmortem" heading inside the markdown.
    expect(screen.getAllByRole("heading", { name: "Postmortem" })).toHaveLength(2);
  });

  it("renders the analysis panel states", async () => {
    const cases: Array<[string, string]> = [
      ["pending", "Investigating. The tool waits a few minutes for it before acting."],
      ["skipped", "Not run: no signals"],
      ["failed", "No report: timed out."],
    ];
    for (const [status, text] of cases) {
      vi.mocked(api.incident).mockResolvedValue(
        incidentFixture({
          analysis: {
            kind: "rca",
            status,
            why: status === "skipped" ? "no signals" : status === "failed" ? "timed out" : undefined,
            report: null,
          },
        }),
      );
      const r = renderDetail();
      expect(await screen.findByText(text)).toBeInTheDocument();
      r.unmount();
    }
  });

  it("renders a completed analysis with root causes, exclusions and right-sizing", async () => {
    vi.mocked(api.incident).mockResolvedValue(incidentFixture());
    renderDetail();
    expect(await screen.findByText("OpenChoreo's root-cause analysis")).toBeInTheDocument();
    expect(screen.getByText("Root cause found.")).toBeInTheDocument();
    expect(screen.getByText("high confidence")).toBeInTheDocument();
    expect(screen.getByText("Bad config")).toBeInTheDocument();
    expect(screen.getByText(/Ruled out: Not the database/)).toBeInTheDocument();
    expect(screen.getByText(/It recommends: Revert the config/)).toBeInTheDocument();
    expect(
      screen.getByText(/Right-size to CPU 100m\/200m, memory 256Mi\/512Mi: saves about 42 USD/),
    ).toBeInTheDocument();
  });

  it("renders a cost analysis with no root cause identified", async () => {
    vi.mocked(api.incident).mockResolvedValue(
      incidentFixture({
        analysis: {
          kind: "cost",
          status: "completed",
          report: {
            summary: "Too big.",
            result: { type: "no_root_cause_identified", explanation: "Nothing conclusive" },
          },
        },
      }),
    );
    renderDetail();
    expect(await screen.findByText("OpenChoreo's FinOps agent")).toBeInTheDocument();
    expect(screen.getByText("No root cause found: Nothing conclusive")).toBeInTheDocument();
  });

  it("shows the nothing-yet note when no actions were taken", async () => {
    vi.mocked(api.incident).mockResolvedValue(incidentFixture({ taken: [], links: {} }));
    renderDetail();
    expect(await screen.findByText("Nothing yet.")).toBeInTheDocument();
  });

  it("shows the component when there is no root cause location, and no record link", async () => {
    vi.mocked(api.incident).mockResolvedValue(incidentFixture({ root: undefined, record_pr: {} }));
    renderDetail();
    expect(await screen.findByText("Triage is down")).toBeInTheDocument();
    expect(screen.getByText("services/triage")).toBeInTheDocument();
    expect(screen.queryByText("Starts in triage")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "this pull request" })).not.toBeInTheDocument();
  });

  it("reports a failed acknowledge page without the paged state", async () => {
    vi.mocked(api.incident).mockResolvedValue(
      incidentFixture({
        paging: [
          {
            at: "2026-11-10T10:00:00Z",
            action: "acknowledge",
            ok: false,
            detail: "timeout",
            dedup_key: "d1",
          },
        ],
      }),
    );
    renderDetail();
    expect(await screen.findByText("PagerDuty didn't take the acknowledge: timeout")).toBeInTheDocument();
    expect(screen.queryByText("Page acknowledged")).not.toBeInTheDocument();
  });

  it("hides the merge form for a closed incident and falls back to the raw status", async () => {
    vi.mocked(api.incident).mockResolvedValue(incidentFixture({ status: "closed", analysis: undefined }));
    renderDetail();
    expect(await screen.findByText("Triage is down")).toBeInTheDocument();
    expect(screen.getByText("Closed")).toBeInTheDocument();
    expect(screen.queryByLabelText("Same failure as incident")).not.toBeInTheDocument();
    expect(screen.queryByText("OpenChoreo's root-cause analysis")).not.toBeInTheDocument();
  });

  it("shows an unmatched signal without a link as plain text", async () => {
    vi.mocked(api.incidents).mockResolvedValue([row()]);
    vi.mocked(api.unmatchedSignals).mockResolvedValue({
      count: 1,
      signals: [
        {
          source: "sentry",
          title: "Mystery alert",
          fingerprint: "fp-x",
          reason: "no product matched",
          received_at: "2026-11-10T08:00:00Z",
        },
      ],
    });
    renderList();
    expect(await screen.findByText("Mystery alert")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Mystery alert" })).not.toBeInTheDocument();
  });

  it("renders a SEV2 row in the list", async () => {
    vi.mocked(api.incidents).mockResolvedValue([
      row({
        feature_id: "CLN-INC-8",
        title: "Degraded search",
        severity: "sev2",
        status: "awaiting_decision",
        firing: 0,
        links: {},
      }),
    ]);
    renderList();
    expect(await screen.findByText("Degraded search")).toBeInTheDocument();
    expect(screen.getByText("SEV2")).toBeInTheDocument();
    expect(screen.getByText("Waiting for on-call")).toBeInTheDocument();
  });

  it("renders a SEV4 row with no component or links", async () => {
    vi.mocked(api.incidents).mockResolvedValue([
      row({
        feature_id: "CLN-INC-9",
        title: "Minor blip",
        severity: "sev4",
        status: "resolved",
        firing: 0,
        component: "",
        links: undefined,
      }),
    ]);
    renderList();
    expect(await screen.findByText("Minor blip")).toBeInTheDocument();
    expect(screen.getByText("SEV4")).toBeInTheDocument();
    expect(screen.getByText("CLN-INC-9")).toBeInTheDocument();
  });

  it("reports with no product when the user has no lanes", async () => {
    const user = userEvent.setup();
    vi.mocked(api.incidents).mockResolvedValue([row()]);
    vi.mocked(api.reportProblem).mockResolvedValue({ feature_id: "CLN-INC-9" });
    renderList({ ...me, lanes: [] });
    await screen.findByText("Triage is down");
    await user.click(screen.getByRole("button", { name: "Report a problem" }));
    expect(screen.queryByLabelText("Product")).not.toBeInTheDocument();
    await user.type(screen.getByLabelText("What's wrong"), "Login fails");
    await user.type(
      screen.getByLabelText("What happened, and what did you expect?"),
      "Users cannot log in at all.",
    );
    await user.click(screen.getByRole("button", { name: "Report problem" }));
    await waitFor(() =>
      expect(vi.mocked(api.reportProblem)).toHaveBeenCalledWith(
        expect.objectContaining({
          product_id: "",
        }),
      ),
    );
  });

  it("shows a report error in the form", async () => {
    const user = userEvent.setup();
    vi.mocked(api.incidents).mockResolvedValue([row()]);
    vi.mocked(api.reportProblem).mockRejectedValue(new Error("report failed"));
    renderList();
    await screen.findByText("Triage is down");
    await user.click(screen.getByRole("button", { name: "Report a problem" }));
    await user.type(screen.getByLabelText("What's wrong"), "Login fails");
    await user.type(
      screen.getByLabelText("What happened, and what did you expect?"),
      "Users cannot log in at all.",
    );
    await user.click(screen.getByRole("button", { name: "Report problem" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("report failed");
    expect(screen.getByRole("button", { name: "Report problem" })).not.toBeDisabled();
  });

  it("shows a plain load error instead of the 404 message", async () => {
    vi.mocked(api.incident).mockRejectedValue(new Error("boom"));
    renderDetail("/incidents/CLN-INC-9");
    expect(await screen.findByRole("alert")).toHaveTextContent("boom");
  });

  it("falls back to raw status with no root, paging, pending or triage text", async () => {
    vi.mocked(api.incident).mockResolvedValue(
      incidentFixture({
        status: "weird",
        root: undefined,
        component: "",
        firing: 0,
        paging: undefined,
        pending: [],
        postmortem: "",
        triage: "",
        signals: [
          {
            source: "sentry",
            status: "firing",
            severity: "sev1",
            title: "Lonely alert",
            detail: "",
            fingerprint: "fp-9",
            url: "",
            at: "2026-11-10T08:00:00Z",
            component: "",
          },
        ],
        analysis: {
          kind: "rca",
          status: "completed",
          report: {
            summary: "",
            result: { type: "root_cause", root_causes: undefined },
          },
        },
      }),
    );
    renderDetail();
    expect(await screen.findByText("Triage is down")).toBeInTheDocument();
    expect(screen.getByText("weird")).toBeInTheDocument();
    expect(screen.queryByText("Starts in triage")).not.toBeInTheDocument();
    expect(screen.queryByText("still firing")).not.toBeInTheDocument();
    expect(screen.queryByText("Waiting for on-call")).not.toBeInTheDocument();
    expect(screen.getByText("Triage in progress.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "OpenChoreo's root-cause analysis" })).toBeInTheDocument();
    expect(screen.queryByText("Root cause found.")).not.toBeInTheDocument();
  });

  it("shows a timed-out analysis with no root cause and an unknown confidence", async () => {
    vi.mocked(api.incident).mockResolvedValue(
      incidentFixture({
        analysis: {
          kind: "rca",
          status: "timed_out",
          why: "",
          report: {
            summary: "Looked, found nothing.",
            result: {
              type: "no_root_cause_identified",
              explanation: "signals were too noisy",
              root_causes: [{ summary: "Guess", confidence: "weird", analysis: "" }],
            },
          },
        },
      }),
    );
    renderDetail();
    expect(await screen.findByText("No report: timed_out.")).toBeInTheDocument();
    expect(screen.getByText("No root cause found: signals were too noisy")).toBeInTheDocument();
    expect(screen.getByText("weird confidence")).toBeInTheDocument();
  });

  it("shows a split error on the detail page", async () => {
    const user = userEvent.setup();
    vi.mocked(api.incident).mockResolvedValue(incidentFixture());
    vi.mocked(api.splitSignal).mockRejectedValue(new Error("split failed"));
    renderDetail();
    await screen.findByText("Triage is down");
    await user.click(screen.getByRole("button", { name: "Not the same failure" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("split failed");
  });

  it("shows the external waiting label when only an external approver is pending", async () => {
    vi.mocked(api.incident).mockResolvedValue(
      incidentFixture({
        pending: [
          pending({ type: "external", label: "Waiting on PagerDuty", can_decide: false, approvers: [] }),
        ],
      }),
    );
    renderDetail();
    expect(await screen.findByText("Waiting on PagerDuty.")).toBeInTheDocument();
  });

  it("falls back to the raw severity text for an unknown severity", async () => {
    vi.mocked(api.incidents).mockResolvedValue([
      row({ feature_id: "CLN-INC-10", title: "Odd one", severity: "sev9", status: "triaging", firing: 0 }),
    ]);
    renderList();
    expect(await screen.findByText("Odd one")).toBeInTheDocument();
    expect(screen.getByText("sev9")).toBeInTheDocument();
  });

  it("shows an unknown action type, no approver and a root cause with no reason", async () => {
    vi.mocked(api.incident).mockResolvedValue(
      incidentFixture({
        root: { component: "triage", why: "" },
        taken: [
          {
            id: "a9",
            type: "weird_action",
            text: "Did a weird thing",
            ok: true,
            result: "It worked",
            by: "",
            at: "2026-11-10T10:30:00Z",
          },
        ],
      }),
    );
    renderDetail();
    expect(await screen.findByText("weird_action")).toBeInTheDocument();
    expect(screen.getByText("It worked")).toBeInTheDocument();
    expect(screen.getByText("Starts in triage")).toBeInTheDocument();
  });
});
