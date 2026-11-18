import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { CostTable } from "./Costs";

describe("CostTable", () => {
  it("says so when nothing is recorded", () => {
    render(<CostTable rows={[]} total={0} caption="Build cost" />);
    expect(screen.getByText("Nothing recorded yet.")).toBeInTheDocument();
  });

  it("renders rows with basis wording and the total", () => {
    render(
      <CostTable
        rows={[
          { category: "llm", label: "Model calls", usd: 5, basis: ["metered"], provisional: false },
          {
            category: "aws",
            label: "AWS services",
            usd: 2.5,
            basis: ["billed", "allocated"],
            provisional: true,
          },
          { category: "ci", label: "CI minutes", usd: 0.75, basis: ["estimate"], provisional: false },
          { category: "other", label: "Other", usd: 0.1, basis: ["mystery"], provisional: false },
        ]}
        total={8.35}
        caption="Build cost by category"
      />,
    );
    expect(screen.getByText("Model calls")).toBeInTheDocument();
    expect(screen.getByText("usage at list price")).toBeInTheDocument();
    expect(screen.getByText("from the AWS bill, shared out, may still change")).toBeInTheDocument();
    expect(screen.getByText("estimate")).toBeInTheDocument();
    expect(screen.getByText("mystery")).toBeInTheDocument();
    expect(screen.getByText("$5.00")).toBeInTheDocument();
    expect(screen.getByText("$2.50")).toBeInTheDocument();
    expect(screen.getByText("$0.750")).toBeInTheDocument();
    expect(screen.getByText("$0.100")).toBeInTheDocument();
    expect(screen.getByText("Total")).toBeInTheDocument();
    expect(screen.getByText("$8.35")).toBeInTheDocument();
  });
});
