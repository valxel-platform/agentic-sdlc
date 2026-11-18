import { beforeEach, describe, expect, it, vi } from "vitest";
import { configure, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { apiMock, resetApiMock } from "../test/apiMock";
import { CommentableDoc, DecisionBrief } from "./DecisionBrief";

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

describe("DecisionBrief", () => {
  it("renders nothing while loading or when the brief fails", () => {
    apiMock.decisionBrief.mockReturnValue(new Promise(() => {}));
    const { container } = render(<DecisionBrief featureId="CLN-1" interruptId="int-1" />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when the brief fails to load", async () => {
    apiMock.decisionBrief.mockRejectedValue(new Error("gone"));
    const { container } = render(<DecisionBrief featureId="CLN-1" interruptId="int-1" />);
    await new Promise((r) => setTimeout(r, 50));
    expect(container).toBeEmptyDOMElement();
  });

  it("renders the summary lines and who wrote them", async () => {
    apiMock.decisionBrief.mockResolvedValue({
      summary: { lines: ["A CSV export for the visits list.", "It covers clinics only."], by: "agent" },
      changed: null,
    });
    render(<DecisionBrief featureId="CLN-1" interruptId="int-1" />);
    expect(await screen.findByText("In two minutes")).toBeInTheDocument();
    expect(screen.getByText("A CSV export for the visits list.")).toBeInTheDocument();
    expect(screen.getByText("Summarised by an agent from the document below.")).toBeInTheDocument();
  });

  it("says the summary is taken from the document when the agent did not write it", async () => {
    apiMock.decisionBrief.mockResolvedValue({
      summary: { lines: ["A line."], by: "doc" },
      changed: null,
    });
    render(<DecisionBrief featureId="CLN-1" interruptId="int-1" />);
    expect(await screen.findByText("Taken from the document below.")).toBeInTheDocument();
  });

  it("renders the diff of what changed since the sent-back version", async () => {
    apiMock.decisionBrief.mockResolvedValue({
      summary: { lines: [], by: "agent" },
      changed: {
        from_version: 2,
        lines: [
          { op: "+", text: "Added the measure." },
          { op: "-", text: "Removed the old scope." },
          { op: "…", text: "" },
          { op: " ", text: "Unchanged line." },
        ],
        asked: "Add a measure",
      },
    });
    render(<DecisionBrief featureId="CLN-1" interruptId="int-1" />);
    expect(await screen.findByText(/Changed since version 2/)).toBeInTheDocument();
    expect(screen.getByText("(2 lines)")).toBeInTheDocument();
    expect(screen.getByText(/You asked: “Add a measure”/)).toBeInTheDocument();
    expect(screen.getByText("Added the measure.")).toBeInTheDocument();
    expect(screen.getByText("Removed the old scope.")).toBeInTheDocument();
    expect(screen.getByText("Unchanged line.")).toBeInTheDocument();
  });

  it("says when nothing changed in the text", async () => {
    apiMock.decisionBrief.mockResolvedValue({
      summary: { lines: [], by: "agent" },
      changed: { from_version: 3, lines: [], asked: "" },
    });
    render(<DecisionBrief featureId="CLN-1" interruptId="int-1" />);
    expect(await screen.findByText("No changes in the text.")).toBeInTheDocument();
  });
});

describe("CommentableDoc", () => {
  beforeEach(() => {
    apiMock.artifact.mockResolvedValue({
      id: "PRD",
      content: "# Product spec\n\nThe export lists visits.\n\n- One column per field",
      version: 1,
      constitution_version: "v1",
    });
  });

  it("shows loading, then the document lines", async () => {
    render(<CommentableDoc featureId="CLN-1" kind="PRD" comments={[]} onAdd={() => {}} />);
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(await screen.findByText("Product spec")).toBeInTheDocument();
    expect(screen.getByText("The export lists visits.")).toBeInTheDocument();
    expect(screen.getByText("One column per field")).toBeInTheDocument();
  });

  it("adds a comment on a line", async () => {
    const user = userEvent.setup();
    const onAdd = vi.fn();
    render(<CommentableDoc featureId="CLN-1" kind="PRD" comments={[]} onAdd={onAdd} />);
    await user.click(await screen.findByRole("button", { name: "Comment on: Product spec" }));
    const box = screen.getByLabelText("Comment on this line");
    await user.type(box, "Say what CSV means here");
    await user.click(screen.getByRole("button", { name: "Add" }));
    expect(onAdd).toHaveBeenCalledWith({ quote: "Product spec", text: "Say what CSV means here" });
  });

  it("disables Add with empty text and cancels the form", async () => {
    const user = userEvent.setup();
    render(<CommentableDoc featureId="CLN-1" kind="PRD" comments={[]} onAdd={() => {}} />);
    await user.click(await screen.findByRole("button", { name: "Comment on: The export lists visits." }));
    expect(screen.getByRole("button", { name: "Add" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByLabelText("Comment on this line")).not.toBeInTheDocument();
  });

  it("marks lines that already have a comment", async () => {
    render(
      <CommentableDoc
        featureId="CLN-1"
        kind="PRD"
        comments={[{ quote: "Product spec", text: "Earlier note" }]}
        onAdd={() => {}}
      />,
    );
    const line = await screen.findByText("Product spec");
    expect(line.closest("li")).toHaveClass("noted");
  });

  it("renders an empty document when the artifact fails", async () => {
    apiMock.artifact.mockRejectedValue(new Error("gone"));
    const { container } = render(
      <CommentableDoc featureId="CLN-1" kind="PRD" comments={[]} onAdd={() => {}} />,
    );
    await new Promise((r) => setTimeout(r, 50));
    expect(container.querySelector("ol")).toBeInTheDocument();
    expect(container.querySelector("ol")?.children).toHaveLength(0);
  });
});
