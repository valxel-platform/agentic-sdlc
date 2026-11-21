import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import type { LiveEvent } from "../live";

let fire: ((e: LiveEvent) => void) | null = null;
vi.mock("../live", () => ({
  useLive: (refresh: () => void, filter: (e: LiveEvent) => boolean, opts?: { steps?: boolean }) => {
    if (opts?.steps) fire = (e) => filter(e) && refresh();
  },
  useLiveStatus: () => true,
}));
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock };
});

import { apiMock } from "../test/apiMock";
import { AgentSteps } from "../pages/Feature";

const step = (s: string, i: number) => ({
  phase: "build",
  role: "coder",
  repo: "cln-intake-api",
  step: s,
  at: `t${i}`,
});

describe("AgentSteps", () => {
  it("shows the last steps and follows new ones as they come", async () => {
    apiMock.agentSteps.mockResolvedValueOnce({ steps: [step("Reading src/queue.py", 1)] });
    render(<AgentSteps id="CLN-FEAT-3" />);
    expect(await screen.findByText("Reading src/queue.py")).toBeInTheDocument();
    expect(screen.getByText("coder in cln-intake-api:")).toBeInTheDocument();
    apiMock.agentSteps.mockResolvedValueOnce({
      steps: [step("Reading src/queue.py", 1), step("Tests: 3 failing, 40 passing", 2)],
    });
    fire!({ type: "feature.progress", feature_id: "OTHER-1", at: "" });
    fire!({ type: "feature.progress", feature_id: "CLN-FEAT-3", at: "" });
    expect(await screen.findByText("Tests: 3 failing, 40 passing")).toBeInTheDocument();
    expect(apiMock.agentSteps).toHaveBeenCalledTimes(2);
  });

  it("shows nothing before the first step", async () => {
    const { container } = render(<AgentSteps id="CLN-FEAT-4" />);
    await Promise.resolve();
    expect(container).toBeEmptyDOMElement();
  });
});
