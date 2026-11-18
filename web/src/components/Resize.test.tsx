import { beforeEach, describe, expect, it, vi } from "vitest";
import { configure, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { apiMock, resetApiMock } from "../test/apiMock";
import { Resize } from "./Resize";

// Multi-step user-event flows and async fetches exceed Vitest's 5s default
// under parallel load on the shared VM; give them room.
vi.setConfig({ testTimeout: 20000 });
configure({ asyncUtilTimeout: 5000 });

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock };
});

function renderResize(kind: "feature" | "initiative", gate: string, id = "CLN-1") {
  const pending = [
    {
      interrupt_id: "int-1",
      type: "gate" as const,
      gate,
      approvers: [],
      can_decide: true,
      targets: [],
      questions: [],
      budget: null,
      decision_brief: null,
      budget_info: null,
      ui_toggle: null,
      incident: null,
      security: null,
      trace_summary: null,
      context: [],
      thread: { id, title: "T", kind },
    },
  ];
  return render(
    <MemoryRouter initialEntries={["/start"]}>
      <Routes>
        <Route path="/features/:id" element={<div>Feature page</div>} />
        <Route path="/initiatives/:id" element={<div>Initiative page</div>} />
        <Route path="/start" element={<Resize id={id} kind={kind} pending={pending as never} />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  resetApiMock();
});

describe("Resize", () => {
  it("renders nothing without a g1 pending for a feature", () => {
    const { container } = renderResize("feature", "g3");
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing without a g0 pending for an initiative", () => {
    const { container } = renderResize("initiative", "g1", "CLN-INI-1");
    expect(container).toBeEmptyDOMElement();
  });

  it("resizes a feature into an initiative and navigates", async () => {
    const user = userEvent.setup();
    apiMock.resize.mockResolvedValue({ feature_id: "CLN-INI-1" });
    renderResize("feature", "g1");
    expect(screen.getByText("Bigger than one feature?")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Make this an initiative" }));
    expect(apiMock.resize).toHaveBeenCalledWith("CLN-1");
    expect(await screen.findByText("Initiative page")).toBeInTheDocument();
  });

  it("resizes an initiative into one feature and navigates", async () => {
    const user = userEvent.setup();
    apiMock.resize.mockResolvedValue({ feature_id: "CLN-1" });
    renderResize("initiative", "g0", "CLN-INI-1");
    expect(screen.getByText("Smaller than it looked?")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Make it one feature" }));
    expect(apiMock.resize).toHaveBeenCalledWith("CLN-INI-1");
    expect(await screen.findByText("Feature page")).toBeInTheDocument();
  });

  it("shows a resize error and stays put", async () => {
    const user = userEvent.setup();
    apiMock.resize.mockRejectedValue(new Error("Resize failed"));
    renderResize("feature", "g1");
    await user.click(screen.getByRole("button", { name: "Make this an initiative" }));
    expect(await screen.findByText("Resize failed")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Make this an initiative" })).toBeEnabled();
  });
});
