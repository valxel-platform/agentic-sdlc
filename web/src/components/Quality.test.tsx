import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import type { QualitySuite, Waiver } from "../api";
import { QualityTable } from "./Quality";

function suite(over: Partial<QualitySuite> = {}): QualitySuite {
  return {
    stage: "pr",
    suite: "unit",
    kind: "test",
    repo: "org/svc",
    state: "passed",
    passed: 10,
    failed: 0,
    coverage: 92,
    ...over,
  };
}

function waiver(over: Partial<Waiver> = {}): Waiver {
  return {
    fingerprint: "fp-1",
    suite: "secrets",
    severity: "high",
    title: "Hardcoded secret",
    repo: "org/visits-svc",
    reason: "False positive",
    approver: "ada",
    expires: "2026-12-31T00:00:00Z",
    ...over,
  };
}

describe("QualityTable", () => {
  it("says when nothing has reported yet", () => {
    render(<QualityTable suites={[]} />);
    expect(screen.getByText("No tests or scans have reported yet.")).toBeInTheDocument();
  });

  it("renders a passing test suite with coverage", () => {
    render(<QualityTable suites={[suite()]} />);
    expect(screen.getByText("Pull requests")).toBeInTheDocument();
    expect(screen.getByText("Unit tests")).toBeInTheDocument();
    expect(screen.getByText("Passed")).toBeInTheDocument();
    expect(screen.getByText("10 passed, 0 failed, coverage 92%")).toBeInTheDocument();
  });

  it("folds two repos of the same suite together, worst state wins", () => {
    render(
      <QualityTable
        suites={[
          suite({ state: "passed", passed: 10, failed: 0, coverage: 95 }),
          suite({ state: "failed", passed: 8, failed: 2, coverage: 80 }),
        ]}
      />,
    );
    expect(screen.getAllByText("Unit tests")).toHaveLength(1);
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(screen.getByText("18 passed, 2 failed, coverage 80% across 2 repos")).toBeInTheDocument();
  });

  it("renders scan suites with findings and waived counts", () => {
    render(
      <QualityTable
        suites={[
          suite({
            stage: "build",
            suite: "sast",
            kind: "security",
            state: "pending",
            findings: [
              {
                suite: "sast",
                tool: "semgrep",
                rule: "r1",
                severity: "high",
                title: "Finding A",
                repo: "org/svc",
                url: "",
                fingerprint: "a",
              },
              {
                suite: "sast",
                tool: "semgrep",
                rule: "r2",
                severity: "low",
                title: "Finding B",
                repo: "org/svc",
                url: "",
                fingerprint: "b",
              },
            ],
            waived: ["c"],
            passed: undefined,
            failed: undefined,
          }),
        ]}
      />,
    );
    expect(screen.getByText("Code scanning")).toBeInTheDocument();
    expect(screen.getByText("Running")).toBeInTheDocument();
    expect(screen.getByText("2 findings, 1 accepted")).toBeInTheDocument();
  });

  it("sorts by stage then kind then suite, and marks optional suites", () => {
    render(
      <QualityTable
        suites={[
          suite({ stage: "staging", suite: "smoke", state: "missing" }),
          suite({
            stage: "pr",
            suite: "e2e",
            state: "passed",
            required: false,
            passed: 1,
            failed: 0,
            coverage: undefined,
          }),
          suite({ stage: "pr", suite: "sast", kind: "security", state: "passed", findings: [] }),
        ]}
      />,
    );
    const rows = screen.getAllByRole("row");
    // Header + 3 data rows; PR rows first, test kind before scan.
    expect(rows[1]).toHaveTextContent("End-to-end tests");
    expect(rows[1]).toHaveTextContent("optional");
    expect(rows[2]).toHaveTextContent("Code scanning");
    expect(rows[3]).toHaveTextContent("Smoke tests");
    expect(screen.getByText("No result")).toBeInTheDocument();
  });

  it("falls back to the raw state for an unknown state", () => {
    render(<QualityTable suites={[suite({ state: "mystery" as never })]} />);
    expect(screen.getByText("mystery")).toBeInTheDocument();
  });

  it("lists accepted risks with approver and expiry", () => {
    render(<QualityTable suites={[suite()]} waivers={[waiver()]} />);
    expect(screen.getByRole("heading", { name: "Accepted risks" })).toBeInTheDocument();
    expect(screen.getByText("high")).toBeInTheDocument();
    expect(
      screen.getByText(
        /Hardcoded secret \(visits-svc\): False positive\. Accepted by ada until 2026-12-31\./,
      ),
    ).toBeInTheDocument();
  });

  it("hides the waivers section when there are none", () => {
    render(<QualityTable suites={[suite()]} waivers={[]} />);
    expect(screen.queryByText("Accepted risks")).not.toBeInTheDocument();
  });
});

it("covers the fold, sort and detail branches", () => {
  render(
    <QualityTable
      suites={[
        suite({
          stage: "pr",
          suite: "unit",
          kind: "test",
          state: "passed",
          passed: undefined,
          failed: undefined,
          coverage: undefined,
        }),
        suite({
          stage: "pr",
          suite: "e2e",
          kind: "test",
          state: "passed",
          passed: 3,
          failed: 0,
          coverage: 80,
        }),
        suite({
          stage: "pr",
          suite: "unit",
          kind: "test",
          state: "passed",
          passed: 5,
          failed: 1,
          coverage: 70,
        }),
        suite({
          stage: "dev",
          suite: "smoke",
          kind: "test",
          state: "passed",
          passed: null,
          failed: null,
          coverage: null,
        }),
        suite({
          stage: "staging",
          suite: "custom",
          kind: "security",
          state: "passed",
          findings: [
            {
              suite: "sast",
              tool: "semgrep",
              rule: "r1",
              severity: "high",
              title: "Finding A",
              repo: "org/svc",
              url: "",
              fingerprint: "a",
            },
          ],
          waived: [],
        }),
      ]}
    />,
  );
  const rows = screen.getAllByRole("row");
  // Same stage, same kind: sorted by suite name (End-to-end before Unit).
  expect(rows[1]).toHaveTextContent("End-to-end tests");
  expect(rows[2]).toHaveTextContent("Unit tests");
  expect(rows[2]).toHaveTextContent("5 passed, 1 failed, coverage 70% across 2 repos");
  expect(screen.getByText("1 finding")).toBeInTheDocument();
  expect(screen.getByText("custom")).toBeInTheDocument();
  expect(screen.getByText("Smoke tests")).toBeInTheDocument();
});

it("falls back to raw stage and suite names", () => {
  render(
    <QualityTable
      suites={[suite({ stage: "mystery", suite: "custom", kind: "security", state: "passed", findings: [] })]}
    />,
  );
  expect(screen.getByText("mystery")).toBeInTheDocument();
  expect(screen.getByText("custom")).toBeInTheDocument();
});

it("merges numeric findings, null coverage and missing stages", () => {
  render(
    <QualityTable
      suites={[
        {
          stage: "pr",
          suite: "unit",
          kind: "test",
          repo: "org/svc",
          state: "passed",
          passed: 3,
          failed: 0,
          coverage: 80,
        },
        {
          stage: "pr",
          suite: "unit",
          kind: "test",
          repo: "org/svc",
          state: "passed",
          passed: undefined,
          failed: undefined,
          coverage: undefined,
        },
        {
          stage: undefined as never,
          suite: "scan",
          kind: "security",
          repo: "org/svc",
          state: "failed",
          findings: 3,
          waived: 2,
          coverage: null,
        },
        {
          stage: "dev",
          suite: "smoke",
          kind: "test",
          repo: "org/svc",
          state: "passed",
          passed: 5,
          failed: undefined,
          coverage: null,
        },
      ]}
    />,
  );
  expect(screen.getByText("Unit tests")).toBeInTheDocument();
  expect(screen.getByText("Smoke tests")).toBeInTheDocument();
  // The merged unit row totals the defined suite (3 passed, 0 failed).
  expect(screen.getByText(/3 passed, 0 failed/)).toBeInTheDocument();
  expect(screen.getByText(/coverage 80%/)).toBeInTheDocument();
  // Numeric findings render as a count with accepted waivers beside them.
  expect(screen.getByText(/3 findings/)).toBeInTheDocument();
  expect(screen.getByText(/2 accepted/)).toBeInTheDocument();
  // The dev smoke suite totals its defined passed count.
  expect(screen.getByText(/5 passed, 0 failed/)).toBeInTheDocument();
  // The stage-less suite falls back to its raw stage text.
  expect(screen.getByText("scan")).toBeInTheDocument();
});

it("sorts a security suite before a test suite at the same stage", () => {
  render(
    <QualityTable
      suites={[
        {
          stage: "pr",
          suite: "scan",
          kind: "security",
          repo: "org/svc",
          state: "passed",
          findings: [],
          waived: [],
        },
        { stage: "pr", suite: "unit", kind: "test", repo: "org/svc", state: "passed", passed: 1, failed: 0 },
      ]}
    />,
  );
  // The security suite sorts after the test suite at the same stage even
  // when it comes first in the input.
  const rows = screen.getAllByRole("row");
  expect(rows[1]).toHaveTextContent("Unit tests");
  expect(rows[2]).toHaveTextContent("scan");
});
