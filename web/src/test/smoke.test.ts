import { describe, expect, it } from "vitest";
import type { Station } from "../api";
import { currentStage, stageStates } from "../components/Stepper";
import { threadPath } from "../vocab";

// Smoke test only: proves the harness (jsdom + TS + coverage) works.
// The full suite lives with the test-writer agents; do not grow this file.

function station(key: string, state: Station["state"], kind: Station["kind"] = "phase"): Station {
  return { key, kind, state };
}

describe("stageStates", () => {
  it("reports every stage todo for an empty rail", () => {
    expect(stageStates([])).toEqual(["todo", "todo", "todo", "todo", "todo", "todo", "todo"]);
  });

  it("derives Idea done and Plan current from the rail", () => {
    expect(stageStates([station("discovery", "done"), station("spec", "current")])).toEqual([
      "done",
      "current",
      "todo",
      "todo",
      "todo",
      "todo",
      "todo",
    ]);
  });

  it("reports a stage stopped when any of its stations stopped", () => {
    expect(stageStates([station("build", "stopped"), station("architecture", "done", "gate")])).toEqual([
      "todo",
      "todo",
      "todo",
      "stopped",
      "todo",
      "todo",
      "todo",
    ]);
  });

  it("treats done-or-skipped stations as a done stage (express profile)", () => {
    expect(stageStates([station("discovery", "skipped"), station("g1", "skipped", "gate")])).toEqual([
      "done",
      "todo",
      "todo",
      "todo",
      "todo",
      "todo",
      "todo",
    ]);
  });

  it("reports waiting when a gate is waiting", () => {
    expect(stageStates([station("g5", "waiting", "gate")])).toEqual([
      "todo",
      "todo",
      "todo",
      "todo",
      "waiting",
      "todo",
      "todo",
    ]);
  });
});

describe("currentStage", () => {
  it("is 0 when nothing has started", () => {
    expect(currentStage([])).toBe(0);
  });

  it("is the index of the current stage", () => {
    expect(currentStage([station("discovery", "done"), station("spec", "current")])).toBe(1);
  });

  it("is the index of the waiting stage", () => {
    expect(currentStage([station("g5", "waiting", "gate")])).toBe(4);
  });

  it("is Live once every stage is done", () => {
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
});

describe("threadPath", () => {
  it.each([
    ["CLN-INI-1", "/initiatives/CLN-INI-1"],
    ["CLN-REL-2", "/releases/CLN-REL-2"],
    ["CLN-PRD-3", "/products/CLN-PRD-3"],
    ["CLN-REV-4", "/reviews/CLN-REV-4"],
    ["CLN-INC-5", "/incidents/CLN-INC-5"],
    ["CLN-1", "/features/CLN-1"],
  ])("maps %s to %s", (id, path) => {
    expect(threadPath(id)).toBe(path);
  });
});
