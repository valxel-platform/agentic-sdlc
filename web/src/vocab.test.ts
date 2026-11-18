import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { gateLabel, waitingLabel, money, ago } from "./vocab";

// threadPath is covered by the smoke test; this file covers the other helpers.

// Guard against a loaded shared box timing a test out.
vi.setConfig({ testTimeout: 30000 });

describe("gateLabel", () => {
  it.each([
    ["g0", "Initiative approval (G0)"],
    ["g1", "Idea approval (G1)"],
    ["g2", "Scope approval (G2)"],
    ["g3", "Design approval (G3)"],
    ["g4", "Architecture approval (G4)"],
    ["g5", "Code approval (G5)"],
    ["g6", "Release approval (G6)"],
  ])("%s -> %s", (gate, label) => {
    expect(gateLabel(gate)).toBe(label);
  });

  it("has plain names for the special gates", () => {
    expect(gateLabel("release")).toBe("Release");
    expect(gateLabel("incident")).toBe("On-call decision");
    expect(gateLabel("postmortem")).toBe("Postmortem");
    expect(gateLabel("brief")).toBe("Brief");
    expect(gateLabel("product")).toBe("Product");
    expect(gateLabel("review")).toBe("Initiative review");
  });

  it("upper-cases unknown gates", () => {
    expect(gateLabel("g9")).toBe("G9");
    expect(gateLabel("custom")).toBe("CUSTOM");
  });

  it("returns an empty string without a gate", () => {
    expect(gateLabel(undefined)).toBe("");
    expect(gateLabel("")).toBe("");
  });
});

describe("waitingLabel", () => {
  it("names known external waits", () => {
    expect(waitingLabel({ type: "external", wait: "ci" })).toBe("Waiting for CI");
    expect(waitingLabel({ type: "external", wait: "build" })).toBe("Building in OpenChoreo");
    expect(waitingLabel({ type: "external", wait: "release_ready" })).toBe("Waiting for its features");
    expect(waitingLabel({ type: "external", wait: "staging" })).toBe("Deploying to staging");
  });

  it("falls back to delivery for an unknown external wait", () => {
    expect(waitingLabel({ type: "external", wait: "mystery" })).toBe("Waiting on delivery");
    expect(waitingLabel({ type: "external" })).toBe("Waiting on delivery");
  });

  it("names escalation phases", () => {
    expect(waitingLabel({ type: "escalation", phase: "security" })).toBe("Security findings: needs a person");
    expect(waitingLabel({ type: "escalation", phase: "provisioning" })).toBe(
      "Repository setup failed: needs a person",
    );
    expect(waitingLabel({ type: "escalation", phase: "delivery", wait: "build" })).toBe(
      "OpenChoreo build failed: needs a person",
    );
    expect(waitingLabel({ type: "escalation", phase: "delivery", wait: "rollout" })).toBe(
      "Store rollout health check failed: needs a person",
    );
    expect(waitingLabel({ type: "escalation", phase: "delivery", wait: "mystery" })).toBe(
      "Delivery failed: needs a person",
    );
    expect(waitingLabel({ type: "escalation", phase: "delivery" })).toBe("Delivery failed: needs a person");
    expect(waitingLabel({ type: "escalation", phase: "build" })).toBe("Build needs a person");
    expect(waitingLabel({ type: "escalation", phase: "plan" })).toBe("Plan needs a person");
    expect(waitingLabel({ type: "escalation", phase: "mystery" })).toBe("mystery needs a person");
    // No phase at all: the app falls back to the raw phase value.
    expect(waitingLabel({ type: "escalation" })).toBe("undefined needs a person");
  });

  it("names handbacks with their gate", () => {
    expect(waitingLabel({ type: "handback", gate: "g2" })).toBe(
      "Scope approval (G2): taken over, waiting for hand-back",
    );
    expect(waitingLabel({ type: "handback", gate: "brief" })).toBe(
      "Brief: taken over, waiting for hand-back",
    );
    expect(waitingLabel({ type: "handback" })).toBe(": taken over, waiting for hand-back");
  });

  it("names the brief and product gates plainly", () => {
    expect(waitingLabel({ type: "gate", gate: "brief" })).toBe("Waiting for the brief gate");
    expect(waitingLabel({ type: "gate", gate: "product" })).toBe("Waiting for the product gate");
  });

  it("names a gate wait, with the lane when it is not the reader's", () => {
    expect(waitingLabel({ type: "gate", gate: "g1" })).toBe("Waiting for Idea approval (G1)");
    expect(waitingLabel({ type: "gate", gate: "g5", lane: "OTHER" }, "CLN")).toBe(
      "Waiting for Code approval (G5) (OTHER)",
    );
    expect(waitingLabel({ type: "gate", gate: "g5", lane: "CLN" }, "CLN")).toBe(
      "Waiting for Code approval (G5)",
    );
    expect(waitingLabel({ type: "gate", gate: "g5", lane: "CLN" })).toBe("Waiting for Code approval (G5)");
  });

  it("upper-cases an unknown gate in a gate wait", () => {
    expect(waitingLabel({ type: "gate", gate: "g9" })).toBe("Waiting for G9");
  });
});

describe("money", () => {
  it("shows a dash for missing amounts", () => {
    expect(money(null)).toBe("–");
    expect(money(undefined)).toBe("–");
  });

  it("uses three decimals below a dollar", () => {
    expect(money(0)).toBe("$0.000");
    expect(money(0.5)).toBe("$0.500");
    expect(money(0.004)).toBe("$0.004");
  });

  it("uses two decimals at a dollar and above", () => {
    expect(money(1)).toBe("$1.00");
    expect(money(12.345)).toBe("$12.35");
    expect(money(1234.5)).toBe("$1234.50");
  });
});

describe("ago", () => {
  const now = new Date("2026-11-18T12:00:00Z").getTime();
  beforeEach(() => {
    vi.spyOn(Date, "now").mockReturnValue(now);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("returns an empty string without a timestamp", () => {
    expect(ago(undefined)).toBe("");
    expect(ago(null)).toBe("");
    expect(ago("")).toBe("");
  });

  it("says just now under a minute, including the future", () => {
    expect(ago("2026-11-18T12:00:00Z")).toBe("just now");
    expect(ago("2026-11-18T11:59:30Z")).toBe("just now");
    expect(ago("2026-11-18T12:05:00Z")).toBe("just now");
  });

  it("counts whole minutes under an hour", () => {
    expect(ago("2026-11-18T11:59:00Z")).toBe("1 min ago");
    expect(ago("2026-11-18T11:30:00Z")).toBe("30 min ago");
    expect(ago("2026-11-18T11:00:01Z")).toBe("59 min ago");
  });

  it("counts whole hours under a day", () => {
    expect(ago("2026-11-18T11:00:00Z")).toBe("1 h ago");
    expect(ago("2026-11-17T13:00:00Z")).toBe("23 h ago");
  });

  it("counts whole days from a day on", () => {
    expect(ago("2026-11-17T12:00:00Z")).toBe("1 d ago");
    expect(ago("2026-11-10T12:00:00Z")).toBe("8 d ago");
  });
});
