import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { api, type HealthMeasure, type Me, type ToolHealth } from "../api";
import { HealthPage } from "./Health";

vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  const mocked = Object.fromEntries(Object.keys(actual.api).map((k) => [k, vi.fn()]));
  return { ...actual, api: mocked };
});

type Mock = ReturnType<typeof vi.fn>;
const m = (k: keyof typeof api) => api[k] as unknown as Mock;

const me: Me = {
  login: "ada",
  name: "Ada",
  avatar_url: "",
  orgs: [],
  teams: [],
  lanes: [{ id: "FARM", name: "Farm" }],
};

function measure(over: Partial<HealthMeasure> = {}): HealthMeasure {
  return {
    since: "2026-11-13T00:00:00+00:00",
    until: "2026-11-20T00:00:00+00:00",
    features: 4,
    phases: {
      spec: { runs: 8, escalation_rate: 0.25, stall_rate: 0.125, stalls: 1, loops_mean: 1.5, loops_p90: 3 },
    },
    critic: { spec: { passed: 6, false_pass: 0.5, failed: 2, false_fail: 0 } },
    gates: { g2: { decided: 6, first_time_approval: 0.5 } },
    escalation_reasons: { "loop cap": 2 },
    stalls: {},
    reopened: {},
    cost_by_phase_role: {},
    agent_minutes_by_phase_role: {},
    cost_per_feature: { features: 4, mean: 3.2, max: 5 },
    ...over,
  };
}

function health(over: Partial<ToolHealth> = {}): ToolHealth {
  return {
    at: "2026-11-20T00:00:00+00:00",
    product_id: "FARM",
    window_days: 7,
    baseline_days: 28,
    current: measure(),
    baseline: measure({
      critic: { spec: { passed: 20, false_pass: 0.1, failed: 4, false_fail: 0.25 } },
      gates: { g2: { decided: 20, first_time_approval: 0.85 } },
    }),
    alerts: [],
    safe_mode: null,
    ...over,
  };
}

const safe = {
  on: true,
  since: "2026-11-18T09:00:00+00:00",
  says: "Safe mode since 2026-11-18 09:00 UTC: The spec critic passed 50% of documents people then sent back.",
  phases: ["spec"],
  normal_checks: 2,
  needed: 2,
  fix: { at: "2026-11-19T10:00:00+00:00", by: "sam", note: "Pinned the previous model" },
  hours: 39,
  ready: true,
  causes: [],
  checks: [
    {
      at: "2026-11-19T11:00:00+00:00",
      source: "replay",
      normal: true,
      counted: true,
      says: "Back at the baseline on 12 cases",
    },
  ],
  approvals_to_review: [{ feature_id: "FARM-FEAT-3", gate: "g2", at: "2026-11-17T10:00:00+00:00" }],
};

function show(who: Me = me) {
  return render(
    <MemoryRouter initialEntries={["/health"]}>
      <HealthPage me={who} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  for (const fn of Object.values(api)) (fn as unknown as Mock).mockRejectedValue(new Error("not mocked"));
});
afterEach(cleanup);

describe("agent health", () => {
  it("shows the product's agents against their baseline, and what's alerting", async () => {
    m("toolHealth").mockResolvedValue(
      health({
        alerts: [
          {
            key: "critic_false_pass:spec",
            rule: "critic_false_pass",
            scope: "spec",
            value: 0.5,
            baseline: 0.1,
            says: "The spec critic passed 50% of documents people then sent back",
          },
        ],
        what_changed_summary: "What changed: FARM added lessons L-007.",
      }),
    );
    show();
    expect(await screen.findByText(/The spec critic passed 50%/)).toBeInTheDocument();
    expect(m("toolHealth")).toHaveBeenCalledWith("FARM");
    expect(screen.getByText("What changed: FARM added lessons L-007.")).toBeInTheDocument();
    const row = screen.getByRole("row", { name: /Spec/ });
    expect(row).toHaveTextContent("50% (10%)");
    expect(screen.getByRole("row", { name: /G2 spec/ })).toHaveTextContent("50% (85%)");
    expect(screen.queryByText("Queue and drift")).not.toBeInTheDocument(); // the tool's owners only
  });

  it("says when nothing is alerting", async () => {
    m("toolHealth").mockResolvedValue(health());
    show();
    expect(await screen.findByText(/Nothing is alerting/)).toBeInTheDocument();
  });

  it("lets the tool's owners see every product, the queue and drift, and end safe mode", async () => {
    m("toolHealth").mockResolvedValue(
      health({
        product_id: undefined,
        safe_mode: { FARM: safe },
        queue: { queued: 1, running: 2, oldest_s: 120 },
        dead_letters: {
          count: 1,
          recent: [{ at: "2026-11-19T00:00:00+00:00", thread_id: "FARM-FEAT-9", attempts: 3 }],
        },
        drift: { at: "2026-11-16T00:00:00+00:00", regressions: [], anchor_cases: 24 },
      }),
    );
    m("safeModeLeave").mockResolvedValue({});
    show({ ...me, tool_owner: true, lanes: [] });
    expect(await screen.findByText("FARM has been in safe mode for 39 hours")).toBeInTheDocument();
    expect(m("toolHealth")).toHaveBeenCalledWith(undefined);
    expect(screen.getByText(/Pinned the previous model/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "FARM-FEAT-3" })).toBeInTheDocument();
    expect(screen.getByText(/1 queued, 2 running, oldest 2 minutes/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "FARM-FEAT-9" })).toBeInTheDocument();
    expect(screen.getByText(/At their baseline on 2026-11-16 \(24 pinned cases\)/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "End safe mode" }));
    await waitFor(() => expect(m("safeModeLeave")).toHaveBeenCalledWith("FARM"));
    expect(await screen.findByText("Safe mode ended")).toBeInTheDocument();
  });

  it("doesn't offer safe mode's actions to a product's people", async () => {
    m("toolHealth").mockResolvedValue(health({ safe_mode: { ...safe, ready: false, normal_checks: 0 } }));
    show();
    expect(await screen.findByText("FARM has been in safe mode for 39 hours")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "End safe mode" })).not.toBeInTheDocument();
  });
});
