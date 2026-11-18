import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import type { AgentSession } from "../api";
import { AgentSessions } from "./AgentSessions";

function session(over: Partial<AgentSession> = {}): AgentSession {
  return {
    trace_id: "t1",
    url: "https://example.invalid/trace/t1",
    at: "2026-11-01T00:00:00Z",
    phase: "build",
    role: "coder",
    runtime: "x",
    model: "org/model-1",
    task: "Write the export",
    attempt: "1",
    calls: 4,
    duration_s: 45,
    tokens: 100,
    cost_usd: 0.5,
    ...over,
  };
}

describe("AgentSessions", () => {
  it("renders nothing for no sessions", () => {
    const { container } = render(<AgentSessions sessions={[]} caption="Sessions" />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders sessions with phase, agent, task, time and cost", () => {
    render(
      <AgentSessions
        sessions={[
          session(),
          session({
            trace_id: "t2",
            url: "",
            phase: "design",
            role: "reviewer",
            model: "org/model-2",
            task: "",
            duration_s: 150,
            cost_usd: 1.25,
            attempt: "",
          }),
        ]}
        caption="Coder and reviewer sessions"
      />,
    );
    expect(screen.getByText("Coder and reviewer sessions")).toBeInTheDocument();
    expect(screen.getByText("Build")).toBeInTheDocument();
    expect(screen.getByText("Design")).toBeInTheDocument();
    expect(screen.getByText(/coder/)).toBeInTheDocument();
    expect(screen.getByText(/model-1/)).toBeInTheDocument();
    expect(screen.getByText("Write the export")).toBeInTheDocument();
    expect(screen.getAllByText("—")).toHaveLength(2);
    expect(screen.getByText("45 s")).toBeInTheDocument();
    expect(screen.getByText("2.5 min")).toBeInTheDocument();
    expect(screen.getByText("$0.500")).toBeInTheDocument();
    expect(screen.getByText("$1.25")).toBeInTheDocument();
    expect(screen.getByText("try 1")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "Open trace" });
    expect(link).toHaveAttribute("href", "https://example.invalid/trace/t1");
  });

  it("shows the extra-skills column only when a session used extra skills", () => {
    render(
      <AgentSessions
        sessions={[session({ extra_skills: ["search", "diagrams"] }), session({ extra_skills: [] })]}
        caption="Sessions"
      />,
    );
    expect(screen.getByText("Other skills it used")).toBeInTheDocument();
    expect(screen.getByText("search, diagrams")).toBeInTheDocument();
  });

  it("notes when traces are not linked", () => {
    render(<AgentSessions sessions={[session({ url: "" })]} caption="Sessions" />);
    expect(screen.getByText(/Traces aren't linked/)).toBeInTheDocument();
  });

  it("does not note missing traces when traced is false", () => {
    render(<AgentSessions sessions={[session({ url: "" })]} caption="Sessions" traced={false} />);
    expect(screen.queryByText(/Traces aren't linked/)).not.toBeInTheDocument();
  });

  it("falls back to the raw phase name for unknown phases", () => {
    render(<AgentSessions sessions={[session({ phase: "mystery" })]} caption="Sessions" />);
    expect(screen.getByText("mystery")).toBeInTheDocument();
  });
});

it("falls back to the raw phase and hides the traces note when told not to", () => {
  render(
    <AgentSessions sessions={[session({ phase: "mystery", url: "" })]} caption="Sessions" traced={false} />,
  );
  expect(screen.getByText("mystery")).toBeInTheDocument();
  expect(screen.queryByText(/Traces aren't linked/)).not.toBeInTheDocument();
});

it("shows a dash for a session without extra skills when another used them", () => {
  render(
    <AgentSessions
      sessions={[session({ extra_skills: ["search"] }), session({ trace_id: "t2" })]}
      caption="Sessions"
    />,
  );
  expect(screen.getByText("search")).toBeInTheDocument();
  expect(screen.getByText("—")).toBeInTheDocument();
});
