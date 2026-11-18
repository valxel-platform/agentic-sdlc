import { beforeEach, describe, expect, it, vi } from "vitest";
import { configure, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { apiMock, resetApiMock } from "../test/apiMock";
import { ChangeRequest } from "./ChangeRequest";

// Multi-step user-event flows and async fetches exceed Vitest's 5s default
// under parallel load on the shared VM; give them room.
vi.setConfig({ testTimeout: 20000 });
configure({ asyncUtilTimeout: 5000 });

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock };
});

const DESC = "Add a CSV export to the visits list";

beforeEach(() => {
  resetApiMock();
});

describe("ChangeRequest", () => {
  it("says a change is a new request once the build has started", () => {
    render(<ChangeRequest id="CLN-1" description={DESC} built onDone={() => {}} />);
    expect(screen.getByText(/The build has started, so a change to the request is a/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "new request" })).toHaveAttribute("href", "/intake");
    expect(apiMock.changeRequest).not.toHaveBeenCalled();
  });

  it("opens the editor prefilled with the current description", async () => {
    const user = userEvent.setup();
    render(<ChangeRequest id="CLN-1" description={DESC} built={false} onDone={() => {}} />);
    await user.click(screen.getByRole("button", { name: "Change the request" }));
    expect(screen.getByLabelText("What you're asking for")).toHaveValue(DESC);
    expect(screen.getByText(/If a decision is waiting, it goes back to its agents/)).toBeInTheDocument();
  });

  it("disables save until the text changes and is at least 10 chars", async () => {
    const user = userEvent.setup();
    render(<ChangeRequest id="CLN-1" description={DESC} built={false} onDone={() => {}} />);
    await user.click(screen.getByRole("button", { name: "Change the request" }));
    const box = screen.getByLabelText("What you're asking for");
    const save = screen.getByRole("button", { name: "Save and update the work" });
    expect(save).toBeDisabled();
    await user.clear(box);
    await user.type(box, "short");
    expect(save).toBeDisabled();
    await user.clear(box);
    await user.type(box, "Add a CSV export and a PDF export");
    expect(save).toBeEnabled();
  });

  it("saves, reports the plain outcome and calls onDone", async () => {
    const user = userEvent.setup();
    const onDone = vi.fn();
    apiMock.changeRequest.mockResolvedValue({ feature_id: "CLN-1", sent_back: "", note: "" });
    render(<ChangeRequest id="CLN-1" description={DESC} built={false} onDone={onDone} />);
    await user.click(screen.getByRole("button", { name: "Change the request" }));
    await user.clear(screen.getByLabelText("What you're asking for"));
    await user.type(screen.getByLabelText("What you're asking for"), "Add a CSV export and a PDF export");
    await user.click(screen.getByRole("button", { name: "Save and update the work" }));
    expect(apiMock.changeRequest).toHaveBeenCalledWith("CLN-1", "Add a CSV export and a PDF export");
    expect(onDone).toHaveBeenCalled();
    expect(await screen.findByText(/Changed\. The next step uses it\./)).toBeInTheDocument();
  });

  it("names the phase sent back when the change goes to the agents", async () => {
    const user = userEvent.setup();
    apiMock.changeRequest.mockResolvedValue({ feature_id: "CLN-1", sent_back: "spec", note: "" });
    render(<ChangeRequest id="CLN-1" description={DESC} built={false} onDone={() => {}} />);
    await user.click(screen.getByRole("button", { name: "Change the request" }));
    await user.clear(screen.getByLabelText("What you're asking for"));
    await user.type(screen.getByLabelText("What you're asking for"), "Add a CSV export and a PDF export");
    await user.click(screen.getByRole("button", { name: "Save and update the work" }));
    expect(
      await screen.findByText(/Changed\. SPEC went back to the agents with your change\./),
    ).toBeInTheDocument();
  });

  it("shows a save error and keeps the editor open", async () => {
    const user = userEvent.setup();
    apiMock.changeRequest.mockRejectedValue(new Error("Save failed"));
    render(<ChangeRequest id="CLN-1" description={DESC} built={false} onDone={() => {}} />);
    await user.click(screen.getByRole("button", { name: "Change the request" }));
    await user.clear(screen.getByLabelText("What you're asking for"));
    await user.type(screen.getByLabelText("What you're asking for"), "Add a CSV export and a PDF export");
    await user.click(screen.getByRole("button", { name: "Save and update the work" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Save failed");
    expect(screen.getByLabelText("What you're asking for")).toBeInTheDocument();
  });

  it("cancels without saving", async () => {
    const user = userEvent.setup();
    render(<ChangeRequest id="CLN-1" description={DESC} built={false} onDone={() => {}} />);
    await user.click(screen.getByRole("button", { name: "Change the request" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByLabelText("What you're asking for")).not.toBeInTheDocument();
    expect(apiMock.changeRequest).not.toHaveBeenCalled();
  });
});
