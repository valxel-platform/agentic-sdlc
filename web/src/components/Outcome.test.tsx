import { beforeEach, describe, expect, it, vi } from "vitest";
import { configure, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import type { GoalOutcome } from "../api";
import { apiMock, resetApiMock } from "../test/apiMock";
import { GoalOutcomeLine, ReadoutTable, StatusChip, WantedForm, num } from "./Outcome";

// Multi-step user-event flows and async fetches exceed Vitest's 5s default
// under parallel load on the shared VM; give them room.
vi.setConfig({ testTimeout: 20000 });
configure({ asyncUtilTimeout: 5000 });

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock };
});

beforeEach(() => {
  resetApiMock();
});

describe("num", () => {
  it.each([
    [null, "", "no data"],
    [undefined, "", "no data"],
    [5, "", "5"],
    [5.678, "", "5.68"],
    [5, "%", "5%"],
    [5, "visits", "5 visits"],
    [0.5, "%", "0.5%"],
  ])("formats %s with unit %s as %s", (v, unit, expected) => {
    expect(num(v as number | null | undefined, unit)).toBe(expected);
  });
});

describe("StatusChip", () => {
  it.each([
    ["on_track", "On track"],
    ["flat", "Flat"],
    ["off_track", "Off track"],
    ["no_data", "No data"],
  ])("renders %s as %s", (status, label) => {
    render(<StatusChip status={status as never} />);
    expect(screen.getByText(label)).toBeInTheDocument();
  });

  it("falls back to No data for an unknown status", () => {
    render(<StatusChip status={"mystery" as never} why="unknown" />);
    const chip = screen.getByText("No data");
    expect(chip).toHaveAttribute("title", "unknown");
  });
});

function outcome(over: Partial<GoalOutcome> = {}): GoalOutcome {
  return {
    metric: {
      goal_id: "G1",
      source: "sql",
      unit: "%",
      direction: "up",
      baseline: 10,
      target: 5,
      by: "2026-12-01",
    },
    latest: 7,
    status: "on_track",
    trend: [
      { end: "2026-11-01", value: 9, status: "ok", note: "" },
      { end: "2026-11-08", value: 7, status: "ok", note: "" },
    ],
    ...over,
  };
}

describe("GoalOutcomeLine", () => {
  it("renders the latest reading, baseline, target and trend", () => {
    render(
      <MemoryRouter>
        <GoalOutcomeLine o={outcome()} />
      </MemoryRouter>,
    );
    expect(screen.getByText("On track")).toBeInTheDocument();
    expect(screen.getByText("7%")).toBeInTheDocument();
    expect(screen.getByText(/from 10%/)).toBeInTheDocument();
    expect(screen.getByText(/, target 5% by 2026-12-01/)).toBeInTheDocument();
    expect(screen.getByText(/read from the product's database/)).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /Trend: 9, 7; target 5/ })).toBeInTheDocument();
  });

  it("says when there is no reading yet", () => {
    render(
      <MemoryRouter>
        <GoalOutcomeLine o={outcome({ latest: null, status: "no_data", why: "No events yet", trend: [] })} />
      </MemoryRouter>,
    );
    expect(screen.getByText("No reading yet")).toBeInTheDocument();
    expect(screen.getByText("No data")).toBeInTheDocument();
    expect(screen.getByText(/No events yet/)).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("names an unknown source as-is", () => {
    render(
      <MemoryRouter>
        <GoalOutcomeLine
          o={outcome({
            metric: {
              goal_id: "G1",
              source: "manual",
              unit: "",
              direction: "up",
              baseline: null,
              target: null,
              by: "",
            },
          })}
        />
      </MemoryRouter>,
    );
    expect(screen.getByText(/entered by hand/)).toBeInTheDocument();
  });
});

describe("WantedForm", () => {
  const wanted = {
    goal_id: "G1",
    feature_id: "CLN-1",
    window_days: 7,
    start: "2026-11-01T00:00:00Z",
    end: "2026-11-08T00:00:00Z",
    unit: "%",
  };

  function renderForm(onDone = vi.fn()) {
    render(
      <MemoryRouter>
        <ul>
          <WantedForm productId="CLN" w={wanted} onDone={onDone} />
        </ul>
      </MemoryRouter>,
    );
    return { onDone };
  }

  it("renders the window and feature link", () => {
    renderForm();
    expect(screen.getByText("G1")).toBeInTheDocument();
    expect(screen.getByText(/2026-11-01 to 2026-11-08, 7 days after/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "CLN-1" })).toHaveAttribute("href", "/features/CLN-1");
    expect(screen.getByLabelText(/Value \(%\)/)).toBeInTheDocument();
  });

  it("disables save until a value is entered", async () => {
    const user = userEvent.setup();
    renderForm();
    expect(screen.getByRole("button", { name: "Save number" })).toBeDisabled();
    await user.type(screen.getByLabelText(/Value \(%\)/), "6");
    expect(screen.getByRole("button", { name: "Save number" })).toBeEnabled();
  });

  it("submits the reading with optional n and k", async () => {
    const user = userEvent.setup();
    const { onDone } = renderForm();
    apiMock.recordReading.mockResolvedValue({ reading: {}, readouts: [] });
    await user.type(screen.getByLabelText(/Value \(%\)/), "6");
    await user.type(screen.getByLabelText("Counted (n)"), "40");
    await user.type(screen.getByLabelText("Of those, on top (k)"), "30");
    await user.click(screen.getByRole("button", { name: "Save number" }));
    expect(apiMock.recordReading).toHaveBeenCalledWith("CLN", "G1", {
      value: 6,
      start: "2026-11-01T00:00:00Z",
      end: "2026-11-08T00:00:00Z",
      n: 40,
      k: 30,
    });
    expect(onDone).toHaveBeenCalled();
  });

  it("omits n and k when left blank and shows an error", async () => {
    const user = userEvent.setup();
    renderForm();
    apiMock.recordReading.mockRejectedValue(new Error("Reading rejected"));
    await user.type(screen.getByLabelText(/Value \(%\)/), "6");
    await user.click(screen.getByRole("button", { name: "Save number" }));
    expect(apiMock.recordReading).toHaveBeenCalledWith("CLN", "G1", expect.objectContaining({ value: 6 }));
    expect(apiMock.recordReading.mock.calls[0][2]).not.toHaveProperty("n");
    expect(await screen.findByRole("alert")).toHaveTextContent("Reading rejected");
  });
});

describe("ReadoutTable", () => {
  it("renders nothing for no readouts", () => {
    const { container } = render(<ReadoutTable readouts={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders before/after/change with the right wording", () => {
    render(
      <ReadoutTable
        metrics={{
          G1: { goal_id: "G1", source: "sql", unit: "%", direction: "up", baseline: 10, target: 5, by: "" },
        }}
        readouts={[
          {
            feature_id: "CLN-1",
            goal_id: "G1",
            window_days: 7,
            released_at: "2026-11-01",
            source: "sql",
            unit: "%",
            direction: "up",
            before: { start: "", end: "", status: "ok", value: 10, n: null, note: "" },
            after: { start: "", end: "", status: "ok", value: 7, n: null, note: "" },
            change: -3,
            toward: true,
            caveat: "",
          },
          {
            feature_id: "CLN-1",
            goal_id: "G2",
            window_days: 30,
            released_at: "2026-11-01",
            source: "manual",
            unit: "",
            direction: "up",
            before: { start: "", end: "", status: "suppressed", value: null, n: null, note: "" },
            after: { start: "", end: "", status: "missing", value: null, n: null, note: "" },
            change: null,
            toward: null,
            caveat: "",
          },
          {
            feature_id: "CLN-1",
            goal_id: "G3",
            window_days: 7,
            released_at: "2026-11-01",
            source: "sql",
            unit: "",
            direction: "up",
            before: { start: "", end: "", status: "missing", value: null, n: null, note: "" },
            after: { start: "", end: "", status: "ok", value: 4, n: null, note: "" },
            change: 1,
            toward: false,
            caveat: "",
          },
          {
            feature_id: "CLN-1",
            goal_id: "G4",
            window_days: 7,
            released_at: "2026-11-01",
            source: "sql",
            unit: "",
            direction: "up",
            before: { start: "", end: "", status: "ok", value: 10, n: null, note: "" },
            after: { start: "", end: "", status: "suppressed", value: null, n: null, note: "" },
            change: 2,
            toward: null,
            caveat: "",
          },
        ]}
      />,
    );
    expect(screen.getByText("10%")).toBeInTheDocument();
    expect(screen.getByText("7%")).toBeInTheDocument();
    expect(screen.getByText("-3%, toward target")).toBeInTheDocument();
    expect(screen.getAllByText("under the minimum")).toHaveLength(2);
    expect(screen.getByText("waiting for the number")).toBeInTheDocument();
    expect(screen.getByText("none yet")).toBeInTheDocument();
    expect(screen.getByText("no data")).toBeInTheDocument();
    expect(screen.getByText("+1, away")).toBeInTheDocument();
    expect(screen.getByText("+2")).toBeInTheDocument();
    expect(screen.getByText("+2").closest("span")).toHaveClass("chip", "plain");
  });
});
