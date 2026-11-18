import { describe, expect, it, vi } from "vitest";
import { configure, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { MockupSet } from "../api";
import { Mockups } from "./Mockups";

// Multi-step user-event flows and async fetches exceed Vitest's 5s default
// under parallel load on the shared VM; give them room.
vi.setConfig({ testTimeout: 20000 });
configure({ asyncUtilTimeout: 5000 });

function set(over: Partial<MockupSet> = {}): MockupSet {
  return {
    screens: [
      { id: "M1", name: "Visits list", app: "web", phone: false },
      { id: "M2", name: "Visit detail", app: "web", phone: true },
    ],
    findings: ["The list needs a filter."],
    ...over,
  };
}

describe("Mockups", () => {
  it("renders nothing when there are no screens and no findings", () => {
    const { container } = render(<Mockups featureId="CLN-1" set={{ screens: [], findings: [] }} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders the first finding when there are no screens", () => {
    render(<Mockups featureId="CLN-1" set={{ screens: [], findings: ["No mockups were produced."] }} />);
    expect(screen.getByText("No mockups were produced.")).toBeInTheDocument();
  });

  it("renders the first screen with tabs and width toggle", async () => {
    const user = userEvent.setup();
    render(<Mockups featureId="CLN-1" set={set()} />);
    expect(screen.getByRole("heading", { name: "The screens" })).toBeInTheDocument();
    const frame = screen.getByTitle("Mockup of M1: Visits list");
    expect(frame).toHaveAttribute("src", "/api/features/CLN-1/mockups/M1");
    expect(screen.getByText(/A low-fidelity mockup from the UX spec, for web/)).toBeInTheDocument();
    expect(screen.getByText("The list needs a filter.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Phone" }));
    expect(screen.getByRole("button", { name: "Phone" })).toHaveAttribute("aria-pressed", "true");
    await user.click(screen.getByRole("tab", { name: /M2 Visit detail/ }));
    expect(screen.getByTitle("Mockup of M2: Visit detail")).toBeInTheDocument();
  });

  it("comments on the current screen", async () => {
    const user = userEvent.setup();
    const onComment = vi.fn();
    render(<Mockups featureId="CLN-1" set={set()} onComment={onComment} />);
    const box = screen.getByLabelText("Comment on this screen");
    expect(box).toHaveAttribute("placeholder", "Comment on M1: what should change on this screen?");
    await user.type(box, "The filter is missing");
    await user.click(screen.getByRole("button", { name: "Add comment" }));
    expect(onComment).toHaveBeenCalledWith("M1 Visits list", "The filter is missing");
    expect(box).toHaveValue("");
  });

  it("disables the comment button with empty text and hides the form without onComment", async () => {
    render(<Mockups featureId="CLN-1" set={set()} />);
    expect(screen.queryByLabelText("Comment on this screen")).not.toBeInTheDocument();
  });
});

it("omits the app note for a screen without an app and toggles back to desktop", async () => {
  const user = userEvent.setup();
  render(
    <Mockups
      featureId="CLN-1"
      set={set({ screens: [{ id: "M1", name: "Visits list", app: "", phone: false }] })}
    />,
  );
  expect(
    screen.getByText(
      /A low-fidelity mockup from the UX spec: switch its states at the top\. Example values only\./,
    ),
  ).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Phone" }));
  expect(screen.getByRole("button", { name: "Phone" })).toHaveAttribute("aria-pressed", "true");
  await user.click(screen.getByRole("button", { name: "Desktop" }));
  expect(screen.getByRole("button", { name: "Desktop" })).toHaveAttribute("aria-pressed", "true");
});
