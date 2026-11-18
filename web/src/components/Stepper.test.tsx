import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import type { Station } from "../api";
import { STAGE_LABELS, Stepper, currentStage, stageStates } from "./Stepper";

// RTL does not auto-cleanup under vitest with globals: false.
afterEach(cleanup);

// Guard against a loaded shared box timing a test out.
vi.setConfig({ testTimeout: 30000 });

// The smoke test covers stageStates/currentStage basics; this file covers
// the rendered Stepper and the helper combinations the smoke test does not.

function station(key: string, state: Station["state"], kind: Station["kind"] = "phase"): Station {
  return { key, kind, state };
}

function renderStepper(stations: Station[], mini = false) {
  return render(<Stepper stations={stations} mini={mini} />);
}

function stageItem(label: string): HTMLElement {
  const item = screen.getByText(label).closest("li");
  if (!item) throw new Error(`no list item for ${label}`);
  return item;
}

describe("Stepper rendering", () => {
  it("renders all seven stages in order", () => {
    renderStepper([]);
    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(7);
    expect(items.map((li) => li.textContent)).toEqual([...STAGE_LABELS]);
    expect(screen.getByRole("list")).toHaveAttribute("aria-label", "Where this is, from idea to live");
  });

  it("marks every stage todo for an empty rail, with no current step", () => {
    renderStepper([]);
    for (const label of STAGE_LABELS) {
      expect(stageItem(label)).toHaveClass("todo");
      expect(stageItem(label)).not.toHaveAttribute("aria-current");
    }
  });

  it("marks done and current stages and flags the current step", () => {
    renderStepper([station("discovery", "done"), station("spec", "current")]);
    expect(stageItem("Idea")).toHaveClass("done");
    expect(stageItem("Plan")).toHaveClass("current");
    expect(stageItem("Plan")).toHaveAttribute("aria-current", "step");
    expect(stageItem("Idea")).not.toHaveAttribute("aria-current");
  });

  it("marks a waiting stage and flags it as the current step", () => {
    renderStepper([station("g5", "waiting", "gate")]);
    expect(stageItem("Check")).toHaveClass("waiting");
    expect(stageItem("Check")).toHaveAttribute("aria-current", "step");
  });

  it("marks a stopped stage", () => {
    renderStepper([station("build", "stopped"), station("architecture", "done", "gate")]);
    expect(stageItem("Build")).toHaveClass("stopped");
    expect(stageItem("Build")).not.toHaveAttribute("aria-current");
  });

  it("marks every stage done when the feature is live", () => {
    renderStepper([
      station("discovery", "done"),
      station("spec", "done"),
      station("design", "done"),
      station("build", "done"),
      station("g5", "done", "gate"),
      station("release", "done"),
      station("operate", "done"),
    ]);
    for (const label of STAGE_LABELS) {
      expect(stageItem(label)).toHaveClass("done");
    }
  });

  it("treats skipped stations as done (express profile)", () => {
    renderStepper([station("discovery", "skipped"), station("g1", "skipped", "gate")]);
    expect(stageItem("Idea")).toHaveClass("done");
  });

  it("adds the mini class when asked", () => {
    renderStepper([], true);
    expect(screen.getByRole("list")).toHaveClass("stepper", "mini");
  });

  it("has no mini class by default", () => {
    renderStepper([]);
    expect(screen.getByRole("list")).toHaveClass("stepper");
    expect(screen.getByRole("list")).not.toHaveClass("mini");
  });
});

describe("stageStates combinations", () => {
  it("stopped beats waiting and current in the same stage", () => {
    expect(
      stageStates([
        station("g5", "waiting", "gate"),
        station("g5", "current", "gate"),
        station("security", "stopped"),
      ])[4],
    ).toBe("stopped");
  });

  it("waiting beats current in the same stage", () => {
    expect(stageStates([station("g5", "waiting", "gate"), station("security", "current")])[4]).toBe(
      "waiting",
    );
  });

  it("a stage with some stations done and one todo is todo, not done", () => {
    const states = stageStates([station("architecture", "done", "gate"), station("build", "todo")]);
    expect(states[3]).toBe("todo");
  });

  it("ignores stations with keys that belong to no stage", () => {
    expect(stageStates([station("bogus", "current")])).toEqual([
      "todo",
      "todo",
      "todo",
      "todo",
      "todo",
      "todo",
      "todo",
    ]);
  });

  it("folds the security station into Check and delivery into Live", () => {
    const states = stageStates([station("security", "waiting"), station("delivery", "current")]);
    expect(states[4]).toBe("waiting");
    expect(states[6]).toBe("current");
  });

  it("folds architecture and G4 into Build", () => {
    const states = stageStates([station("architecture", "done", "gate"), station("g4", "current", "gate")]);
    expect(states[3]).toBe("current");
  });
});

describe("currentStage combinations", () => {
  it("is the waiting stage when nothing is current", () => {
    expect(currentStage([station("release", "waiting")])).toBe(5);
  });

  it("is the stage after the last done stage", () => {
    expect(
      currentStage([station("discovery", "done"), station("spec", "done"), station("design", "done")]),
    ).toBe(3);
  });

  it("never goes past Live", () => {
    expect(
      currentStage([
        station("discovery", "done"),
        station("spec", "done"),
        station("design", "done"),
        station("build", "done"),
        station("g5", "done", "gate"),
        station("release", "done"),
        station("operate", "done"),
      ]),
    ).toBe(6);
  });

  it("is 0 when the rail has only unknown stations", () => {
    expect(currentStage([station("bogus", "current")])).toBe(0);
  });
});
