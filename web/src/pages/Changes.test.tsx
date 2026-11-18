import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { api, type ChangeRecord, type Lookback, type Me } from "../api";
import { ChangesPage } from "./Changes";

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

function changeFixture(overrides: Record<string, unknown> = {}): ChangeRecord {
  return {
    id: "chg-1",
    at: "2026-11-10T10:00:00Z",
    product_id: "cln",
    env: "production",
    kind: "release",
    type: "standard",
    action: "deploy",
    summary: "Released triage queue",
    targets: [{ kind: "component", name: "triage", release: "1.2.0", on: true, percent: 50 }],
    feature_id: "CLN-1",
    release_id: "CLN-REL-1",
    incident_id: "",
    requested_by: "ada",
    approved_by: ["sam"],
    approvals: [{ gate: "g6", lane: "cln", approver: "sam", at: "2026-11-10T09:00:00Z" }],
    evidence: {
      pull_requests: ["https://github.com/org/repo/pull/42"],
      quality: { tests: {} },
      triage: "Low risk release",
    },
    outcome: "done",
    detail: "",
    caused_incidents: ["CLN-INC-1"],
    hash: "aabbccdd11223344",
    prev_hash: "99887766",
    receipt: { status: "closed", number: "INC0001", detail: "linked" },
    ...overrides,
  } as unknown as ChangeRecord;
}

function lookbackFixture(overrides: Record<string, unknown> = {}): Lookback {
  return {
    product_id: "cln",
    months: [
      {
        month: "2026-11",
        incidents: 2,
        by_severity: { sev1: 1 },
        components: {},
        fixed_by_tool: 1,
        escalated: 1,
        open: 0,
        changes: {},
        failed_changes: 0,
        releases: 3,
        releases_causing_incidents: 1,
        change_failure_rate: 0.25,
        recover_minutes_median: 120,
        recover_minutes_max: 300,
      },
      {
        month: "2026-10",
        incidents: 0,
        by_severity: {},
        components: {},
        fixed_by_tool: 0,
        escalated: 0,
        open: 0,
        changes: {},
        failed_changes: 0,
        releases: 1,
        releases_causing_incidents: 0,
        change_failure_rate: null,
        recover_minutes_median: null,
        recover_minutes_max: null,
      },
    ],
    total: {
      incidents: 2,
      fixed_by_tool: 1,
      escalated: 1,
      releases: 4,
      releases_causing_incidents: 1,
      failed_changes: 0,
      change_failure_rate: 0.25,
    },
    causes: [
      {
        change_id: "chg-1",
        summary: "Released triage queue",
        at: "2026-11-10T10:00:00Z",
        incidents: ["CLN-INC-1"],
      },
    ],
    ...overrides,
  } as unknown as Lookback;
}

function mockLoaded(
  opts: {
    rows?: ChangeRecord[];
    lookback?: Lookback;
    chain?: { ok: boolean; records: number; broken_at: string | null };
  } = {},
) {
  vi.mocked(api.changes).mockResolvedValue(opts.rows ?? [changeFixture()]);
  vi.mocked(api.lookback).mockResolvedValue(opts.lookback ?? lookbackFixture());
  vi.mocked(api.verifyChanges).mockResolvedValue(opts.chain ?? { ok: true, records: 12, broken_at: null });
}

function renderChanges(initial = "/changes?product=cln", meOverride: Me = me) {
  return render(
    <MemoryRouter initialEntries={[initial]}>
      <ChangesPage me={meOverride} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  defaultRejectAll();
});

afterEach(() => cleanup());

describe("ChangesPage", () => {
  it("shows the no-lane message when the user has no lanes", () => {
    renderChanges("/changes", { ...me, lanes: [] });
    expect(screen.getByText("You aren't a member of any product's organization.")).toBeInTheDocument();
    expect(vi.mocked(api.changes)).not.toHaveBeenCalled();
  });

  it("renders the change log rows with type, action and approver", async () => {
    mockLoaded();
    renderChanges();
    expect(
      await screen.findByRole("button", { name: /Released: Released triage queue/ }),
    ).toBeInTheDocument();
    expect(screen.getByText("Standard")).toBeInTheDocument();
    expect(screen.getByText("sam")).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "CLN-INC-1" })).toHaveLength(2);
    expect(screen.getByText("12 records, chain intact")).toBeInTheDocument();
    expect(screen.getByText("chg-1, INC0001")).toBeInTheDocument();
  });

  it("shows an error alert when the load fails", async () => {
    vi.mocked(api.changes).mockRejectedValue(new Error("changes down"));
    vi.mocked(api.lookback).mockResolvedValue(lookbackFixture());
    vi.mocked(api.verifyChanges).mockResolvedValue({ ok: true, records: 0, broken_at: null });
    renderChanges();
    expect(await screen.findByRole("alert")).toHaveTextContent("changes down");
  });

  it("shows the empty change log message", async () => {
    mockLoaded({ rows: [] });
    renderChanges();
    expect(await screen.findByText("No production changes yet.")).toBeInTheDocument();
  });

  it("renders the lookback table values, including nulls as dashes", async () => {
    mockLoaded();
    renderChanges();
    const section = await screen.findByRole("heading", { name: "The last six months" });
    const table = section.closest("section")!;
    expect(within(table).getByText("2026-11")).toBeInTheDocument();
    expect(within(table).getByText("2026-10")).toBeInTheDocument();
    expect(within(table).getByText("(1 SEV1)")).toBeInTheDocument();
    expect(within(table).getByText("2.0 h")).toBeInTheDocument();
    expect(within(table).getByText("(25%)")).toBeInTheDocument();
    expect(within(table).getByText("Released triage queue")).toBeInTheDocument();
    expect(within(table).getByText("Releases behind the most incidents")).toBeInTheDocument();
  });

  it("renders a short median recovery in minutes", async () => {
    const lb = lookbackFixture();
    lb.months[0].recover_minutes_median = 45;
    mockLoaded({ lookback: lb });
    renderChanges();
    expect(await screen.findByText("45 min")).toBeInTheDocument();
  });

  it("warns when the change chain is broken", async () => {
    mockLoaded({ chain: { ok: false, records: 12, broken_at: "2026-10-02T00:00:00Z" } });
    renderChanges();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The change log was altered: its records no longer chain together from 2026-10-02T00:00:00Z.",
    );
    expect(screen.queryByText(/chain intact/)).not.toBeInTheDocument();
  });

  it("expands a row to show its detail and collapses it again", async () => {
    const user = userEvent.setup();
    mockLoaded();
    renderChanges();
    const toggle = await screen.findByRole("button", { name: /Released: Released triage queue/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("What changed")).toBeInTheDocument();
    expect(screen.getByText("component triage (1.2.0) on for 50% of users")).toBeInTheDocument();
    expect(screen.getByText("Asked for by")).toBeInTheDocument();
    expect(screen.getByText("ada")).toBeInTheDocument();
    expect(screen.getByText("G6 sam")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "org/repo/pull/42" })).toHaveAttribute(
      "href",
      "https://github.com/org/repo/pull/42",
    );
    expect(screen.getByText("Checks")).toBeInTheDocument();
    expect(screen.getByText("tests")).toBeInTheDocument();
    expect(screen.getByText("Low risk release")).toBeInTheDocument();
    expect(screen.getByText("INC0001 (closed): linked")).toBeInTheDocument();
    expect(screen.getByText(/aabbccdd11223344… \(after 99887766…\)/)).toBeInTheDocument();
    await user.click(toggle);
    expect(screen.queryByText("What changed")).not.toBeInTheDocument();
  });

  it("shows the Failed chip and the what-went-wrong detail for a failed change", async () => {
    const user = userEvent.setup();
    mockLoaded({
      rows: [changeFixture({ outcome: "failed", detail: "Deploy crashed", approvals: [], receipt: null })],
    });
    renderChanges();
    expect(await screen.findByText("Failed")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Released: Released triage queue/ }));
    expect(screen.getByText("What went wrong")).toBeInTheDocument();
    expect(screen.getByText("Deploy crashed")).toBeInTheDocument();
    // Row approver cell + the detail's fallback approvals line.
    expect(screen.getAllByText("sam")).toHaveLength(2);
  });

  it("renders emergency and automatic types and unknown actions verbatim", async () => {
    mockLoaded({
      rows: [
        changeFixture({ id: "chg-2", type: "emergency", action: "rollback", summary: "Rolled back" }),
        changeFixture({ id: "chg-3", type: "automatic", action: "mystery_action", summary: "Odd change" }),
      ],
    });
    renderChanges();
    expect(await screen.findByText("Emergency")).toBeInTheDocument();
    expect(screen.getByText("Automatic")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Rolled back: Rolled back/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /mystery_action: Odd change/ })).toBeInTheDocument();
  });

  it("links the CSV export with the product id", async () => {
    mockLoaded();
    renderChanges();
    expect(await screen.findByRole("link", { name: "Download for audit (CSV)" })).toHaveAttribute(
      "href",
      "/api/changes/export.csv?product_id=cln",
    );
  });

  it("switching the product selector refetches all three calls", async () => {
    const user = userEvent.setup();
    mockLoaded();
    renderChanges();
    await screen.findByText("Released triage queue");
    expect(vi.mocked(api.changes)).toHaveBeenCalledWith("cln");
    expect(vi.mocked(api.lookback)).toHaveBeenCalledWith("cln", 6);
    await user.selectOptions(screen.getByLabelText("Product"), "ops");
    await waitFor(() => expect(vi.mocked(api.changes)).toHaveBeenCalledWith("ops"));
    await waitFor(() => expect(vi.mocked(api.lookback)).toHaveBeenCalledWith("ops", 6));
  });

  it("renders a minimal change row with every fallback", async () => {
    const user = userEvent.setup();
    mockLoaded({
      rows: [
        changeFixture({
          id: "chg-9",
          type: "emergency",
          action: "flag_off",
          summary: "Flag off",
          targets: [{ kind: "component", name: "triage", percent: 100, on: false }],
          feature_id: "",
          release_id: "",
          incident_id: "",
          requested_by: "",
          approved_by: [],
          approvals: [],
          evidence: {},
          outcome: "done",
          detail: "",
          caused_incidents: [],
          receipt: null,
        }),
      ],
      lookback: lookbackFixture({ causes: [] }),
    });
    renderChanges();
    const toggle = await screen.findByRole("button", { name: /Flag turned off: Flag off/ });
    const log = screen.getByRole("heading", { name: "Change log" }).closest("section")!;
    // Who (empty requested_by) and Caused (no incidents) both render "–".
    expect(within(log).getAllByText("–")).toHaveLength(2);
    expect(screen.queryByText(/Releases behind the most incidents/)).not.toBeInTheDocument();
    await user.click(toggle);
    expect(screen.getByText("component triage off")).toBeInTheDocument();
    expect(screen.queryByText("Pull requests")).not.toBeInTheDocument();
    expect(screen.queryByText("Checks")).not.toBeInTheDocument();
    expect(screen.queryByText("Why")).not.toBeInTheDocument();
    expect(screen.queryByText("What went wrong")).not.toBeInTheDocument();
    expect(screen.queryByText("Record")).not.toBeInTheDocument();
    expect(screen.queryByText("ServiceNow")).not.toBeInTheDocument();
  });

  it("links the record from the release id when there is no incident or feature", async () => {
    const user = userEvent.setup();
    mockLoaded({
      rows: [
        changeFixture({
          feature_id: "",
          incident_id: "",
          release_id: "CLN-REL-1",
          evidence: { pull_requests: [], quality: {}, triage: "" },
        }),
      ],
    });
    renderChanges();
    const toggle = await screen.findByRole("button", { name: /Released: Released triage queue/ });
    await user.click(toggle);
    expect(screen.getByRole("link", { name: "CLN-REL-1" })).toHaveAttribute("href", "/releases/CLN-REL-1");
    expect(screen.queryByText("Pull requests")).not.toBeInTheDocument();
    expect(screen.queryByText("Checks")).not.toBeInTheDocument();
  });
});
