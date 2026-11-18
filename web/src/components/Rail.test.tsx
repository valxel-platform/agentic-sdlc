import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import type { PathStation } from "../api";
import { Rail } from "./Rail";

function station(over: Partial<PathStation> = {}): PathStation {
  return { key: "build", kind: "phase", state: "todo", eta_minutes: 60, from_history: false, ...over };
}

describe("Rail", () => {
  it("says Starting when there are no stations", () => {
    render(<Rail stations={[]} />);
    expect(screen.getByText("Starting…")).toBeInTheDocument();
  });

  it("renders phase and gate stations with labels and state", () => {
    render(
      <Rail
        stations={[
          station({ key: "discovery", state: "done" }),
          station({ key: "g1", kind: "gate", state: "waiting" }),
          station({ key: "build", state: "current" }),
          station({ key: "g6", kind: "gate", state: "skipped" }),
        ]}
      />,
    );
    const rail = screen.getByRole("img");
    expect(rail).toHaveClass("full");
    expect(rail).toHaveAttribute(
      "aria-label",
      "Discovery: done; G1 Idea approval: waiting on a person; Build: agents working; G6 Release approval: skipped (low risk)",
    );
    expect(screen.getByText("Discovery")).toBeInTheDocument();
    expect(screen.getByText("G1")).toBeInTheDocument();
    expect(screen.getByText("Build")).toBeInTheDocument();
    expect(screen.getByText("G6")).toBeInTheDocument();
    expect(screen.getByTitle("G1 Idea approval: waiting on a person")).toBeInTheDocument();
  });

  it("uses the compact class when asked", () => {
    render(<Rail stations={[station()]} compact />);
    expect(screen.getByRole("img")).toHaveClass("compact");
  });

  it("falls back to the raw key for an unknown phase", () => {
    render(<Rail stations={[station({ key: "mystery", state: "stopped" })]} />);
    // The visible label is empty for an unknown phase; the summary still names it.
    expect(screen.getByRole("img")).toHaveAttribute("aria-label", "mystery: stopped");
  });
});
