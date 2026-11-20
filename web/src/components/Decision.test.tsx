import { beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, configure, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import type { IncidentAction, PendingItem, ReleaseReadiness } from "../api";
import { apiMock, resetApiMock } from "../test/apiMock";
import { DecisionPanel } from "./Decision";

// Multi-step user-event flows and async fetches exceed Vitest's 5s default
// under parallel load on the shared VM; give them room.
vi.setConfig({ testTimeout: 20000 });
configure({ asyncUtilTimeout: 5000 });

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock };
});

function gateItem(over: Partial<PendingItem> = {}): PendingItem {
  return {
    interrupt_id: "int-1",
    type: "gate",
    gate: "g1",
    approvers: ["product-council"],
    can_decide: true,
    feature_id: "CLN-1",
    product_id: "CLN",
    title: "Add a CSV export",
    cost_usd: 1.2,
    budget_usd: 10,
    ...over,
  };
}

function renderPanel(item: PendingItem, onDone = vi.fn()) {
  render(
    <MemoryRouter>
      <DecisionPanel item={item} featureId={item.feature_id ?? "CLN-1"} onDone={onDone} />
    </MemoryRouter>,
  );
  return { onDone };
}

beforeEach(() => {
  resetApiMock();
  apiMock.artifact.mockResolvedValue({
    id: "PRD",
    content: "# Product spec\n\nThe export lists visits as CSV.",
    version: 1,
    constitution_version: "v1",
  });
});

describe("DecisionPanel headings and approve flow", () => {
  it("renders all four panel headings for a simple gate", async () => {
    renderPanel(gateItem());
    expect(await screen.findByRole("heading", { name: "Is this worth building?" })).toBeInTheDocument();
    expect(screen.getByText("What you’re approving")).toBeInTheDocument();
    expect(screen.getByText("Worth knowing")).toBeInTheDocument();
    // Renders even when the gate has no check sections beneath it.
    expect(screen.getByText("Checks that passed")).toBeInTheDocument();
    expect(screen.getByText("What happens next")).toBeInTheDocument();
  });

  it("shows the gate's approvers and the cost facts", async () => {
    renderPanel(gateItem({ score: 0.87, loops: 2, risk_tier: "high" }));
    expect(await screen.findByText(/Idea approval \(G1\), decided by product-council/)).toBeInTheDocument();
    expect(screen.getByText("0.87")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
    expect(screen.getByText("High risk")).toBeInTheDocument();
    expect(screen.getByText("$1.20 of $10.00")).toBeInTheDocument();
  });

  it("approves with the gate's verb and calls onDone", async () => {
    const user = userEvent.setup();
    const { onDone } = renderPanel(gateItem());
    await user.click(await screen.findByRole("button", { name: "Approve the idea" }));
    expect(apiMock.decide).toHaveBeenCalledWith(
      "CLN-1",
      "int-1",
      expect.objectContaining({ decision: "approve", feedback: "" }),
    );
    const body = apiMock.decide.mock.calls[0][2] as Record<string, unknown>;
    expect(typeof body.active_minutes).toBe("number");
    expect(body.active_minutes as number).toBeGreaterThanOrEqual(0);
    expect(onDone).toHaveBeenCalled();
  });

  it("shows a decide error and keeps the panel open", async () => {
    const user = userEvent.setup();
    apiMock.decide.mockRejectedValueOnce(new Error("Server is busy"));
    const { onDone } = renderPanel(gateItem());
    await user.click(await screen.findByRole("button", { name: "Approve the idea" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Server is busy");
    expect(screen.getByRole("button", { name: "Approve the idea" })).toBeInTheDocument();
    expect(onDone).not.toHaveBeenCalled();
  });

  it("takes over from a gate", async () => {
    const user = userEvent.setup();
    renderPanel(gateItem());
    await user.click(await screen.findByRole("button", { name: "Take over" }));
    expect(apiMock.decide).toHaveBeenCalledWith(
      "CLN-1",
      "int-1",
      expect.objectContaining({ decision: "takeover" }),
    );
  });

  it("stops the feature through the abort flow", async () => {
    const user = userEvent.setup();
    renderPanel(gateItem());
    await user.click(await screen.findByRole("button", { name: "Stop feature" }));
    expect(screen.getByLabelText("Why stop this feature?")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Why stop this feature?"), "Out of scope now");
    await user.click(screen.getByRole("button", { name: "Stop feature" }));
    expect(apiMock.decide).toHaveBeenCalledWith(
      "CLN-1",
      "int-1",
      expect.objectContaining({ decision: "abort", feedback: "Out of scope now" }),
    );
  });

  it("cancels out of a mode back to the main buttons", async () => {
    const user = userEvent.setup();
    renderPanel(gateItem());
    await user.click(await screen.findByRole("button", { name: "Request changes" }));
    expect(screen.queryByRole("button", { name: "Approve the idea" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("button", { name: "Approve the idea" })).toBeInTheDocument();
  });

  it("resets its state when a different item is shown", async () => {
    const user = userEvent.setup();
    const first = gateItem();
    const { rerender } = render(
      <MemoryRouter>
        <DecisionPanel item={first} featureId="CLN-1" onDone={() => {}} />
      </MemoryRouter>,
    );
    await user.click(await screen.findByRole("button", { name: "Request changes" }));
    await user.type(screen.getByLabelText("What should the agents change?"), "Needs more detail");
    rerender(
      <MemoryRouter>
        <DecisionPanel
          item={gateItem({ interrupt_id: "int-2", gate: "g2" })}
          featureId="CLN-1"
          onDone={() => {}}
        />
      </MemoryRouter>,
    );
    expect(screen.getByRole("button", { name: "Approve the scope" })).toBeInTheDocument();
    expect(screen.queryByDisplayValue("Needs more detail")).not.toBeInTheDocument();
  });
});

describe("send back (revise)", () => {
  it("sends feedback with a reason", async () => {
    const user = userEvent.setup();
    renderPanel(gateItem());
    await user.click(await screen.findByRole("button", { name: "Request changes" }));
    const send = screen.getByRole("button", { name: "Send back with changes" });
    expect(send).toBeDisabled();
    await user.type(screen.getByLabelText("What should the agents change?"), "The measure is missing");
    await user.selectOptions(screen.getByLabelText("Main reason"), "quality");
    await user.click(screen.getByRole("button", { name: "Send back with changes" }));
    expect(apiMock.decide).toHaveBeenCalledWith(
      "CLN-1",
      "int-1",
      expect.objectContaining({ decision: "revise", feedback: "The measure is missing", reason: "quality" }),
    );
  });

  it("offers to remember the correction as a lesson", async () => {
    const user = userEvent.setup();
    renderPanel(gateItem());
    await user.click(await screen.findByRole("button", { name: "Request changes" }));
    await user.click(screen.getByLabelText("Remember this for future features"));
    await user.type(
      screen.getByLabelText(/The lesson, in one line/),
      "Every spec says how long data is kept",
    );
    await user.type(screen.getByLabelText("What should the agents change?"), "Add retention");
    await user.click(screen.getByRole("button", { name: "Send back with changes" }));
    expect(apiMock.decide).toHaveBeenCalledWith(
      "CLN-1",
      "int-1",
      expect.objectContaining({
        decision: "revise",
        remember: true,
        lesson: "Every spec says how long data is kept",
      }),
    );
  });

  it("hides send-back when the item does not allow revise", () => {
    renderPanel(gateItem({ allowed: ["approve"] }));
    expect(screen.queryByRole("button", { name: "Request changes" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve the idea" })).toBeInTheDocument();
  });

  it("surfaces the stuck note and its takeover hint", async () => {
    renderPanel(gateItem({ stuck: true, revisions: 3 }));
    expect(await screen.findByText("Sent back 3 times.")).toBeInTheDocument();
    expect(screen.getByText(/Taking it over may be quicker/)).toBeInTheDocument();
  });

  it("tells a stuck item that cannot be sent back again", () => {
    renderPanel(gateItem({ stuck: true, revisions: 4, allowed: ["approve"] }));
    expect(screen.getByText(/It can't be sent back again/)).toBeInTheDocument();
  });

  it("shows earlier corrections in the history", async () => {
    renderPanel(
      gateItem({
        history: [
          {
            gate: "g1",
            decision: "revise",
            approver: "ada",
            reason: "quality",
            feedback: "Needs a measure",
            at: "2026-11-01T00:00:00Z",
          },
          {
            gate: "g1",
            decision: "retry",
            approver: "bo",
            reason: "",
            feedback: "",
            at: "2026-11-02T00:00:00Z",
          },
        ],
      }),
    );
    expect(await screen.findByText("What people asked for before")).toBeInTheDocument();
    expect(screen.getByText(/Sent back/, { exact: false })).toBeInTheDocument();
    expect(screen.getByText(/by ada \(quality isn't good enough\): Needs a measure/)).toBeInTheDocument();
    expect(screen.getByText("Retried")).toBeInTheDocument();
    expect(screen.getByText(/by bo/)).toBeInTheDocument();
  });
});

describe("line comments", () => {
  const withDoc = () =>
    gateItem({ artifact: { id: "PRD", kind: "PRD" }, findings: ["The measure is missing"] });

  it("adds a comment from a finding and sends it with revise", async () => {
    const user = userEvent.setup();
    renderPanel(withDoc());
    await user.click(await screen.findByRole("button", { name: "Add as comment" }));
    expect(screen.getByText("Your comments (1)")).toBeInTheDocument();
    // The finding stays in the findings list and appears again in the comments box.
    expect(screen.getAllByText("The measure is missing")).toHaveLength(2);
    await user.click(screen.getByRole("button", { name: "Request changes (1 comment)" }));
    await user.click(screen.getByRole("button", { name: /Revise with 1 comment/ }));
    const body = apiMock.decide.mock.calls[0][2] as Record<string, unknown>;
    expect(body.decision).toBe("revise");
    expect(body.comments).toEqual([{ quote: "", text: "The measure is missing" }]);
  });

  it("does not duplicate the same finding comment", async () => {
    const user = userEvent.setup();
    renderPanel(withDoc());
    const btn = await screen.findByRole("button", { name: "Add as comment" });
    await user.click(btn);
    await user.click(btn);
    expect(screen.getByText("Your comments (1)")).toBeInTheDocument();
  });

  it("removes a comment", async () => {
    const user = userEvent.setup();
    renderPanel(withDoc());
    await user.click(await screen.findByRole("button", { name: "Add as comment" }));
    await user.click(screen.getByRole("button", { name: "Remove" }));
    expect(screen.queryByText("Your comments (1)")).not.toBeInTheDocument();
  });

  it("comments on a document line through the Comment tab", async () => {
    const user = userEvent.setup();
    renderPanel(withDoc());
    await user.click(await screen.findByRole("tab", { name: "Comment on lines" }));
    const line = await screen.findByRole("button", { name: "Comment on: Product spec" });
    await user.click(line);
    await user.type(screen.getByLabelText("Comment on this line"), "Say what CSV means here");
    await user.click(screen.getByRole("button", { name: "Add" }));
    expect(screen.getByText("Your comments (1)")).toBeInTheDocument();
    expect(screen.getByText(/On “Product spec”:/)).toBeInTheDocument();
    expect(screen.getByText("Say what CSV means here")).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Read" }));
    expect(await screen.findByText("The export lists visits as CSV.")).toBeInTheDocument();
  });

  it("loads the brief and shows what changed since the sent-back version", async () => {
    apiMock.decisionBrief.mockResolvedValue({
      summary: { lines: ["A CSV export for the visits list."], by: "agent" },
      changed: {
        from_version: 2,
        lines: [
          { op: "+", text: "Added the measure." },
          { op: "-", text: "Removed the old scope." },
          { op: "…", text: "" },
        ],
        asked: "Add a measure",
      },
    });
    renderPanel(gateItem());
    expect(await screen.findByText("In two minutes")).toBeInTheDocument();
    expect(screen.getByText("A CSV export for the visits list.")).toBeInTheDocument();
    expect(screen.getByText("Summarised by an agent from the document below.")).toBeInTheDocument();
    expect(screen.getByText(/Changed since version 2/)).toBeInTheDocument();
    expect(screen.getByText("Added the measure.")).toBeInTheDocument();
    expect(screen.getByText("Removed the old scope.")).toBeInTheDocument();
    expect(screen.getByText(/You asked: “Add a measure”/)).toBeInTheDocument();
  });

  it("renders nothing extra when the brief fails to load", async () => {
    apiMock.decisionBrief.mockRejectedValue(new Error("gone"));
    renderPanel(gateItem());
    expect(await screen.findByRole("heading", { name: "Is this worth building?" })).toBeInTheDocument();
    expect(screen.queryByText("In two minutes")).not.toBeInTheDocument();
  });
});

describe("agent notes", () => {
  it("lists notes and lets an open question become a comment", async () => {
    const user = userEvent.setup();
    renderPanel(
      gateItem({
        agent_notes: [
          { kind: "assumed", text: "Visits are exported per clinic.", task: "Write spec", repo: "org/repo" },
          { kind: "open", text: "Which columns?", phase: "spec" },
        ],
      }),
    );
    expect(await screen.findByText("The agents' notes")).toBeInTheDocument();
    expect(screen.getByText(/Approving confirms what the agents assumed/)).toBeInTheDocument();
    expect(screen.getByText("Assumed:")).toBeInTheDocument();
    expect(screen.getByText(/Visits are exported per clinic/)).toBeInTheDocument();
    const add = screen.getAllByRole("button", { name: "Add as comment" });
    await user.click(add[1]);
    expect(screen.getByText("Your comments (1)")).toBeInTheDocument();
  });
});

describe("G6 traceability", () => {
  const traceability = {
    complete: false,
    gaps: ["Requirement R1 has no passing test"],
    deferred: [{ id: "R2", reason: "next release" }],
    drift: ["Work tracker lists R3 as done; matrix does not"],
    drift_error: "",
    drift_blocks: false,
    blocks: true,
    override_approvers: ["trace-team"],
  };

  it("blocks approval until an override reason is given", async () => {
    const user = userEvent.setup();
    renderPanel(gateItem({ gate: "g6", traceability }));
    const approve = await screen.findByRole("button", { name: "Release to everyone" });
    expect(approve).toBeDisabled();
    expect(screen.getByText("Traceability")).toBeInTheDocument();
    expect(screen.getByText("Requirement R1 has no passing test")).toBeInTheDocument();
    expect(
      screen.getByText(/Releasing needs these closed, or an override with your reason/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Approving accepts what the plan defers: R2 \(next release\)/),
    ).toBeInTheDocument();
    expect(screen.getByLabelText(/Release anyway: why/)).toBeInTheDocument();
    await user.type(screen.getByLabelText(/Release anyway: why/), "Hotfix for a live outage");
    await user.click(screen.getByRole("button", { name: "Release to everyone" }));
    expect(apiMock.decide).toHaveBeenCalledWith(
      "CLN-1",
      "int-1",
      expect.objectContaining({ decision: "approve", override_reason: "Hotfix for a live outage" }),
    );
  });

  it("shows gaps as non-blocking for a hotfix and drift as a warning", async () => {
    renderPanel(
      gateItem({
        gate: "g6",
        traceability: {
          ...traceability,
          blocks: false,
          drift_blocks: false,
          drift_error: "tracker unreachable",
        },
      }),
    );
    expect(await screen.findByText(/Gaps \(a hotfix isn't held for them\):/)).toBeInTheDocument();
    expect(screen.getByText(/The work tracker differs from the matrix \(a warning\):/)).toBeInTheDocument();
    expect(screen.getByText(/The work tracker couldn't be checked: tracker unreachable/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Release to everyone" })).toBeEnabled();
  });

  it("shows drift as blocking when the product holds releases for it", async () => {
    renderPanel(
      gateItem({ gate: "g6", traceability: { ...traceability, blocks: false, drift_blocks: true } }),
    );
    expect(
      await screen.findByText(
        /The work tracker differs from the matrix \(this product holds releases for it\):/,
      ),
    ).toBeInTheDocument();
  });

  it("reports a complete matrix", async () => {
    renderPanel(
      gateItem({
        gate: "g6",
        traceability: {
          complete: true,
          gaps: [],
          deferred: [],
          drift: [],
          drift_error: "",
          drift_blocks: false,
          blocks: false,
          override_approvers: [],
        },
      }),
    );
    expect(await screen.findByText("Every requirement is traced to passing tests.")).toBeInTheDocument();
  });

  it("shows release candidates and agent evals", async () => {
    renderPanel(
      gateItem({
        gate: "g6",
        packages: [
          {
            repo: "org/pkg",
            version: "1.2.0",
            rc: "v1.2.0-rc.1",
            status: "missing",
            package: { type: "npm", name: "pkg" },
          },
        ],
        agent_evals: [
          {
            repo: "org/agent",
            role: "agent",
            stages: { pr: { suite: "golden", passed: 8, failed: 2, rate: 0.8, state: "passed" } },
            previous: { pr: { suite: "golden", passed: 7, failed: 3, rate: 0.7, state: "passed" } },
            previous_feature: "CLN-9",
            change: { pr: 0.1 },
          },
        ],
      }),
    );
    expect(await screen.findByText("Packages")).toBeInTheDocument();
    expect(
      screen.getByText(/org\/pkg: v1.2.0-rc.1 becomes v1.2.0 \(not in GitHub Packages yet, npm pkg\)/),
    ).toBeInTheDocument();
    expect(screen.getByText("The agents and knowledge this change ships")).toBeInTheDocument();
    expect(screen.getByText("8 of 10 (80%)")).toBeInTheDocument();
    expect(screen.getByText("70% (CLN-9)")).toBeInTheDocument();
    expect(screen.getByText("+10 points")).toBeInTheDocument();
  });

  it("shows agent sessions behind a disclosure", async () => {
    renderPanel(
      gateItem({
        gate: "g6",
        agent_sessions: [
          {
            trace_id: "t1",
            url: "https://trace.example/t1",
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
    const details = await screen.findByText(/How the agents built it \(1 sessions\)/);
    await userEvent.setup().click(details);
    expect(await screen.findByText("Open trace")).toBeInTheDocument();
  });

  it("shows mockups at G3 and screens as built at G6", async () => {
    const user = userEvent.setup();
    renderPanel(
      gateItem({
        gate: "g3",
        mockups: {
          screens: [{ id: "M1", name: "Visits list", app: "web", phone: false }],
          findings: ["The list needs a filter."],
        },
        screens: {
          scanned: true,
          state: "passed",
          screens: [
            {
              id: "M1",
              name: "Visits list",
              app: "web",
              staging: { route: "/visits", desktop: "shot", phone: "shot" },
              violations: [
                {
                  fingerprint: "f1",
                  severity: "high",
                  rule: "color-contrast",
                  title: "M1: low contrast text",
                },
              ],
            },
          ],
        },
      }),
    );
    expect(await screen.findByText("The screens")).toBeInTheDocument();
    const frame = screen.getByTitle("Mockup of M1: Visits list");
    expect(frame).toHaveAttribute("src", "/api/features/CLN-1/mockups/M1");
    await user.click(screen.getByRole("button", { name: "Phone" }));
    expect(screen.getByRole("button", { name: "Phone" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("The screens, as approved and as built")).toBeInTheDocument();
    expect(screen.getByText("Serious")).toBeInTheDocument();
    expect(screen.getByText(/color-contrast: low contrast text/)).toBeInTheDocument();
  });

  it("offers the change set's environment at G5, or says it's gone", async () => {
    renderPanel(
      gateItem({
        gate: "g5",
        try_it: {
          url: "https://web.cs-cln-1.dev.example",
          env: "cs-cln-1",
          expires_at: "2026-11-22T18:00:00+00:00",
        },
      }),
    );
    const link = await screen.findByRole("link", { name: "Open it in cs-cln-1" });
    expect(link).toHaveAttribute("href", "https://web.cs-cln-1.dev.example");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(
      screen.getByText(/Removed when the change merges, or at 2026-11-22 18:00 UTC/),
    ).toBeInTheDocument();
    cleanup();
    renderPanel(
      gateItem({
        gate: "g5",
        try_it: { gone: "Its environment ran out of time (48 hours) and was removed" },
      }),
    );
    expect(await screen.findByText(/ran out of time/)).toBeInTheDocument();
  });

  it("shows a rough sketch beside the brief at G1, with no scripts", async () => {
    renderPanel(gateItem({ gate: "g1", sketch: { version: 1, findings: [] } }));
    const frame = await screen.findByTitle("Rough sketch of the main screens");
    expect(frame).toHaveAttribute("src", "/api/features/CLN-1/sketch");
    expect(frame).toHaveAttribute("sandbox", "");
    expect(screen.getByText(/It isn.t the design/)).toBeInTheDocument();
  });

  it("lets people walk through the prototype at G3, starting at any screen", async () => {
    const user = userEvent.setup();
    renderPanel(
      gateItem({
        gate: "g3",
        mockups: {
          prototype: true,
          screens: [
            { id: "S1", name: "Visit list", app: "web", phone: false, today: { desktop: true, phone: true } },
            { id: "S2", name: "New visit", app: "web", phone: false, today: null, new: true },
          ],
          findings: [],
        },
        screens: {
          scanned: false,
          state: "not_run",
          prototype: true,
          screens: [{ id: "S1", name: "Visit list", app: "web", violations: [] }],
        },
      }),
    );
    expect(await screen.findByText("Try the flow")).toBeInTheDocument();
    const frame = screen.getByTitle("Prototype, starting at S1: Visit list");
    expect(frame).toHaveAttribute("src", "/api/features/CLN-1/prototype#S1");
    expect(frame).toHaveAttribute("sandbox", "allow-scripts"); // never allow-same-origin
    expect(screen.getByRole("tab", { name: /S1 Visit list/ })).toHaveTextContent("Changed");
    expect(screen.getByRole("tab", { name: /S2 New visit/ })).toHaveTextContent("New");
    await user.click(screen.getByRole("button", { name: "Today" }));
    expect(screen.getByAltText("S1 Visit list as it is today, in staging")).toHaveAttribute(
      "src",
      "/api/features/CLN-1/today/S1/desktop",
    );
    await user.click(screen.getByRole("button", { name: "With this change" }));
    expect(screen.getByTitle("Prototype, starting at S1: Visit list")).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: /S2 New visit/ }));
    expect(screen.getByTitle("Prototype, starting at S2: New visit")).toHaveAttribute(
      "src",
      "/api/features/CLN-1/prototype#S2",
    );
    expect(screen.getByText(/Click through it as a user would/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Today" })).not.toBeInTheDocument(); // S2 is new: nothing to compare
    expect(screen.getByTitle("Approved prototype at S1")).toHaveAttribute("sandbox", "allow-scripts");
  });

  it("notes when no staging scan ran", () => {
    renderPanel(gateItem({ gate: "g6", screens: { scanned: false, state: "not_run", screens: [] } }));
    expect(screen.getByText(/No staging scan reported for these screens/)).toBeInTheDocument();
  });

  it("shows the AWS services approval also creates", async () => {
    renderPanel(
      gateItem({
        gate: "g6",
        aws_resources: {
          resources: [
            {
              name: "visits-db",
              type: "database",
              service: "RDS",
              stateful: true,
              parameters: {},
              access: { app: ["read", "write"] },
              sizes: { dev: "small", production: "large" },
              monthly_usd: { dev: 10, production: 100 },
              alarms: [{ name: "cpu-high", severity: "high", says: "CPU above 80%" }],
            },
          ],
          monthly_usd: { dev: 10, production: 100 },
          region: "us-east-1",
          accounts: {},
          note: "Planning figures.",
        },
        alarms_firing: [
          {
            env: "staging",
            resource: "visits-db",
            alarm: "cpu",
            severity: "high",
            says: "CPU above 90%",
            since: "2026-11-01T00:00:00Z",
            url: "https://cloud.example/cpu",
            title: "High CPU",
          },
        ],
      }),
    );
    expect(
      await screen.findByText("Approving also creates these AWS services in production"),
    ).toBeInTheDocument();
    expect(screen.getByText("visits-db")).toBeInTheDocument();
    expect(screen.getByText("Alarms firing before production")).toBeInTheDocument();
    expect(screen.getByText("High CPU")).toBeInTheDocument();
    expect(screen.getByText(/Alarms that come with them/)).toBeInTheDocument();
  });
});

describe("incident decisions", () => {
  const incidentItem = (over: Partial<PendingItem> = {}) =>
    gateItem({
      gate: "incident",
      severity: "sev1",
      firing: 2,
      actions: [
        { id: "a1", type: "flag_off", text: "Turn the export flag off" },
        { id: "a2", type: "rollback", text: "Roll back to the previous release" },
      ],
      signals: [
        {
          source: "sentry",
          status: "firing",
          severity: "high",
          title: "Export errors",
          detail: "",
          fingerprint: "fp1",
          url: "",
          at: "2026-11-01T00:00:00Z",
          component: "export",
        },
      ],
      suspects: [
        {
          feature_id: "CLN-3",
          title: "Export v2",
          released_at: "2026-11-01",
          flag: "export-v2",
          flag_on: true,
        },
      ],
      ...over,
    });

  it("defaults to all proposed actions and approves the chosen ones", async () => {
    const user = userEvent.setup();
    renderPanel(incidentItem());
    expect(await screen.findByText("Proposed actions")).toBeInTheDocument();
    expect(screen.getByText("SEV1")).toBeInTheDocument();
    expect(screen.getByText("2 alerts or issues")).toBeInTheDocument();
    expect(screen.getByText("Export errors")).toBeInTheDocument();
    await user.click(screen.getByLabelText(/Roll back to the previous release/));
    expect(screen.getByRole("button", { name: "Approve 1 action" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Approve 1 action" }));
    expect(apiMock.decide).toHaveBeenCalledWith(
      "CLN-1",
      "int-1",
      expect.objectContaining({ decision: "approve", action_ids: ["a1"] }),
    );
  });

  it("disables approval when no action is picked", async () => {
    const user = userEvent.setup();
    renderPanel(incidentItem());
    await user.click(await screen.findByLabelText(/Turn the export flag off/));
    await user.click(screen.getByLabelText(/Roll back to the previous release/));
    expect(screen.getByRole("button", { name: "Approve 0 actions" })).toBeDisabled();
  });

  it("asks triage to look again and closes without action", async () => {
    const user = userEvent.setup();
    renderPanel(incidentItem());
    await user.click(await screen.findByRole("button", { name: "Look again" }));
    expect(screen.getByLabelText("What should triage look at?")).toBeInTheDocument();
    await user.type(screen.getByLabelText("What should triage look at?"), "Check the queue");
    await user.click(screen.getByRole("button", { name: "Send back with changes" }));
    expect(apiMock.decide).toHaveBeenCalledWith(
      "CLN-1",
      "int-1",
      expect.objectContaining({ decision: "revise", feedback: "Check the queue" }),
    );
    expect(screen.queryByRole("button", { name: "No action needed" })).not.toBeInTheDocument();
  });

  it("closes without action through the abort path", async () => {
    const user = userEvent.setup();
    renderPanel(incidentItem());
    await user.click(await screen.findByRole("button", { name: "No action needed" }));
    expect(screen.getByLabelText("Why is no action needed?")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Close without action" }));
    expect(apiMock.decide).toHaveBeenCalledWith(
      "CLN-1",
      "int-1",
      expect.objectContaining({ decision: "resolve" }),
    );
  });

  it("says so when no actions are proposed", () => {
    renderPanel(incidentItem({ actions: [] }));
    expect(screen.getByText("None proposed.")).toBeInTheDocument();
  });
});

describe("initiative review decisions", () => {
  const reviewItem = (over: Partial<PendingItem> = {}) =>
    gateItem({
      gate: "review",
      next_review: "2026-12-01",
      retire_candidates: [
        { feature_id: "CLN-2", title: "Old export", flag: "old-export", why: "Superseded by the new one" },
        { feature_id: "CLN-3", title: "Legacy import", flag: "legacy", why: "No longer used" },
      ],
      preview: "## Evidence\n\nNo-shows fell 10%.",
      ...over,
    });

  it("continues to the next review date", async () => {
    const user = userEvent.setup();
    renderPanel(reviewItem());
    expect(
      await screen.findByRole("heading", { name: "Is this initiative moving its goal?" }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Continue" }));
    const date = screen.getByLabelText("Next review");
    expect(date).toHaveValue("2026-12-01");
    await user.clear(date);
    await user.type(date, "2027-01-15");
    await user.click(screen.getByRole("button", { name: "Continue to 2027-01-15" }));
    expect(apiMock.decide).toHaveBeenCalledWith(
      "CLN-1",
      "int-1",
      expect.objectContaining({ decision: "continue", review_by: "2027-01-15" }),
    );
  });

  it("changes course only with feedback", async () => {
    const user = userEvent.setup();
    renderPanel(reviewItem());
    await user.click(screen.getByRole("button", { name: "Change course" }));
    const send = screen.getByRole("button", { name: "Open the change as an intent" });
    expect(send).toBeDisabled();
    await user.type(
      screen.getByLabelText("What should change? (it becomes the new intent's request)"),
      "Aim at a different goal",
    );
    await user.click(send);
    expect(apiMock.decide).toHaveBeenCalledWith(
      "CLN-1",
      "int-1",
      expect.objectContaining({ decision: "change", feedback: "Aim at a different goal" }),
    );
  });

  it("stops and retires picked features", async () => {
    const user = userEvent.setup();
    renderPanel(reviewItem());
    await user.click(screen.getByRole("button", { name: "Stop the initiative" }));
    expect(screen.getByText("Retire these features too (each opens a retire intent)")).toBeInTheDocument();
    await user.click(screen.getByLabelText(/Old export/));
    expect(screen.getByRole("button", { name: "Stop and retire 1 feature" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Stop and retire 1 feature" }));
    expect(apiMock.decide).toHaveBeenCalledWith(
      "CLN-1",
      "int-1",
      expect.objectContaining({ decision: "stop", retire: ["CLN-2"] }),
    );
  });

  it("stops while keeping features when none are retired", async () => {
    const user = userEvent.setup();
    renderPanel(reviewItem());
    await user.click(screen.getByRole("button", { name: "Stop the initiative" }));
    await user.click(screen.getByRole("button", { name: "Stop, keep its features" }));
    expect(apiMock.decide).toHaveBeenCalledWith(
      "CLN-1",
      "int-1",
      expect.objectContaining({ decision: "stop", retire: [] }),
    );
  });
});

describe("how it ships (G1), launch flags (G4) and release readiness", () => {
  const releases = [
    {
      id: "FARM-REL-1",
      title: "Spring season",
      model: "launch" as const,
      target_date: "2027-03-01",
      initiative: "FARM-INI-1",
    },
  ];

  it("asks at G1 and sends the change with the approval", async () => {
    const user = userEvent.setup();
    renderPanel(
      gateItem({
        gate: "g1",
        ship: { current: { release_id: "FARM-REL-1", why: "With FARM-INI-1's release (launch)" }, releases },
      }),
    );
    const select = await screen.findByLabelText("When it's approved at G6");
    expect(select).toHaveValue("FARM-REL-1");
    expect(screen.getByText(/With FARM-INI-1's release \(launch\). It can change until/)).toBeInTheDocument();
    await user.selectOptions(select, "");
    await user.click(screen.getByRole("button", { name: "Approve the idea" }));
    expect(apiMock.decide).toHaveBeenCalledWith(
      "CLN-1",
      "int-1",
      expect.objectContaining({ decision: "approve", ship: "" }),
    );
  });

  it("sends nothing about shipping when it's untouched", async () => {
    const user = userEvent.setup();
    renderPanel(gateItem({ gate: "g1", ship: { current: { own: true, why: "On its own" }, releases } }));
    await user.click(await screen.findByRole("button", { name: "Approve the idea" }));
    const body = apiMock.decide.mock.calls[0][2] as Record<string, unknown>;
    expect(body).not.toHaveProperty("ship");
  });

  it("warns at G4 when a launch feature won't have a flag", async () => {
    renderPanel(
      gateItem({ gate: "g4", flag_warning: "This feature is in launch FARM-REL-1, ... no flag service" }),
    );
    expect(await screen.findByRole("status")).toHaveTextContent("no flag service");
  });

  it("shows the release's readiness at its gate", async () => {
    renderPanel(
      gateItem({
        gate: "release",
        readiness: {
          release_id: "FARM-REL-1",
          model: "launch",
          target_date: "2027-03-01",
          status: "ready",
          counts: { ready: 6, checking: 0, building: 2, design: 1, not_started: 1, stopped: 0 },
          total: 10,
          ready: 6,
          features: [
            {
              feature_id: "FARM-FEAT-14",
              title: "Soil sensor import",
              group: "building",
              stage: "build",
              must: true,
            },
          ],
          not_started: [{ title: "Harvest export", must: false }],
          must: { total: 5, ready: 4, missing: ["FARM-FEAT-14"] },
          says: "6 of 10 ready; 2 building; 1 in design; 1 not started; must-haves 4 of 5 ready",
        },
      }),
    );
    expect(await screen.findByRole("heading", { name: /6 of 10 ready/ })).toBeInTheDocument();
    expect(screen.getByText("Must-haves not ready:")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "FARM-FEAT-14" })).toBeInTheDocument();
    expect(screen.getByText("Harvest export")).toBeInTheDocument();
  });

  const due: ReleaseReadiness = {
    release_id: "FARM-REL-1",
    model: "train",
    target_date: "2026-11-20",
    status: "planned",
    counts: { ready: 1, checking: 0, building: 1, design: 0, not_started: 0, stopped: 0 },
    total: 2,
    ready: 1,
    features: [],
    not_started: [],
    must: { total: 0, ready: 0, missing: [] },
    says: "1 of 2 ready; 1 building",
  };

  it("on a release's date, ships what's ready or moves the date", async () => {
    const user = userEvent.setup();
    renderPanel(gateItem({ gate: "release_date", allowed: ["ship_ready", "move_date"], readiness: due }));
    await user.click(await screen.findByRole("button", { name: "Ship what's ready" }));
    expect(apiMock.decide).toHaveBeenCalledWith(
      "CLN-1",
      "int-1",
      expect.objectContaining({ decision: "ship_ready" }),
    );
  });

  it("moves the date when a must-have isn't ready", async () => {
    const user = userEvent.setup();
    renderPanel(
      gateItem({
        gate: "release_date",
        allowed: ["move_date"],
        readiness: { ...due, must: { total: 1, ready: 0, missing: ["FARM-FEAT-14"] } },
      }),
    );
    expect(await screen.findByText(/a must-have isn't ready/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Ship what's ready" })).not.toBeInTheDocument();
    const move = screen.getByRole("button", { name: "Move the date" });
    expect(move).toBeDisabled();
    await user.type(screen.getByLabelText("New date"), "2026-12-15");
    await user.click(move);
    expect(apiMock.decide).toHaveBeenCalledWith(
      "CLN-1",
      "int-1",
      expect.objectContaining({ decision: "move_date", target_date: "2026-12-15" }),
    );
  });
});

describe("inception's launch", () => {
  const launch = {
    initiative: "INI-1",
    title: "First alerts",
    features: ["Moisture alerts", "Field map"],
    must: ["Moisture alerts", "Field map"],
    target_date: "",
  };

  it("approves with the date and the must-haves chosen", async () => {
    const user = userEvent.setup();
    renderPanel(gateItem({ gate: "product", launch }));
    expect(await screen.findByText("The launch: First alerts")).toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: "Field map" }));
    await user.type(screen.getByLabelText("Launch date (optional)"), "2027-02-01");
    await user.click(screen.getAllByRole("button", { name: /Approve/ })[0]);
    expect(apiMock.decide).toHaveBeenCalledWith(
      "CLN-1",
      "int-1",
      expect.objectContaining({ decision: "approve", target_date: "2027-02-01", must: ["Moisture alerts"] }),
    );
  });

  it("leaves them as proposed when untouched", async () => {
    const user = userEvent.setup();
    renderPanel(gateItem({ gate: "product", launch }));
    await user.click((await screen.findAllByRole("button", { name: /Approve/ }))[0]);
    const sent = apiMock.decide.mock.calls.at(-1)![2];
    expect(sent).not.toHaveProperty("must");
    expect(sent).not.toHaveProperty("target_date");
  });
});

describe("G2 UX toggle", () => {
  it("sends ui=false when the design is switched off", async () => {
    const user = userEvent.setup();
    renderPanel(gateItem({ gate: "g2", ux_design: { runs: true, why: "Changes the visits list" } }));
    const box = await screen.findByLabelText(/Design the screens/);
    expect(box).toBeChecked();
    expect(
      screen.getByText(/Runs: the feature changes what people see. Changes the visits list./),
    ).toBeInTheDocument();
    await user.click(box);
    await user.click(screen.getByRole("button", { name: "Approve the scope" }));
    expect(apiMock.decide).toHaveBeenCalledWith(
      "CLN-1",
      "int-1",
      expect.objectContaining({ decision: "approve", ui: false }),
    );
  });

  it("sends no ui flag when the toggle is untouched", async () => {
    const user = userEvent.setup();
    renderPanel(gateItem({ gate: "g2", ux_design: { runs: false, why: "" } }));
    await user.click(await screen.findByRole("button", { name: "Approve the scope" }));
    const body = apiMock.decide.mock.calls[0][2] as Record<string, unknown>;
    expect(body).not.toHaveProperty("ui");
  });
});

describe("gate confidence", () => {
  it("shows how far the product's history predicts this approval", async () => {
    renderPanel(
      gateItem({
        gate: "g2",
        confidence: {
          level: "high",
          decisions: 42,
          held: 41,
          lower_bound: 0.9,
          checks: [],
          says: "High confidence: 41 of 42 specs like this were approved first time and kept (at least 90%).",
        },
      }),
    );
    expect(await screen.findByText("High confidence")).toBeInTheDocument();
    expect(screen.getByText(/41 of 42 specs like this/)).toBeInTheDocument();
  });

  it("says nothing without a score", async () => {
    renderPanel(gateItem({ gate: "g2" }));
    await screen.findAllByRole("heading");
    expect(screen.queryByText(/confidence/i)).not.toBeInTheDocument();
  });
});

describe("safe mode", () => {
  it("says why every gate is read in full, and what the second critic said", async () => {
    renderPanel(
      gateItem({
        gate: "g2",
        safe_mode: {
          on: true,
          since: "2026-11-18T09:00:00+00:00",
          says: "Safe mode since 2026-11-18 09:00 UTC: The spec critic passed 50% of documents people then sent back.",
          phases: ["spec"],
          normal_checks: 1,
          needed: 2,
          fix: { at: "2026-11-19T10:00:00+00:00", by: "sam", note: "Pinned the previous model" },
        },
        second_critic: {
          model: "anthropic/claude-sonnet-5-5",
          agrees: false,
          passed: false,
          says: "The second critic failed it: the critic didn't.",
        },
      }),
    );
    expect(await screen.findByText("Safe mode.")).toBeInTheDocument();
    expect(screen.getByText(/Pinned the previous model/)).toBeInTheDocument();
    expect(screen.getByText(/1 of 2 normal checks/)).toBeInTheDocument();
    expect(screen.getByText("Second critic disagrees")).toBeInTheDocument();
  });
});

describe("escalations", () => {
  it("reopens the document build found wrong, with the evidence shown", async () => {
    const user = userEvent.setup();
    renderPanel(
      gateItem({
        type: "escalation",
        phase: "build",
        reason: "The architecture needs changing (coder on T1): the record doesn't exist yet",
        allowed: ["resolved", "retry", "abort", "spec", "design", "architecture"],
        design_issues: [
          {
            task: "T1",
            by: "coder",
            document: "architecture",
            section: "## Endpoints",
            problem: "the record doesn't exist until the import finishes",
            evidence: "404 before the import job runs",
          },
        ],
      }),
    );
    expect(await screen.findByText(/404 before the import job runs \(coder on T1\)/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reopen the spec" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Reopen the architecture" }));
    expect(apiMock.decide).toHaveBeenCalledWith(
      "CLN-1",
      "int-1",
      expect.objectContaining({ decision: "architecture" }),
    );
  });

  it("offers no reopen without a design issue", async () => {
    renderPanel(gateItem({ type: "escalation", phase: "build", reason: "CI failed" }));
    await screen.findByRole("button", { name: "Let agents retry" });
    expect(screen.queryByRole("button", { name: /^Reopen/ })).not.toBeInTheDocument();
  });

  it("retries a generic escalation with a raised budget", async () => {
    const user = userEvent.setup();
    renderPanel(
      gateItem({ type: "escalation", phase: "plan", reason: "The planner is stuck on the budget" }),
    );
    expect(await screen.findByRole("heading", { name: "Plan needs a person" })).toBeInTheDocument();
    expect(screen.getByText("The planner is stuck on the budget")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Let agents retry" }));
    await user.type(screen.getByLabelText("New budget in dollars (optional)"), "25");
    await user.click(screen.getByRole("button", { name: "Retry with agents" }));
    expect(apiMock.decide).toHaveBeenCalledWith(
      "CLN-1",
      "int-1",
      expect.objectContaining({ decision: "retry", budget_usd: 25 }),
    );
  });

  it("resolves a delivery failure by carrying on", async () => {
    const user = userEvent.setup();
    renderPanel(gateItem({ type: "escalation", phase: "delivery", wait: "build" }));
    expect(await screen.findByRole("heading", { name: "The OpenChoreo build failed" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Carry on anyway" }));
    expect(apiMock.decide).toHaveBeenCalledWith(
      "CLN-1",
      "int-1",
      expect.objectContaining({ decision: "resolved" }),
    );
    expect(screen.getByRole("button", { name: "Rebuild" })).toBeInTheDocument();
  });

  it("names the failed delivery step for a change set", () => {
    renderPanel(gateItem({ type: "escalation", phase: "delivery", wait: "change_set" }));
    expect(
      screen.getByRole("heading", { name: "The change set failed in its own environment" }),
    ).toBeInTheDocument();
  });

  it("names a failed repository setup", () => {
    renderPanel(
      gateItem({ type: "escalation", phase: "provisioning", reason: "GitHub App install missing" }),
    );
    expect(screen.getByRole("heading", { name: "Setting up a new repository failed" })).toBeInTheDocument();
  });

  it("accepts a security risk with a waiver reason and expiry", async () => {
    const user = userEvent.setup();
    renderPanel(
      gateItem({
        type: "escalation",
        phase: "security",
        stage: "pr",
        max_waiver_days: 30,
        blocking: [
          {
            suite: "secrets",
            tool: "gitleaks",
            rule: "generic-api-key",
            severity: "high",
            title: "API key in code",
            repo: "org/repo",
            url: "https://example.invalid/finding",
            fingerprint: "fp-1",
          },
        ],
      }),
    );
    expect(
      await screen.findByRole("heading", { name: "Security findings block this change" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Blocking findings")).toBeInTheDocument();
    expect(screen.getByText("API key in code")).toBeInTheDocument();
    expect(
      screen.getByText(/Accepting the risk records a waiver with your reason, valid for up to 30 days/),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Have agents fix them" }));
    expect(apiMock.decide).toHaveBeenCalledWith(
      "CLN-1",
      "int-1",
      expect.objectContaining({ decision: "retry" }),
    );
    await user.click(screen.getByRole("button", { name: "Accept the risk" }));
    const send = screen.getByRole("button", { name: "Accept the risk" });
    expect(send).toBeDisabled();
    await user.type(screen.getByLabelText(/Why is this risk acceptable?/), "Key is a test key");
    await user.type(screen.getByLabelText("Valid for how many days"), "14");
    await user.click(screen.getByRole("button", { name: "Accept the risk" }));
    expect(apiMock.decide).toHaveBeenCalledWith(
      "CLN-1",
      "int-1",
      expect.objectContaining({ decision: "accept_risk", feedback: "Key is a test key", expires_days: 14 }),
    );
  });

  it("offers to scan again when findings are not on a PR", () => {
    renderPanel(gateItem({ type: "escalation", phase: "security", stage: "dev", blocking: [] }));
    expect(screen.getByRole("button", { name: "Scan again" })).toBeInTheDocument();
  });

  it("finishes an escalation with the person's own version of the document", async () => {
    const user = userEvent.setup();
    renderPanel(
      gateItem({
        type: "escalation",
        phase: "plan",
        content: "# Plan\n\nDraft plan.",
        artifact: { id: "PLAN", kind: "PLAN" },
      }),
    );
    await user.click(await screen.findByRole("button", { name: "Finish it myself" }));
    const box = screen.getByLabelText(/Finish the plan/);
    await user.clear(box);
    await user.type(box, "# Plan\n\nMy finished plan.");
    await user.click(screen.getByRole("button", { name: "Resolve with my version" }));
    const body = apiMock.decide.mock.calls[0][2] as Record<string, unknown>;
    expect(body.decision).toBe("resolved");
    expect(body.artifact_content).toBe("# Plan\n\nMy finished plan.");
  });
});

describe("handback", () => {
  it("hands back an edited document", async () => {
    const user = userEvent.setup();
    renderPanel(
      gateItem({
        type: "handback",
        gate: "g1",
        content: "# Idea\n\nDraft.",
        artifact: { id: "OPP", kind: "OPP" },
      }),
    );
    expect(
      await screen.findByRole("heading", { name: "You took over Idea approval (G1)" }),
    ).toBeInTheDocument();
    const box = screen.getByLabelText("Opportunity brief");
    await user.clear(box);
    await user.type(box, "# Idea\n\nMy version.");
    await user.click(screen.getByRole("button", { name: "Hand back with my version" }));
    expect(apiMock.decide).toHaveBeenCalledWith(
      "CLN-1",
      "int-1",
      expect.objectContaining({ decision: "handback", artifact_content: "# Idea\n\nMy version." }),
    );
  });

  it("hands back unchanged work without sending content", async () => {
    const user = userEvent.setup();
    renderPanel(
      gateItem({
        type: "handback",
        gate: "g1",
        content: "# Idea\n\nDraft.",
        artifact: { id: "OPP", kind: "OPP" },
      }),
    );
    await user.click(await screen.findByRole("button", { name: "Hand back to agents" }));
    const body = apiMock.decide.mock.calls[0][2] as Record<string, unknown>;
    expect(body.decision).toBe("handback");
    expect(body).not.toHaveProperty("artifact_content");
  });

  it("explains a handback with no document to edit", async () => {
    const user = userEvent.setup();
    renderPanel(gateItem({ type: "handback", gate: "g2" }));
    expect(screen.getByText(/Finish the work yourself, then hand it back/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Hand back to agents" })).toBeInTheDocument();
    await user.type(screen.getByLabelText(/What did you change, and why?/), "Rewrote the intro");
    await user.click(screen.getByRole("button", { name: "Hand back to agents" }));
    expect(apiMock.decide).toHaveBeenCalledWith(
      "CLN-1",
      "int-1",
      expect.objectContaining({ decision: "handback", feedback: "Rewrote the intro" }),
    );
  });
});

describe("fit check", () => {
  const fitItem = () =>
    gateItem({
      phase: "fit",
      fit: [
        { to: "design", text: "The design adds a field the architecture drops" },
        { to: "architecture", text: "The architecture stores visits the design never shows" },
      ],
    });

  it("sends one side back or carries on with a reason", async () => {
    const user = userEvent.setup();
    renderPanel(fitItem());
    expect(
      await screen.findByRole("heading", { name: "The design and the architecture don't fit together" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Design should change")).toBeInTheDocument();
    expect(screen.getByText("Architecture should change")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Send the design back" }));
    expect(apiMock.decide).toHaveBeenCalledWith(
      "CLN-1",
      "int-1",
      expect.objectContaining({ decision: "design" }),
    );
    await user.click(screen.getByRole("button", { name: "Send the architecture back" }));
    expect(apiMock.decide).toHaveBeenCalledWith(
      "CLN-1",
      "int-1",
      expect.objectContaining({ decision: "architecture" }),
    );
    await user.click(screen.getByRole("button", { name: "Carry on anyway" }));
    const send = screen.getByRole("button", { name: "Carry on to planning" });
    expect(send).toBeDisabled();
    await user.type(screen.getByLabelText(/Why carry on with them as they are?/), "The field is cosmetic");
    await user.click(send);
    expect(apiMock.decide).toHaveBeenCalledWith(
      "CLN-1",
      "int-1",
      expect.objectContaining({ decision: "resolved", feedback: "The field is cosmetic" }),
    );
  });
});

describe("who can decide", () => {
  it("shows the waiting note when the person cannot decide", () => {
    renderPanel(gateItem({ can_decide: false, approvers: ["ada", "bo"] }));
    expect(
      screen.getByText(/Waiting on ada, bo\. You can follow along, but only they can decide\./),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve the idea" })).not.toBeInTheDocument();
  });

  it("shows the passkey prompt when a passkey sign-in is needed", () => {
    renderPanel(gateItem({ can_decide: false, needs_passkey: true }));
    expect(
      screen.getByText(/You approve this through your workspace group, which needs a passkey sign-in/),
    ).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "Sign in with your passkey" });
    expect(link).toHaveAttribute("href", "/api/auth/login?provider=passkey&next=%2F");
    expect(screen.getByRole("link", { name: "add one" })).toHaveAttribute("href", "/api/auth/passkey");
  });

  it("names each lane in turn for a multi-lane gate", () => {
    renderPanel(gateItem({ lanes: ["CLN", "PAY"], lane: "CLN" }));
    expect(
      screen.getByText(
        "Idea approval (G1), decided by each lane in turn: CLN then PAY. This is CLN's decision.",
      ),
    ).toBeInTheDocument();
  });

  it("disables the UX toggle when the person cannot decide", () => {
    renderPanel(gateItem({ gate: "g2", can_decide: false, ux_design: { runs: true, why: "" } }));
    expect(screen.getByLabelText(/Design the screens/)).toBeDisabled();
  });
});

describe("roadmap gates, initiative and release detail", () => {
  it("renders the initiative being approved at G0 and drops it by name", async () => {
    renderPanel(
      gateItem({
        gate: "g0",
        initiative: {
          goal: "Cut no-shows",
          new_goal: null,
          measure: "No-show rate under 5%",
          horizon: "now",
          budget_usd: 5000,
          lanes: ["CLN"],
          release_model: "launch",
          candidates: [{ title: "Reminder texts", description: "Text patients a day ahead" }],
        },
      }),
    );
    expect(await screen.findByText("The initiative")).toBeInTheDocument();
    expect(screen.getByText("Cut no-shows")).toBeInTheDocument();
    expect(screen.getByText("Reminder texts")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Drop initiative" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Take over" })).not.toBeInTheDocument();
  });

  it("renders a new goal when the initiative proposes one", () => {
    renderPanel(
      gateItem({
        gate: "g0",
        initiative: {
          goal: "",
          new_goal: { title: "Fewer no-shows", measure: "under 5%" },
          measure: "",
          horizon: "next",
          budget_usd: 1000,
          lanes: ["CLN"],
          release_model: "train",
          candidates: [],
        },
      }),
    );
    expect(screen.getByText("New: Fewer no-shows (measure: under 5%)")).toBeInTheDocument();
    expect(screen.getByText("None")).toBeInTheDocument();
  });

  it("launches a release group and ships a release train", async () => {
    const user = userEvent.setup();
    renderPanel(
      gateItem({
        gate: "release",
        model: "launch",
        target_date: "2026-12-15",
        features: ["CLN-1", "CLN-2"],
        dropped: ["CLN-3"],
        evidence: "All features passed staging.",
      }),
    );
    expect(
      await screen.findByRole("heading", { name: "Launch these features together?" }),
    ).toBeInTheDocument();
    expect(screen.getByText("2026-12-15")).toBeInTheDocument();
    expect(screen.getByText("CLN-3 (stopped before the release)")).toBeInTheDocument();
    expect(screen.getByText("All features passed staging.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Request changes" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Launch" }));
    expect(apiMock.decide).toHaveBeenCalledWith(
      "CLN-1",
      "int-1",
      expect.objectContaining({ decision: "approve" }),
    );
  });

  it("asks to ship a release train for the train model", () => {
    renderPanel(gateItem({ gate: "release", model: "train", features: [] }));
    expect(screen.getByRole("heading", { name: "Ship this release train?" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve release" })).toBeInTheDocument();
  });

  it("renders the product brief and capacity at the product gate", async () => {
    renderPanel(
      gateItem({
        gate: "product",
        preview: "## Architecture\n\nTwo services.",
        monthly_usd: { dev: 20, production: 200 },
        capacity: {
          tier: 2,
          stages: {
            pilot: { monthly_usd: 30, monthly_usd_max: 30 },
            growth: { monthly_usd: 90, monthly_usd_max: 90 },
            scale: { monthly_usd: 0, monthly_usd_max: 400 },
          },
          fixed: { monthly_usd: 15, unpriced: ["search"] },
          findings: [],
        },
      }),
    );
    expect(await screen.findByText("Product architecture and roadmap")).toBeInTheDocument();
    expect(screen.getByText(/dev \$20\.00, production \$200\.00/)).toBeInTheDocument();
    expect(screen.getByText(/Production at target tier 2, per month:/)).toBeInTheDocument();
    expect(screen.getByText(/not yet priced: search/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve and set up" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop this product" })).toBeInTheDocument();
  });

  it("renders the brief gate preview", async () => {
    renderPanel(gateItem({ gate: "brief", preview: "## Brief\n\nA clinic product." }));
    expect(await screen.findByText("Product brief")).toBeInTheDocument();
    expect(screen.getByText("A clinic product.")).toBeInTheDocument();
  });

  it("notes the express profile and shows its spec preview", async () => {
    renderPanel(
      gateItem({
        gate: "g4",
        profile: "express",
        spec_preview: "# Spec\n\nSmall change.",
      }),
    );
    expect(await screen.findByText(/Express workflow: A small, low-risk change/)).toBeInTheDocument();
    const summary = screen.getByText(
      "The spec you approve with this design (express: no separate spec gate)",
    );
    expect(summary).toBeInTheDocument();
    expect(screen.getByText("Small change.")).toBeInTheDocument();
  });

  it("shows the refused reason when asked again", () => {
    renderPanel(gateItem({ refused: "The council asked for more evidence." }));
    expect(screen.getByRole("alert")).toHaveTextContent("The council asked for more evidence.");
  });
});

describe("worth knowing and checks sections", () => {
  it("shows roadmap fit with an over-budget chip", async () => {
    renderPanel(
      gateItem({
        roadmap_fit: {
          initiative: {
            id: "CLN-INI-1",
            title: "Cut no-shows",
            goal: "",
            horizon: "now",
            release_model: "launch",
          },
          budget_usd: 1000,
          spent_usd: 1200,
          over: true,
        },
      }),
    );
    expect(await screen.findByText("Roadmap")).toBeInTheDocument();
    expect(screen.getByText("Over budget")).toBeInTheDocument();
    expect(screen.getByText(/\$1200\.00 spent of \$1000\.00/)).toBeInTheDocument();
  });

  it("says when the work is unplanned", () => {
    renderPanel(gateItem({ roadmap_fit: { unplanned: true } }));
    expect(
      screen.getByText(/Unplanned work: this feature isn't part of an approved initiative/),
    ).toBeInTheDocument();
  });

  it("lists related work and the creator's not-a-duplicate reason", async () => {
    renderPanel(
      gateItem({
        related_features: [
          {
            kind: "feature",
            id: "CLN-2",
            title: "CSV export v1",
            status: "released",
            stage: "",
            score: 0.9,
            duplicate: true,
          },
        ],
        not_duplicate_reason: "This one covers clinics, not hospitals.",
      }),
    );
    expect(await screen.findByText("Already in this product")).toBeInTheDocument();
    expect(
      screen.getByText("The creator says it's different: This one covers clinics, not hospitals."),
    ).toBeInTheDocument();
    expect(screen.getByText("Likely the same")).toBeInTheDocument();
  });

  it("shows the design review against the existing design", async () => {
    renderPanel(
      gateItem({
        design_review: {
          findings: ["Adds a new table."],
          builds_on: [{ id: "ADR-1", title: "Use Postgres", status: "accepted" }],
          supersedes: [],
          contracts: [{ repo: "org/api", current: "v1", next: "v2", breaking: true, note: "" }],
          related: [],
          in_flight: [],
          record: { repo: "org/arch", sha: "abcdef123456" },
          errors: {},
        },
      }),
    );
    expect(await screen.findByText("Against the existing design")).toBeInTheDocument();
    expect(screen.getByText("Adds a new table.")).toBeInTheDocument();
    expect(screen.getByText("Breaking")).toBeInTheDocument();
    expect(screen.getByText(/Read from org\/arch@abcdef1\./)).toBeInTheDocument();
  });

  it("shows quality suites, capacity changes, code read, performance and instrumentation", async () => {
    renderPanel(
      gateItem({
        quality: {
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
          waivers: [],
        },
        capacity_change: [{ repo: "svc", max_from: 2, max_to: 5, busy_from: 10, busy_to: 50, unit: "req" }],
        code_read: { checked: true, repos: { "org/svc": { sha: "abc1234def" } } },
        performance: [
          { repo: "org/svc", role: "service", level: "smoke", reasons: ["new endpoint"], soak: false },
        ],
        instrumentation: {
          checked: true,
          environment: "staging",
          fired: { signup: 3 },
          missing: [],
          events: [{ event: "signup", goals: ["G1"], repo: "org/svc" }],
          why: "",
        },
        pull_requests: ["org/svc#12"],
      }),
    );
    expect(await screen.findByText("Test and security evidence")).toBeInTheDocument();
    expect(screen.getByText("Capacity this design needs")).toBeInTheDocument();
    expect(screen.getByText("Capacity this design needs").parentElement).toHaveTextContent(
      "svc: up to 2 → 5 pods",
    );
    expect(screen.getByText("The code the architect read")).toBeInTheDocument();
    expect(screen.getByText("Performance tests")).toBeInTheDocument();
    expect(screen.getByText(/smoke test on the pull request/)).toBeInTheDocument();
    expect(screen.getByText("Goal measurement")).toBeInTheDocument();
    expect(screen.getByText("Fired 3 times")).toBeInTheDocument();
    expect(screen.getByText("Pull requests in this decision")).toBeInTheDocument();
    expect(screen.getByText("org/svc#12")).toBeInTheDocument();
  });

  it("says when the code was not read and instrumentation was not checked", async () => {
    renderPanel(
      gateItem({
        code_read: { checked: false },
        instrumentation: {
          checked: false,
          environment: "staging",
          fired: {},
          missing: [],
          events: [{ event: "signup", goals: ["G1"], repo: "org/svc" }],
          why: "no staging run yet",
        },
      }),
    );
    expect(
      await screen.findByText(/Not read: this runtime works from the architecture record/),
    ).toBeInTheDocument();
    expect(screen.getByText("Not checked: no staging run yet")).toBeInTheDocument();
  });

  it("shows new repos approved at G4", async () => {
    renderPanel(
      gateItem({
        gate: "g4",
        new_repos: [
          {
            repo: "org/visits-svc",
            role: "service",
            purpose: "Serves visits",
            template: "template-service",
            visibility: "private",
            ruleset: { required_approvals: 1, required_checks: ["ci"] },
            teams: { backend: "write" },
            security: { secret_scanning: true },
          },
        ],
      }),
    );
    expect(await screen.findByText("Approving also creates these repositories")).toBeInTheDocument();
    expect(screen.getByText("org/visits-svc")).toBeInTheDocument();
  });
});

describe("DecisionPanel goal measurement and resolve details", () => {
  it("warns when goal events never fired in staging", async () => {
    renderPanel(
      gateItem({
        instrumentation: {
          checked: true,
          environment: "staging",
          fired: {},
          missing: ["signup"],
          events: [{ event: "signup", goals: ["G1"], repo: "org/svc" }],
          why: "",
        },
      }),
    );
    expect(await screen.findByText("Goal measurement")).toBeInTheDocument();
    expect(
      screen.getByText(
        /Some events never fired in staging\. The goals they feed can't be measured after release until they do\./,
      ),
    ).toBeInTheDocument();
  });

  it("lets the person finish an escalation's document themselves", async () => {
    const user = userEvent.setup();
    renderPanel(
      gateItem({ type: "escalation", phase: "build", content: "# Draft spec\n\nThe export lists visits." }),
    );
    await user.click(await screen.findByRole("button", { name: "Finish it myself" }));
    expect(screen.getByLabelText(/Finish the .*the agents' last draft/)).toHaveValue(
      "# Draft spec\n\nThe export lists visits.",
    );
    await user.type(screen.getByLabelText(/What was wrong with it?/), "The intro is vague");
    expect(screen.getByLabelText(/What was wrong with it?/)).toHaveValue("The intro is vague");
  });
});

it("marks an escalation resolved without a document to finish", async () => {
  const user = userEvent.setup();
  renderPanel(gateItem({ type: "escalation", phase: "build" }));
  await user.click(await screen.findByRole("button", { name: "Mark resolved" }));
  expect(
    screen.getByLabelText(/Your finished .* \(optional: leave empty to keep the agents' last draft\)/),
  ).toBeInTheDocument();
});

it("comments on a mockup screen from the decision panel", async () => {
  const user = userEvent.setup();
  renderPanel(
    gateItem({
      gate: "g3",
      mockups: {
        screens: [{ id: "M1", name: "Visits list", app: "web", phone: false }],
        findings: ["The list needs a filter."],
      },
    }),
  );
  expect(await screen.findByText("The screens")).toBeInTheDocument();
  await user.type(screen.getByLabelText("Comment on this screen"), "Add the filter here");
  await user.click(screen.getByRole("button", { name: "Add comment" }));
  expect(screen.getByLabelText("Comment on this screen")).toHaveValue("");
});

it("stops the feature from a delivery escalation", async () => {
  const user = userEvent.setup();
  renderPanel(gateItem({ type: "escalation", phase: "delivery", wait: "build" }));
  await user.click(await screen.findByRole("button", { name: "Stop feature" }));
  expect(screen.getByLabelText(/Why stop this feature?/)).toBeInTheDocument();
});

it("stops the feature from a fit check", async () => {
  const user = userEvent.setup();
  renderPanel(
    gateItem({
      phase: "fit",
      fit: [{ to: "design", text: "The design adds a field the architecture drops" }],
    }),
  );
  // A fit check renders two "Stop feature" buttons (the gate action row
  // and the fit-check row, in that order); the second is the fit one.
  const stops = screen.getAllByRole("button", { name: "Stop feature" });
  expect(stops).toHaveLength(2);
  await user.click(stops[1]);
  expect(screen.getByLabelText(/Why stop this feature?/)).toBeInTheDocument();
});

it("stops the feature from the gate action row", async () => {
  const user = userEvent.setup();
  renderPanel(gateItem());
  await user.click(await screen.findByRole("button", { name: "Stop feature" }));
  expect(screen.getByLabelText(/Why stop this feature?/)).toBeInTheDocument();
});

it("stops the feature from a handback", async () => {
  const user = userEvent.setup();
  renderPanel(gateItem({ type: "handback", gate: "g2" }));
  await user.click(await screen.findByRole("button", { name: "Stop feature" }));
  expect(screen.getByLabelText(/Why stop this feature?/)).toBeInTheDocument();
});

it("stops the feature from a security escalation", async () => {
  const user = userEvent.setup();
  renderPanel(gateItem({ type: "escalation", phase: "security", stage: "dev", blocking: [] }));
  await user.click(await screen.findByRole("button", { name: "Stop feature" }));
  expect(screen.getByLabelText(/Why stop this feature?/)).toBeInTheDocument();
});

it("stops the feature from a non-delivery escalation", async () => {
  const user = userEvent.setup();
  renderPanel(gateItem({ type: "escalation", phase: "build" }));
  await user.click(await screen.findByRole("button", { name: "Stop feature" }));
  expect(screen.getByLabelText(/Why stop this feature?/)).toBeInTheDocument();
});

it("retries a failed delivery step", async () => {
  const user = userEvent.setup();
  renderPanel(gateItem({ type: "escalation", phase: "delivery", wait: "build" }));
  await user.click(await screen.findByRole("button", { name: "Rebuild" }));
  expect(apiMock.decide).toHaveBeenCalledWith(
    "CLN-1",
    "int-1",
    expect.objectContaining({ decision: "retry" }),
  );
});

describe("defensive fallbacks for missing or malformed fields", () => {
  const review = (over: Partial<PendingItem> = {}) =>
    gateItem({
      gate: "review",
      next_review: "2026-12-01",
      retire_candidates: [
        { feature_id: "CLN-2", title: "Old export", flag: "old-export", why: "Superseded by the new one" },
        { feature_id: "CLN-3", title: "Legacy import", flag: "legacy", why: "No longer used" },
      ],
      preview: "## Evidence\n\nNo-shows fell 10%.",
      ...over,
    });

  const incident = (over: Partial<PendingItem> = {}) =>
    gateItem({
      gate: "incident",
      severity: "sev1",
      firing: 2,
      actions: [
        { id: "a1", type: "flag_off", text: "Turn the export flag off" },
        { id: "a2", type: "rollback", text: "Roll back to the previous release" },
      ],
      signals: [
        {
          source: "sentry",
          status: "firing",
          severity: "high",
          title: "Export errors",
          detail: "",
          fingerprint: "fp1",
          url: "",
          at: "2026-11-01T00:00:00Z",
          component: "export",
        },
      ],
      suspects: [
        {
          feature_id: "CLN-3",
          title: "Export v2",
          released_at: "2026-11-01",
          flag: "export-v2",
          flag_on: true,
        },
      ],
      ...over,
    });

  const renderItem = (item: PendingItem) =>
    render(
      <MemoryRouter>
        <DecisionPanel item={item} featureId="CLN-1" onDone={() => {}} />
      </MemoryRouter>,
    );

  it("renders a gate with no gate, title, or known risk tier as a plain Decision", async () => {
    renderPanel(
      gateItem({ gate: undefined, title: undefined, summary: "Covers clinics only.", risk_tier: "weird" }),
    );
    expect(await screen.findByRole("heading", { name: "Decision" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(screen.getByText("Covers clinics only.")).toBeInTheDocument();
    expect(screen.getByText("weird")).toBeInTheDocument();
  });

  it("names an unknown delivery wait and an unknown or missing phase", async () => {
    const { rerender } = renderItem(gateItem({ type: "escalation", phase: "delivery" }));
    expect(await screen.findByRole("heading", { name: "Delivery failed" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
    rerender(
      <MemoryRouter>
        <DecisionPanel
          item={gateItem({ type: "escalation", phase: "mystery" })}
          featureId="CLN-1"
          onDone={() => {}}
        />
      </MemoryRouter>,
    );
    expect(await screen.findByRole("heading", { name: "mystery needs a person" })).toBeInTheDocument();
    rerender(
      <MemoryRouter>
        <DecisionPanel item={gateItem({ type: "escalation" })} featureId="CLN-1" onDone={() => {}} />
      </MemoryRouter>,
    );
    expect(await screen.findByRole("heading", { name: "undefined needs a person" })).toBeInTheDocument();
  });

  it("renders agent notes with no repo, task, or phase", async () => {
    renderPanel(
      gateItem({
        agent_notes: [
          { kind: "changed", text: "Moved the export to a background job.", task: "Write spec" },
          { kind: "traded off", text: "Kept CSV only; no Excel." },
          { kind: "open", text: "Which columns?", phase: "" },
        ],
      }),
    );
    expect(await screen.findByText("The agents' notes")).toBeInTheDocument();
    expect(screen.getByText("Changed:")).toBeInTheDocument();
    expect(screen.getByText("Traded off:")).toBeInTheDocument();
    expect(screen.getByText(/\(Write spec, \)/)).toBeInTheDocument();
    expect(screen.getByText(/Which columns\?/).closest("li")).not.toHaveTextContent("(");
    expect(screen.queryByText(/Approving confirms what the agents assumed/)).not.toBeInTheDocument();
  });

  it("labels history entries the API does not know, with their raw reason", async () => {
    renderPanel(
      gateItem({
        history: [
          {
            gate: "g1",
            decision: "takeover",
            approver: "ada",
            reason: "",
            feedback: "",
            at: "2026-11-01T00:00:00Z",
          },
          {
            gate: "g1",
            decision: "handback",
            approver: "bo",
            reason: "bogus",
            feedback: "Took it back",
            at: "2026-11-02T00:00:00Z",
          },
          {
            gate: "g1",
            decision: "approve",
            approver: "cy",
            reason: "",
            feedback: "",
            at: "2026-11-03T00:00:00Z",
          },
        ],
      }),
    );
    expect(await screen.findByText("What people asked for before")).toBeInTheDocument();
    expect(screen.getByText("Taken over")).toBeInTheDocument();
    expect(screen.getByText("Handed back")).toBeInTheDocument();
    expect(screen.getByText("Resolved")).toBeInTheDocument();
    expect(screen.getByText(/by bo \(bogus\): Took it back/)).toBeInTheDocument();
  });

  it("renders a G0 initiative with no goal and unknown horizon and release model", async () => {
    renderPanel(
      gateItem({
        gate: "g0",
        initiative: {
          goal: "",
          new_goal: null,
          measure: "",
          horizon: "someday",
          budget_usd: 1000,
          lanes: ["CLN"],
          release_model: "solo" as unknown as "launch",
          candidates: [{ title: "Reminder texts", description: "" }],
        },
      }),
    );
    expect(await screen.findByText("The initiative")).toBeInTheDocument();
    expect(screen.getAllByText("None")).toHaveLength(2);
    expect(screen.getByText("someday")).toBeInTheDocument();
    expect(screen.getByText("solo")).toBeInTheDocument();
    expect(screen.getByText("Reminder texts").closest("li")).toHaveTextContent("Reminder texts");
  });

  it("shows release candidates that are all checked and have no package", async () => {
    renderPanel(
      gateItem({
        gate: "g6",
        packages: [
          { repo: "org/a", version: "1.0.0", rc: "v1.0.0-rc.1", status: "published", package: null },
          { repo: "org/b", version: "2.0.0", rc: "v2.0.0-rc.1", status: "unknown", package: null },
        ],
        agent_evals: [
          {
            repo: "org/knowledge",
            role: "knowledge",
            stages: {
              pr: { suite: "golden", passed: 5, failed: 0, rate: null, state: "passed" },
              staging: { suite: "golden", passed: 4, failed: 1, rate: 0.8, state: "passed" },
            },
            previous: { pr: { suite: "golden", passed: 5, failed: 0, rate: null, state: "passed" } },
            previous_feature: "CLN-8",
            change: {},
          },
          {
            repo: "org/agent",
            role: "agent",
            stages: { pr: { suite: "golden", passed: 9, failed: 1, rate: 0.9, state: "passed" } },
            previous: { pr: { suite: "golden", passed: 10, failed: 0, rate: 1, state: "passed" } },
            previous_feature: "CLN-9",
            change: { pr: -0.1 },
          },
        ],
      }),
    );
    expect(await screen.findByText("Packages")).toBeInTheDocument();
    expect(screen.getByText("org/a: v1.0.0-rc.1 becomes v1.0.0 (published)")).toBeInTheDocument();
    expect(screen.getByText("org/b: v2.0.0-rc.1 becomes v2.0.0 (not checked)")).toBeInTheDocument();
    expect(screen.getByText("The agents and knowledge this change ships")).toBeInTheDocument();
    expect(screen.getByText("Golden set (PR)")).toBeInTheDocument();
    expect(screen.getByText("staging")).toBeInTheDocument();
    expect(screen.getByText("5 of 5 (–)")).toBeInTheDocument();
    expect(screen.getByText("first release")).toBeInTheDocument();
    expect(screen.getByText("9 of 10 (90%)")).toBeInTheDocument();
    expect(screen.getByText("100% (CLN-9)")).toBeInTheDocument();
    expect(screen.getByText("-10 points")).toBeInTheDocument();
  });

  it("counts a missing firing total and no actions, and re-picks an unchecked action", async () => {
    const user = userEvent.setup();
    const { rerender } = renderItem(
      incident({ interrupt_id: "int-a", firing: undefined, actions: undefined }),
    );
    expect(await screen.findByText("Proposed actions")).toBeInTheDocument();
    expect(screen.getByText("0 alerts or issues")).toBeInTheDocument();
    expect(screen.getByText("None proposed.")).toBeInTheDocument();
    rerender(
      <MemoryRouter>
        <DecisionPanel
          item={incident({ interrupt_id: "int-b", firing: 1 })}
          featureId="CLN-1"
          onDone={() => {}}
        />
      </MemoryRouter>,
    );
    expect(await screen.findByText("1 alert or issue")).toBeInTheDocument();
    await user.click(screen.getByLabelText(/Roll back to the previous release/));
    expect(screen.getByRole("button", { name: "Approve 1 action" })).toBeInTheDocument();
    await user.click(screen.getByLabelText(/Roll back to the previous release/));
    expect(screen.getByRole("button", { name: "Approve 2 actions" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Approve 2 actions" }));
    expect(apiMock.decide).toHaveBeenCalledWith(
      "CLN-1",
      "int-b",
      expect.objectContaining({ decision: "approve", action_ids: ["a1", "a2"] }),
    );
    rerender(
      <MemoryRouter>
        <DecisionPanel
          item={incident({
            interrupt_id: "int-c",
            actions: [{ id: "a3", type: "custom_action", text: "" } as unknown as IncidentAction],
            suspects: [
              {
                feature_id: "CLN-3",
                title: "Export v2",
                released_at: "2026-11-01",
                flag: "export-v2",
                flag_on: true,
              },
              {
                feature_id: "CLN-4",
                title: "Export v3",
                released_at: "2026-11-02",
                flag: "export-v3",
                flag_on: true,
              },
            ],
          })}
          featureId="CLN-1"
          onDone={() => {}}
        />
      </MemoryRouter>,
    );
    expect(await screen.findByText("custom_action")).toBeInTheDocument();
    expect(screen.getByText("CLN-3")).toBeInTheDocument();
    expect(screen.getByText("CLN-4")).toBeInTheDocument();
  });

  it("shows a resolved signal as a plain link and a firing one as text", async () => {
    renderPanel(
      incident({
        signals: [
          {
            source: "sentry",
            status: "firing",
            severity: "high",
            title: "Export errors",
            detail: "",
            fingerprint: "fp1",
            url: "",
            at: "2026-11-01T00:00:00Z",
            component: "export",
          },
          {
            source: "pagerduty",
            status: "resolved",
            severity: "low",
            title: "Latency recovered",
            detail: "",
            fingerprint: "fp2",
            url: "https://example.invalid/latency",
            at: "2026-11-01T00:00:00Z",
            component: "export",
          },
        ],
      }),
    );
    expect(await screen.findByText("Export errors")).toBeInTheDocument();
    expect(screen.getByText("resolved")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Latency recovered" })).toHaveAttribute(
      "href",
      "https://example.invalid/latency",
    );
    expect(screen.queryByRole("link", { name: "Export errors" })).not.toBeInTheDocument();
  });

  it("renders a release gate with no model and no features", async () => {
    renderPanel(gateItem({ gate: "release", model: undefined, features: undefined }));
    expect(await screen.findByRole("heading", { name: "Ship this release train?" })).toBeInTheDocument();
    expect(screen.getByText("Kind")).toBeInTheDocument();
    expect(screen.getByText("Features")).toBeInTheDocument();
  });

  it("prices missing capacity stages at zero", async () => {
    const { rerender } = render(
      <MemoryRouter>
        <DecisionPanel
          item={gateItem({
            gate: "product",
            preview: "## Architecture\n\nTwo services.",
            capacity: {
              tier: 2,
              stages: { growth: { monthly_usd: 90, monthly_usd_max: 90 } },
              fixed: { monthly_usd: 15, unpriced: [] },
              findings: [],
            },
          })}
          featureId="CLN-1"
          onDone={() => {}}
        />
      </MemoryRouter>,
    );
    expect(await screen.findByText("Product architecture and roadmap")).toBeInTheDocument();
    expect(screen.getByText(/Production at target tier 2/)).toHaveTextContent(
      "pilot $0.000, growth $90.00, scale up to $0.000",
    );
    rerender(
      <MemoryRouter>
        <DecisionPanel
          item={gateItem({
            gate: "product",
            interrupt_id: "int-b",
            preview: "## Architecture\n\nTwo services.",
            capacity: {
              tier: 2,
              stages: {},
              fixed: { monthly_usd: 15, unpriced: [] },
              findings: [],
            },
          })}
          featureId="CLN-1"
          onDone={() => {}}
        />
      </MemoryRouter>,
    );
    expect(await screen.findByText("Product architecture and roadmap")).toBeInTheDocument();
    expect(screen.getByText(/Production at target tier 2/)).toHaveTextContent(
      "pilot $0.000, growth $0.000, scale up to $0.000",
    );
  });

  it("uses the right preview heading for each roadmap gate", async () => {
    const { rerender } = renderItem(gateItem({ gate: "g0", preview: "## Draft\n\nAn initiative." }));
    expect(await screen.findByRole("heading", { name: "Initiative draft" })).toBeInTheDocument();
    rerender(
      <MemoryRouter>
        <DecisionPanel
          item={incident({ interrupt_id: "int-b", preview: "## What happened\n\nThe export failed." })}
          featureId="CLN-1"
          onDone={() => {}}
        />
      </MemoryRouter>,
    );
    expect(await screen.findByRole("heading", { name: "Triage" })).toBeInTheDocument();
    rerender(
      <MemoryRouter>
        <DecisionPanel
          item={gateItem({
            gate: "postmortem",
            interrupt_id: "int-c",
            preview: "## What happened\n\nThe export failed.",
          })}
          featureId="CLN-1"
          onDone={() => {}}
        />
      </MemoryRouter>,
    );
    expect(await screen.findByRole("heading", { name: "Postmortem" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Rewrite" })).toBeInTheDocument();
    rerender(
      <MemoryRouter>
        <DecisionPanel
          item={gateItem({
            gate: "release",
            model: "train",
            interrupt_id: "int-d",
            preview: "## Notes\n\nAll green.",
          })}
          featureId="CLN-1"
          onDone={() => {}}
        />
      </MemoryRouter>,
    );
    expect(await screen.findByRole("heading", { name: "Release notes" })).toBeInTheDocument();
  });

  it("shows a roadmap fit with an unknown horizon as-is", async () => {
    renderPanel(
      gateItem({
        roadmap_fit: {
          initiative: {
            id: "CLN-INI-1",
            title: "Cut no-shows",
            goal: "",
            horizon: "someday",
            release_model: "launch",
          },
          budget_usd: 1000,
          spent_usd: 1200,
          over: true,
        },
      }),
    );
    expect(await screen.findByText("Roadmap")).toBeInTheDocument();
    expect(screen.getByText(/\(someday\)/)).toBeInTheDocument();
  });

  it("approves AWS services and new repos outside their usual gates", async () => {
    const aws = {
      resources: [
        {
          name: "visits-db",
          type: "database",
          service: "RDS",
          stateful: true,
          parameters: {},
          access: { app: ["read", "write"] },
          sizes: { dev: "small", production: "large" },
          monthly_usd: { dev: 10, production: 100 },
          alarms: [],
        },
      ],
      monthly_usd: { dev: 10, production: 100 },
      region: "us-east-1",
      accounts: {},
      note: "Planning figures.",
    };
    const { rerender } = renderItem(gateItem({ gate: "g5", aws_resources: aws }));
    expect(await screen.findByText("Approving also approves these AWS services")).toBeInTheDocument();
    rerender(
      <MemoryRouter>
        <DecisionPanel
          item={gateItem({
            type: "escalation",
            phase: "plan",
            interrupt_id: "int-b",
            new_repos: [
              {
                repo: "org/visits-svc",
                role: "service",
                purpose: "Serves visits",
                template: "template-service",
                visibility: "private",
                ruleset: { required_approvals: 1, required_checks: ["ci"] },
                teams: { backend: "write" },
                security: { secret_scanning: true },
              },
            ],
          })}
          featureId="CLN-1"
          onDone={() => {}}
        />
      </MemoryRouter>,
    );
    expect(await screen.findByText("New repositories")).toBeInTheDocument();
    expect(screen.getByText("org/visits-svc")).toBeInTheDocument();
  });

  it("hides mockup comments when the person cannot decide", () => {
    renderPanel(
      gateItem({
        gate: "g3",
        can_decide: false,
        mockups: {
          screens: [{ id: "M1", name: "Visits list", app: "web", phone: false }],
          findings: [],
        },
      }),
    );
    expect(screen.getByText("The screens")).toBeInTheDocument();
    expect(screen.queryByLabelText("Comment on this screen")).not.toBeInTheDocument();
  });

  it("shows an unknown artifact kind as-is", async () => {
    renderPanel(gateItem({ artifact: { id: "XXX", kind: "WEIRD" } }));
    expect(await screen.findByRole("heading", { name: "WEIRD" })).toBeInTheDocument();
  });

  it("names no approvers when the list is empty", () => {
    renderPanel(gateItem({ can_decide: false, approvers: [] }));
    expect(screen.getByText(/Waiting on the listed approvers\. You can follow along/)).toBeInTheDocument();
  });

  it("asks why for aborting a g0, a brief, and a release", async () => {
    const user = userEvent.setup();
    const { rerender } = renderItem(gateItem({ gate: "g0" }));
    await user.click(await screen.findByRole("button", { name: "Drop initiative" }));
    expect(screen.getByLabelText("Why drop this initiative?")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    rerender(
      <MemoryRouter>
        <DecisionPanel
          item={gateItem({ gate: "brief", interrupt_id: "int-b" })}
          featureId="CLN-1"
          onDone={() => {}}
        />
      </MemoryRouter>,
    );
    await user.click(await screen.findByRole("button", { name: "Stop this product" }));
    expect(screen.getByLabelText("Why stop this product?")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    rerender(
      <MemoryRouter>
        <DecisionPanel
          item={gateItem({ gate: "release", model: "train", interrupt_id: "int-c" })}
          featureId="CLN-1"
          onDone={() => {}}
        />
      </MemoryRouter>,
    );
    await user.click(await screen.findByRole("button", { name: "Cancel release" }));
    expect(screen.getByLabelText("Why cancel this release?")).toBeInTheDocument();
  });

  it("retries without a budget when none is typed", async () => {
    const user = userEvent.setup();
    renderPanel(gateItem({ type: "escalation", phase: "plan", budget_usd: undefined }));
    await user.click(await screen.findByRole("button", { name: "Let agents retry" }));
    expect(screen.getByLabelText("New budget in dollars (optional)")).toHaveAttribute("placeholder", "");
    await user.click(screen.getByRole("button", { name: "Retry with agents" }));
    const body = apiMock.decide.mock.calls[0][2] as Record<string, unknown>;
    expect(body.decision).toBe("retry");
    expect(body).not.toHaveProperty("budget_usd");
  });

  it("accepts a risk with the default waiver length when none is typed", async () => {
    const user = userEvent.setup();
    renderPanel(gateItem({ type: "escalation", phase: "security", stage: "pr", blocking: [] }));
    expect(await screen.findByText(/valid for up to 90 days/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Accept the risk" }));
    expect(screen.getByLabelText("Valid for how many days")).toHaveAttribute("placeholder", "90");
    await user.type(screen.getByLabelText(/Why is this risk acceptable\?/), "Key is a test key");
    await user.click(screen.getByRole("button", { name: "Accept the risk" }));
    const body = apiMock.decide.mock.calls[0][2] as Record<string, unknown>;
    expect(body.decision).toBe("accept_risk");
    expect(body).not.toHaveProperty("expires_days");
  });

  it("falls back to the artifact kind in the resolve label", async () => {
    const user = userEvent.setup();
    renderPanel(gateItem({ type: "escalation", phase: "build", artifact: { id: "PLAN", kind: "PLAN" } }));
    await user.click(await screen.findByRole("button", { name: "Mark resolved" }));
    expect(screen.getByLabelText(/Your finished plan \(optional: leave empty/)).toBeInTheDocument();
  });

  it("labels a handback document 'Document' when the item has no artifact", () => {
    renderPanel(gateItem({ type: "handback", gate: "g1", content: "# Idea\n\nDraft." }));
    expect(screen.getByLabelText("Document")).toBeInTheDocument();
  });

  it("stops a review without retire candidates, then retires two, one, and none", async () => {
    const user = userEvent.setup();
    const { rerender } = renderItem(review({ retire_candidates: undefined }));
    await user.click(await screen.findByRole("button", { name: "Stop the initiative" }));
    expect(
      screen.queryByText("Retire these features too (each opens a retire intent)"),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop, keep its features" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    rerender(
      <MemoryRouter>
        <DecisionPanel item={review({ interrupt_id: "int-b" })} featureId="CLN-1" onDone={() => {}} />
      </MemoryRouter>,
    );
    await user.click(await screen.findByRole("button", { name: "Stop the initiative" }));
    await user.click(screen.getByLabelText(/Old export/));
    await user.click(screen.getByLabelText(/Legacy import/));
    expect(screen.getByRole("button", { name: "Stop and retire 2 features" })).toBeInTheDocument();
    await user.click(screen.getByLabelText(/Old export/));
    expect(screen.getByRole("button", { name: "Stop and retire 1 feature" })).toBeInTheDocument();
    await user.click(screen.getByLabelText(/Legacy import/));
    expect(screen.getByRole("button", { name: "Stop, keep its features" })).toBeInTheDocument();
  });

  it("continues with no review date when the date is cleared", async () => {
    const user = userEvent.setup();
    renderPanel(review());
    await user.click(await screen.findByRole("button", { name: "Continue" }));
    const date = screen.getByLabelText("Next review");
    await user.clear(date);
    await user.click(screen.getByRole("button", { name: "Continue to the next review" }));
    const body = apiMock.decide.mock.calls[0][2] as Record<string, unknown>;
    expect(body.decision).toBe("continue");
    expect(body).not.toHaveProperty("review_by");
  });

  it("names the traceability approvers when none are listed", async () => {
    renderPanel(
      gateItem({
        gate: "g6",
        traceability: {
          complete: false,
          gaps: ["Requirement R1 has no passing test"],
          deferred: [],
          drift: [],
          drift_error: "",
          drift_blocks: false,
          blocks: true,
          override_approvers: [],
        },
      }),
    );
    expect(await screen.findByLabelText(/Release anyway: why/)).toBeInTheDocument();
    expect(screen.getByText(/only the traceability approvers can/)).toBeInTheDocument();
  });

  it("remembers a correction without a lesson when none is written", async () => {
    const user = userEvent.setup();
    renderPanel(gateItem());
    await user.click(await screen.findByRole("button", { name: "Request changes" }));
    await user.click(screen.getByLabelText("Remember this for future features"));
    await user.type(screen.getByLabelText("What should the agents change?"), "Add retention");
    await user.click(screen.getByRole("button", { name: "Send back with changes" }));
    const body = apiMock.decide.mock.calls[0][2] as Record<string, unknown>;
    expect(body.remember).toBe(true);
    expect(body).not.toHaveProperty("lesson");
  });

  it("shows a security finding with an unknown severity, no link, and no rule", async () => {
    renderPanel(
      gateItem({
        type: "escalation",
        phase: "security",
        stage: "pr",
        blocking: [
          {
            suite: "secrets",
            tool: "gitleaks",
            rule: "",
            severity: "mystery",
            title: "Odd finding",
            repo: "org/repo",
            url: "",
            fingerprint: "fp-9",
          },
        ],
      }),
    );
    expect(await screen.findByText("Blocking findings")).toBeInTheDocument();
    expect(screen.getByText("mystery")).toBeInTheDocument();
    expect(screen.getByText("Odd finding")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Odd finding" })).not.toBeInTheDocument();
    expect(screen.getByText("repo, gitleaks")).toBeInTheDocument();
  });

  it("says what went wrong for a delivery escalation with findings", async () => {
    renderPanel(
      gateItem({
        type: "escalation",
        phase: "delivery",
        wait: "build",
        findings: ["The build log shows a missing secret"],
      }),
    );
    expect(await screen.findByRole("heading", { name: "The OpenChoreo build failed" })).toBeInTheDocument();
    expect(screen.getByText("What went wrong")).toBeInTheDocument();
    expect(screen.getByText("The build log shows a missing secret")).toBeInTheDocument();
  });

  it("keeps agent notes read-only when the person cannot decide", async () => {
    renderPanel(
      gateItem({
        can_decide: false,
        agent_notes: [
          { kind: "assumed", text: "Visits are exported per clinic.", task: "Write spec", repo: "org/repo" },
        ],
      }),
    );
    expect(await screen.findByText("The agents' notes")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add as comment" })).not.toBeInTheDocument();
  });

  it("names performance levels none and full, with a soak warning", async () => {
    renderPanel(
      gateItem({
        performance: [
          { repo: "org/svc", role: "service", level: "none", reasons: [], soak: false },
          { repo: "org/api", role: "service", level: "full", reasons: ["big launch"], soak: true },
        ],
      }),
    );
    expect(await screen.findByText("Performance tests")).toBeInTheDocument();
    expect(screen.getByText(/none needed/)).toBeInTheDocument();
    expect(screen.getByText(/load test in staging/)).toBeInTheDocument();
    expect(screen.getByText(/: big launch/)).toBeInTheDocument();
    expect(screen.getByText(/\(data grows: watch the nightly soak test\)/)).toBeInTheDocument();
  });

  it("reports goal events that never fired, feed no goal, or have no repo", async () => {
    renderPanel(
      gateItem({
        instrumentation: {
          checked: true,
          environment: "staging",
          fired: { signup: 1 },
          missing: [],
          events: [
            { event: "signup", goals: [], repo: "" },
            { event: "login", goals: ["G1"], repo: "org/svc" },
          ],
          why: "",
        },
      }),
    );
    expect(await screen.findByText("Goal measurement")).toBeInTheDocument();
    expect(screen.getByText("Fired 1 time")).toBeInTheDocument();
    expect(screen.getByText("Never fired")).toBeInTheDocument();
    expect(screen.getByText(/no goal/)).toBeInTheDocument();
    expect(screen.getByText(/· svc/)).toBeInTheDocument();
  });

  it("shows a capacity error and code that was not read", async () => {
    renderPanel(
      gateItem({
        capacity_change: [{ repo: "svc", error: "No quota in this account" }],
        code_read: {
          checked: true,
          repos: { "org/svc": { sha: "abc1234def" }, "org/other": { skipped: "private repo" } },
        },
      }),
    );
    expect(await screen.findByText("Capacity this design needs")).toBeInTheDocument();
    expect(screen.getByText("No quota in this account")).toBeInTheDocument();
    expect(screen.getByText("The code the architect read")).toBeInTheDocument();
    expect(screen.getByText("Not read")).toBeInTheDocument();
    expect(screen.getByText("(private repo)")).toBeInTheDocument();
    expect(screen.getByText("Read")).toBeInTheDocument();
  });

  it("truncates a long comment quote with an ellipsis", async () => {
    const user = userEvent.setup();
    renderPanel(
      gateItem({
        gate: "g3",
        mockups: {
          screens: [
            {
              id: "M1",
              name: "A very long screen name that keeps going until the quote is well past eighty characters",
              app: "web",
              phone: false,
            },
          ],
          findings: [],
        },
      }),
    );
    expect(await screen.findByText("The screens")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Comment on this screen"), "Add the filter here");
    await user.click(screen.getByRole("button", { name: "Add comment" }));
    expect(screen.getByText("Your comments (1)")).toBeInTheDocument();
    expect(document.querySelector(".comments-box")).toHaveTextContent("…");
  });

  it("sends two comments as a plural revise", async () => {
    const user = userEvent.setup();
    renderPanel(
      gateItem({
        artifact: { id: "PRD", kind: "PRD" },
        findings: ["The measure is missing", "The scope is too wide"],
      }),
    );
    const adds = await screen.findAllByRole("button", { name: "Add as comment" });
    await user.click(adds[0]);
    await user.click(adds[1]);
    expect(screen.getByText("Your comments (2)")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Request changes (2 comments)" }));
    expect(screen.getByRole("button", { name: "Revise with 2 comments" })).toBeInTheDocument();
  });

  it("resolves with the person's version only when the document changed or was written", async () => {
    const user = userEvent.setup();
    renderPanel(
      gateItem({ type: "escalation", phase: "build", content: "# Draft spec\n\nThe export lists visits." }),
    );
    await user.click(await screen.findByRole("button", { name: "Finish it myself" }));
    await user.click(screen.getByRole("button", { name: "Mark resolved" }));
    const body = apiMock.decide.mock.calls[0][2] as Record<string, unknown>;
    expect(body.decision).toBe("resolved");
    expect(body).not.toHaveProperty("artifact_content");
  });

  it("sends a typed document when the escalation had none", async () => {
    const user = userEvent.setup();
    renderPanel(gateItem({ type: "escalation", phase: "build" }));
    await user.click(await screen.findByRole("button", { name: "Mark resolved" }));
    await user.type(screen.getByLabelText(/Your finished artifact/), "# Plan\n\nMy plan.");
    await user.click(screen.getByRole("button", { name: "Mark resolved" }));
    const body = apiMock.decide.mock.calls[0][2] as Record<string, unknown>;
    expect(body.decision).toBe("resolved");
    expect(body.artifact_content).toBe("# Plan\n\nMy plan.");
  });
});

describe("reading time", () => {
  it("reports time with a gate open every 15 seconds, and once more when it closes", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const { unmount } = render(
        <MemoryRouter>
          <DecisionPanel item={gateItem()} featureId="CLN-1" onDone={vi.fn()} />
        </MemoryRouter>,
      );
      await vi.advanceTimersByTimeAsync(15000);
      expect(apiMock.reading).toHaveBeenCalledWith("CLN-1", "int-1", expect.any(Number));
      const [, , first] = apiMock.reading.mock.calls[0];
      expect(first).toBeGreaterThanOrEqual(14);
      expect(first).toBeLessThanOrEqual(60);
      await vi.advanceTimersByTimeAsync(4000); // read on, then close it
      unmount();
      expect(apiMock.reading).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("doesn't report for someone who can't decide it", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      render(
        <MemoryRouter>
          <DecisionPanel item={gateItem({ can_decide: false })} featureId="CLN-1" onDone={vi.fn()} />
        </MemoryRouter>,
      );
      await vi.advanceTimersByTimeAsync(16000);
      expect(apiMock.reading).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });
});
