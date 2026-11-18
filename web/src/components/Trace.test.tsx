import { beforeEach, describe, expect, it, vi } from "vitest";
import { configure, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { Trace } from "../api";
import { apiMock, resetApiMock } from "../test/apiMock";
import { TracePanel } from "./Trace";

// Multi-step user-event flows and async fetches exceed Vitest's 5s default
// under parallel load on the shared VM; give them room.
vi.setConfig({ testTimeout: 20000 });
configure({ asyncUtilTimeout: 5000 });

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock };
});

function trace(over: Partial<Trace> = {}): Trace {
  return {
    feature_id: "CLN-1",
    title: "CSV export",
    profile: {
      id: "p1",
      label: "Default",
      budget_usd: 0,
      test_threshold: 0,
      release_model: "launch",
      review_model: "async",
      require_release_gate: false,
      stages: {},
      gates: {},
      review: { mode: "async", reviewers: [], require_human: false },
    },
    metrics: [],
    goals: [{ id: "G1", text: "Fewer no-shows", metrics: ["G1"], requirements: ["C1"] }],
    criteria: [
      {
        id: "C1",
        type: "functional",
        text: "Export a CSV of visits",
        goals: ["G1"],
        tasks: ["T1"],
        cases: [{ id: "TC1", kind: "unit", status: "passed", what: "CSV rows", ref: "tests/export.test.ts" }],
        prs: [{ key: "org/visits#12", url: "https://example.invalid/pr/12" }],
        acceptance: { staging: "passed" },
        deployed: { "org/visits": "production" },
        deferred: null,
      },
      {
        id: "C2",
        type: "functional",
        text: "Deferred work",
        goals: [],
        tasks: [],
        cases: [],
        prs: [],
        acceptance: {},
        deployed: {},
        deferred: "Waiting for the new API",
      },
    ],
    nfrs: [
      {
        id: "N1",
        type: "security",
        text: "No secrets in logs",
        goals: [],
        tasks: [],
        cases: [],
        prs: [],
        acceptance: {},
        deployed: {},
        deferred: null,
      },
    ],
    deferred: [],
    complete: false,
    decisions: [{ id: "D1", text: "Use Postgres" }],
    repos: [],
    release: { flag: "", percent: null, production: true, merged: true },
    gaps: ["C2 has no test case"],
    incidents: [{ id: "CLN-INC-1", title: "Export outage", status: "open" }],
    ...over,
  } as Trace;
}

beforeEach(() => {
  resetApiMock();
});

describe("TracePanel", () => {
  it("renders nothing while loading, on error, or when there is nothing to trace", async () => {
    apiMock.trace.mockReturnValue(new Promise(() => {}));
    const { container, unmount } = render(
      <MemoryRouter>
        <TracePanel featureId="CLN-1" version={1} />
      </MemoryRouter>,
    );
    expect(container).toBeEmptyDOMElement();
    unmount();

    apiMock.trace.mockRejectedValue(new Error("gone"));
    const failed = render(
      <MemoryRouter>
        <TracePanel featureId="CLN-1" version={1} />
      </MemoryRouter>,
    );
    await new Promise((r) => setTimeout(r, 50));
    expect(failed.container).toBeEmptyDOMElement();
    failed.unmount();

    apiMock.trace.mockResolvedValue(trace({ criteria: [], goals: [] }));
    const empty = render(
      <MemoryRouter>
        <TracePanel featureId="CLN-1" version={1} />
      </MemoryRouter>,
    );
    await new Promise((r) => setTimeout(r, 50));
    expect(empty.container).toBeEmptyDOMElement();
  });

  it("renders the full matrix with goals, gaps, decisions and incidents", async () => {
    apiMock.trace.mockResolvedValue(trace());
    render(
      <MemoryRouter>
        <TracePanel featureId="CLN-1" version={1} />
      </MemoryRouter>,
    );
    expect(await screen.findByRole("heading", { name: "Traceability" })).toBeInTheDocument();
    expect(screen.getAllByText("G1")).toHaveLength(2);
    expect(screen.getByText("Fewer no-shows")).toBeInTheDocument();
    expect(screen.getByText(/1 gap below\./)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Export the matrix (CSV)" })).toHaveAttribute(
      "href",
      "/api/features/CLN-1/trace.csv",
    );
    expect(screen.getByText("C1")).toBeInTheDocument();
    expect(screen.getByText("TC1 (unit): passed")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "visits#12" })).toHaveAttribute(
      "href",
      "https://example.invalid/pr/12",
    );
    expect(screen.getByText("staging: passed")).toBeInTheDocument();
    expect(screen.getByText("visits in production")).toBeInTheDocument();
    expect(screen.getByText("Deferred: Waiting for the new API")).toBeInTheDocument();
    expect(screen.getByText("N1")).toBeInTheDocument();
    expect(screen.getByText("no test case")).toBeInTheDocument();
    expect(screen.getByText("none yet")).toBeInTheDocument();
    expect(screen.getByText("not run")).toBeInTheDocument();
    expect(screen.getByText("not deployed")).toBeInTheDocument();
    expect(screen.getByText(/Decisions: D1 Use Postgres/)).toBeInTheDocument();
    expect(screen.getByText("C2 has no test case")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "CLN-INC-1" })).toHaveAttribute("href", "/incidents/CLN-INC-1");
  });

  it("says every requirement is traced when complete", async () => {
    apiMock.trace.mockResolvedValue(trace({ complete: true, gaps: [] }));
    render(
      <MemoryRouter>
        <TracePanel featureId="CLN-1" version={1} />
      </MemoryRouter>,
    );
    expect(await screen.findByText(/Every requirement is traced to passing tests\./)).toBeInTheDocument();
    expect(screen.queryByText("Gaps")).not.toBeInTheDocument();
  });

  it("refetches when the version changes", async () => {
    apiMock.trace.mockResolvedValue(trace());
    const { rerender } = render(
      <MemoryRouter>
        <TracePanel featureId="CLN-1" version={1} />
      </MemoryRouter>,
    );
    await screen.findByRole("heading", { name: "Traceability" });
    rerender(
      <MemoryRouter>
        <TracePanel featureId="CLN-1" version={2} />
      </MemoryRouter>,
    );
    expect(apiMock.trace).toHaveBeenCalledTimes(2);
  });
});

it("covers the remaining trace branches", async () => {
  const base = trace();
  apiMock.trace.mockResolvedValue(
    trace({
      goals: [{ id: "G1", text: "Fewer no-shows", metrics: [], requirements: ["C1"] }],
      gaps: ["First gap", "Second gap"],
      nfrs: undefined as unknown as Trace["nfrs"],
      criteria: [{ ...base.criteria[0], acceptance: { staging: "weird" } }, base.criteria[1]],
      incidents: [
        { id: "CLN-INC-1", title: "Export outage", status: "open", severity: "high", touch: "export" },
        { id: "CLN-INC-2", title: "Slow export", status: "closed", severity: "low", touch: "export" },
      ],
    }),
  );
  render(
    <MemoryRouter>
      <TracePanel featureId="CLN-1" version={1} />
    </MemoryRouter>,
  );
  expect(await screen.findByText("(no metric)")).toBeInTheDocument();
  expect(screen.getByText(/2 gaps below\./)).toBeInTheDocument();
  expect(screen.getByText("staging: weird")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "CLN-INC-2" })).toBeInTheDocument();
});

it("falls back to raw status text and plain pull-request numbers", async () => {
  const base = trace();
  apiMock.trace.mockResolvedValue(
    trace({
      criteria: [
        {
          ...base.criteria[0],
          cases: [
            {
              id: "TC9",
              kind: "unit",
              status: "mystery",
              what: "Odd case",
              ref: "tests/odd.test.ts",
            } as never,
          ],
          prs: [
            { key: "org/visits#12", url: "https://example.invalid/pr/12" } as never,
            { key: "org/visits#13", url: "" } as never,
          ],
        },
        base.criteria[1],
      ],
    }),
  );
  render(
    <MemoryRouter>
      <TracePanel featureId="CLN-1" version={1} />
    </MemoryRouter>,
  );
  // "G1" appears twice: once in the goals list, once in the criteria cell.
  expect(await screen.findAllByText("G1")).toHaveLength(2);
  expect(screen.getByText("mystery")).toBeInTheDocument();
  // The linked PR shows its key; the url-less one is plain text after a separator.
  expect(screen.getByText(/visits#12/)).toBeInTheDocument();
  const second = screen.getByText(/visits#13/);
  expect(second.tagName).toBe("SPAN");
  expect(second.textContent).toMatch(/^, /);
});
