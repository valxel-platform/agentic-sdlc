import { beforeEach, describe, expect, it, vi } from "vitest";
import { configure, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { FeaturePath } from "../api";
import { apiMock, resetApiMock } from "../test/apiMock";
import { NotifyPanel, PathPanel, aboutTime } from "./Path";

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

describe("aboutTime", () => {
  it.each([
    [0, "about 5 min"],
    [12, "about 10 min"],
    [85, "about 85 min"],
    [90, "about 2 h"],
    [120, "about 2 h"],
    [2000, "about 33 h"],
    [2160, "about 2 days"],
    [4320, "about 3 days"],
  ])("formats %s minutes as %s", (minutes, expected) => {
    expect(aboutTime(minutes)).toBe(expected);
  });
});

function path(over: Partial<FeaturePath> = {}): FeaturePath {
  return {
    minutes_left: null,
    expected_done: new Date(Date.now() + 3 * 86400000).toISOString(),
    stations: [
      { key: "discovery", kind: "phase", state: "done", mine: false, eta_minutes: 60, from_history: true },
      { key: "g1", kind: "gate", state: "waiting", mine: true, eta_minutes: 30, from_history: false },
      { key: "build", kind: "phase", state: "todo", mine: false, eta_minutes: 500, from_history: true },
      { key: "g2", kind: "gate", state: "skipped", mine: false, eta_minutes: 0, from_history: false },
    ],
    ...over,
  };
}

describe("PathPanel", () => {
  it("renders stations with gate names, states and estimates", () => {
    render(<PathPanel path={path()} />);
    expect(screen.getByRole("heading", { name: "The path" })).toBeInTheDocument();
    expect(screen.getByText(/Expected to reach everyone/)).toBeInTheDocument();
    expect(screen.getByText(/if you decide your step on time/)).toBeInTheDocument();
    expect(screen.getByText("Discovery")).toBeInTheDocument();
    expect(screen.getByText("Idea approval")).toBeInTheDocument();
    expect(screen.getByText("Needs you now")).toBeInTheDocument();
    expect(screen.getByText("Waiting")).toBeInTheDocument();
    expect(screen.getByText("Build")).toBeInTheDocument();
    expect(screen.getByText(/people usually take about 30 min/)).toBeInTheDocument();
    expect(screen.getByText(/usually about 8 h/)).toBeInTheDocument();
    // Skipped stations are filtered out.
    expect(screen.queryByText("Scope approval")).not.toBeInTheDocument();
    expect(screen.getByText(/Estimates come from this product's past features/)).toBeInTheDocument();
  });

  it("says Stopped when a station stopped and Done when finished", () => {
    render(
      <PathPanel
        path={path({
          expected_done: null,
          stations: [
            {
              key: "build",
              kind: "phase",
              state: "stopped",
              mine: false,
              eta_minutes: 0,
              from_history: false,
            },
          ],
        })}
      />,
    );
    expect(screen.getByText("Stopped.")).toBeInTheDocument();
    expect(screen.getByText("Stopped")).toBeInTheDocument();

    render(
      <PathPanel
        path={path({
          expected_done: null,
          stations: [
            {
              key: "discovery",
              kind: "phase",
              state: "done",
              mine: false,
              eta_minutes: 0,
              from_history: false,
            },
          ],
        })}
      />,
    );
    expect(screen.getByText("Done.")).toBeInTheDocument();
  });

  it("uses the plural form for several of my steps", () => {
    render(
      <PathPanel
        path={path({
          stations: [
            { key: "g1", kind: "gate", state: "waiting", mine: true, eta_minutes: 30, from_history: false },
            { key: "g2", kind: "gate", state: "todo", mine: true, eta_minutes: 30, from_history: false },
          ],
        })}
      />,
    );
    expect(screen.getByText(/if you decide your 2 steps on time/)).toBeInTheDocument();
  });
});

describe("NotifyPanel", () => {
  const prefs = {
    needed: true,
    staging: false,
    email: "ada@example.com",
    channels: { email: true, chat: false },
  };

  function renderPanel(watching = false) {
    apiMock.notifications.mockResolvedValue(prefs);
    apiMock.setNotifications.mockImplementation(async (patch) => ({ ...prefs, ...patch }));
    apiMock.watch.mockImplementation(async (_id, w) => ({ watching: w }));
    render(<NotifyPanel threadId="CLN-1" watching={watching} />);
  }

  it("renders nothing while the preferences load", () => {
    apiMock.notifications.mockReturnValue(new Promise(() => {}));
    const { container } = render(<NotifyPanel threadId="CLN-1" watching={false} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("saves checkbox changes and the watch toggle", async () => {
    const user = userEvent.setup();
    renderPanel();
    const needed = await screen.findByLabelText("When a decision waits for me (any feature)");
    expect(needed).toBeChecked();
    await user.click(needed);
    expect(apiMock.setNotifications).toHaveBeenCalledWith({ needed: false });

    await user.click(screen.getByLabelText("Watch this one"));
    expect(apiMock.watch).toHaveBeenCalledWith("CLN-1", true);
    // Watching enables the staging checkbox.
    expect(await screen.findByLabelText("Also when what I watch reaches staging")).not.toBeDisabled();
    await user.click(screen.getByLabelText("Also when what I watch reaches staging"));
    expect(apiMock.setNotifications).toHaveBeenCalledWith({ staging: true });
  });

  it("saves the email on blur and notes the chat channel", async () => {
    const user = userEvent.setup();
    apiMock.notifications.mockResolvedValue({ ...prefs, channels: { email: true, chat: true } });
    apiMock.setNotifications.mockImplementation(async (patch) => ({
      ...prefs,
      channels: { email: true, chat: true },
      ...patch,
    }));
    render(<NotifyPanel threadId="CLN-1" watching={false} />);
    const email = await screen.findByLabelText("Email");
    expect(email).toHaveValue("ada@example.com");
    await user.clear(email);
    await user.type(email, "new@example.com");
    await user.tab();
    expect(apiMock.setNotifications).toHaveBeenCalledWith({ email: "new@example.com" });
    expect(screen.getByText("Your team's chat channel is told too.")).toBeInTheDocument();
  });

  it("does not save when the email is unchanged and warns when no channel exists", async () => {
    const user = userEvent.setup();
    apiMock.notifications.mockResolvedValue({ ...prefs, channels: { email: false, chat: false } });
    render(<NotifyPanel threadId="CLN-1" watching={false} />);
    const email = await screen.findByLabelText("Email");
    await user.click(email);
    await user.tab();
    expect(apiMock.setNotifications).not.toHaveBeenCalled();
    expect(screen.getByText(/No email or chat channel is set up yet/)).toBeInTheDocument();
  });

  it("shows a save error", async () => {
    const user = userEvent.setup();
    renderPanel();
    apiMock.setNotifications.mockRejectedValue(new Error("Save failed"));
    await user.click(await screen.findByLabelText("When a decision waits for me (any feature)"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Save failed");
  });
});
