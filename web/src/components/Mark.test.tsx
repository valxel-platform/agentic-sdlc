import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { Lockup, Mark, Wordmark } from "./Mark";

describe("Mark", () => {
  it("renders an svg mark at the requested size", () => {
    const { container } = render(<Mark size={40} />);
    const svg = container.querySelector("svg.mark");
    expect(svg).toBeInTheDocument();
    expect(svg).toHaveAttribute("width", "40");
    expect(svg).toHaveAttribute("height", "40");
    expect(svg).toHaveAttribute("aria-hidden", "true");
  });

  it("defaults to 26px", () => {
    const { container } = render(<Mark />);
    expect(container.querySelector("svg")).toHaveAttribute("width", "26");
  });

  it("gives each mark its own gradient ids", () => {
    const { container } = render(
      <>
        <Mark />
        <Mark />
      </>,
    );
    const ids = [...container.querySelectorAll("linearGradient")].map((g) => g.id);
    expect(new Set(ids).size).toBe(ids.length);
  });
});

describe("Wordmark", () => {
  it("renders the wordmark svg", () => {
    const { container } = render(<Wordmark />);
    const svg = container.querySelector("svg.wordmark");
    expect(svg).toBeInTheDocument();
    expect(svg).toHaveAttribute("height", "14");
  });
});

describe("Lockup", () => {
  it("renders mark, wordmark and the accessible product name", () => {
    const { container } = render(<Lockup />);
    expect(container.querySelector("svg.mark")).toBeInTheDocument();
    expect(container.querySelector("svg.wordmark")).toBeInTheDocument();
    expect(screen.getByText("Valxel Agentic SDLC")).toBeInTheDocument();
    expect(screen.getByText("Agentic SDLC")).toBeInTheDocument();
  });
});
