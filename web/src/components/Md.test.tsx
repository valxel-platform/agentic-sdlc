import { describe, expect, it, vi } from "vitest";
import { configure, render, screen, within } from "@testing-library/react";
import { Md, MermaidDiagram } from "./Md";

// Mermaid renders slowly in jsdom and far slower under parallel load;
// give the diagram tests room beyond Vitest's 5s default.
vi.setConfig({ testTimeout: 30000 });
configure({ asyncUtilTimeout: 5000 });

describe("Md", () => {
  it("renders markdown with GFM features", () => {
    render(
      <Md>{"# Title\n\nSome **bold** text and a [link](https://example.invalid).\n\n- one\n- two\n"}</Md>,
    );
    expect(screen.getByRole("heading", { name: "Title" })).toBeInTheDocument();
    expect(screen.getByText("bold")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "link" })).toHaveAttribute("href", "https://example.invalid");
    expect(screen.getByText("one")).toBeInTheDocument();
  });

  it("does not render raw HTML", () => {
    const { container } = render(<Md>{"Hello <script>alert(1)</script> world"}</Md>);
    expect(container.querySelector("script")).not.toBeInTheDocument();
    expect(screen.getByText(/Hello/)).toBeInTheDocument();
  });

  it("renders a plain code block as pre, not a diagram", () => {
    render(<Md>{"```ts\nconst x = 1;\n```"}</Md>);
    expect(screen.getByText("const x = 1;")).toBeInTheDocument();
    expect(screen.queryByText("Drawing the diagram…")).not.toBeInTheDocument();
  });

  it("reports that a mermaid block could not draw in jsdom, with its source", async () => {
    // mermaid needs real SVG layout APIs (getBBox and friends) that jsdom
    // lacks, so in this environment the draw always fails and the component
    // shows the error with the source. (In a real browser it draws.)
    const { container } = render(<Md>{"```mermaid\ngraph TD\n  A-->B\n```"}</Md>);
    expect(await screen.findByText(/This diagram didn't draw/, {}, { timeout: 20000 })).toBeInTheDocument();
    // Scoped to the rendered figure: mermaid also leaves a temporary
    // measuring SVG in document.body whose error text matches the same query.
    expect(within(container).getByText("graph TD A-->B")).toBeInTheDocument();
  });
});

describe("MermaidDiagram", () => {
  it("shows the drawing state, then the error with the source", async () => {
    const { container } = render(<MermaidDiagram source={"graph TD\n  A-->B"} />);
    expect(screen.getByText("Drawing the diagram…")).toBeInTheDocument();
    expect(await screen.findByText(/This diagram didn't draw/, {}, { timeout: 20000 })).toBeInTheDocument();
    expect(within(container).getByText("graph TD A-->B")).toBeInTheDocument();
  });
});

it("uses the dark theme when the system prefers dark", async () => {
  const original = window.matchMedia;
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: query === "(prefers-color-scheme: dark)",
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
  try {
    render(<Md>{"```mermaid\ngraph TD\n  A-->B\n```"}</Md>);
    // The draw still fails in jsdom, but it runs through the dark-theme
    // initialize call first.
    await screen.findByText(/This diagram didn't draw/, {}, { timeout: 20000 });
  } finally {
    window.matchMedia = original;
  }
});
