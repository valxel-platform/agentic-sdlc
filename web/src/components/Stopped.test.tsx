import { beforeEach, describe, expect, it, vi } from "vitest";
import { configure, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { apiMock, resetApiMock } from "../test/apiMock";
import { Stopped } from "./Stopped";

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

describe("Stopped", () => {
  it("renders the error and resumes on click", async () => {
    const user = userEvent.setup();
    const onResumed = vi.fn();
    render(<Stopped id="CLN-1" error="Build timed out" onResumed={onResumed} />);
    expect(screen.getByRole("alert")).toHaveTextContent(
      "The workflow stopped with an error: Build timed out",
    );
    expect(
      screen.getByText(/Fix the cause, then resume\. It carries on from its last completed step\./),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Resume" }));
    expect(apiMock.resume).toHaveBeenCalledWith("CLN-1");
    expect(onResumed).toHaveBeenCalled();
  });

  it("names the stopped thing and applies the style", () => {
    const { container } = render(
      <Stopped id="CLN-1" error="boom" what="release" style={{ marginTop: 4 }} onResumed={() => {}} />,
    );
    expect(screen.getByText(/The release stopped with an error: boom/)).toBeInTheDocument();
    expect(container.firstElementChild).toHaveStyle("margin-top: 4px");
  });

  it("shows a resume error", async () => {
    const user = userEvent.setup();
    apiMock.resume.mockRejectedValue(new Error("Still failing"));
    render(<Stopped id="CLN-1" error="Build timed out" onResumed={() => {}} />);
    await user.click(screen.getByRole("button", { name: "Resume" }));
    expect(await screen.findByText("Couldn't resume: Still failing")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Resume" })).toBeEnabled();
  });
});
