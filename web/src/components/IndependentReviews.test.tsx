import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { IndependentReviews } from "./IndependentReviews";

describe("IndependentReviews", () => {
  it("shows each PR's verdict and findings", () => {
    render(
      <IndependentReviews
        reviews={[
          {
            repo: "acme/api",
            number: 7,
            head_sha: "abc",
            state: "failure",
            description: "Changes requested: 1 blocking, 0 major (high risk)",
            at: "2026-11-22T00:00:00Z",
            product_id: "CLN",
            summary: "Adds an endpoint without an authorization check.",
            findings: [
              { severity: "blocking", file: "src/api.py", line: 12, issue: "No authorization check" },
            ],
          },
        ]}
      />,
    );
    expect(screen.getByText(/acme\/api#7: changes requested/)).toBeInTheDocument();
    expect(screen.getByText("src/api.py:12")).toBeInTheDocument();
  });

  it("shows nothing without reviews", () => {
    const { container } = render(<IndependentReviews reviews={[]} />);
    expect(container).toBeEmptyDOMElement();
  });
});
