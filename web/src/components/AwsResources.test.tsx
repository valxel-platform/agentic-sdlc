import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import type { AwsResources } from "../api";
import { AlarmsFiring, AwsResourcesTable } from "./AwsResources";

function aws(over: Partial<AwsResources> = {}): AwsResources {
  return {
    resources: [
      {
        name: "visits-db",
        type: "database",
        service: "RDS",
        stateful: true,
        parameters: {},
        access: { app: ["read", "write"], worker: ["delete"] },
        sizes: { dev: "small", production: "large" },
        monthly_usd: { dev: 10, production: 100 },
        alarms: [{ name: "cpu-high", severity: "high", says: "CPU above 80%" }],
      },
      {
        name: "cache",
        type: "cache",
        service: "ElastiCache",
        stateful: false,
        parameters: {},
        access: { app: ["read"] },
        sizes: { dev: "tiny", production: "medium" },
        monthly_usd: { dev: 2, production: 20 },
      },
    ],
    monthly_usd: { dev: 12, production: 120 },
    region: "us-east-1",
    accounts: {},
    note: "Planning figures.",
    ...over,
  };
}

describe("AlarmsFiring", () => {
  it("renders each alarm with severity, time and link", () => {
    render(
      <AlarmsFiring
        alarms={[
          {
            env: "staging",
            resource: "visits-db",
            alarm: "cpu",
            severity: "high",
            says: "CPU above 90% for 10 minutes",
            since: "2026-11-01T00:00:00Z",
            url: "https://example.invalid/cloudwatch",
            title: "High CPU",
          },
          {
            env: "dev",
            resource: "cache",
            alarm: "mem",
            severity: "low",
            says: "Memory high",
            since: "2026-11-02T00:00:00Z",
            url: "",
            title: "High memory",
          },
        ]}
      />,
    );
    expect(screen.getByText("High CPU")).toBeInTheDocument();
    expect(screen.getByText(/in staging, HIGH, since/)).toBeInTheDocument();
    expect(screen.getByText("CPU above 90% for 10 minutes")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "Open in CloudWatch" });
    expect(link).toHaveAttribute("href", "https://example.invalid/cloudwatch");
    expect(screen.getByText("High memory")).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "Open in CloudWatch" })).toHaveLength(1);
  });
});

describe("AwsResourcesTable", () => {
  it("renders services, access, per-env sizes and monthly totals", () => {
    render(<AwsResourcesTable aws={aws()} />);
    expect(screen.getByText("visits-db")).toBeInTheDocument();
    expect(screen.getByText(/RDS, keeps data/)).toBeInTheDocument();
    expect(screen.getByText("ElastiCache")).toBeInTheDocument();
    expect(screen.getByText("app reads and writes; worker deletes")).toBeInTheDocument();
    expect(screen.getByText("app reads")).toBeInTheDocument();
    expect(screen.getByText("small, $10/mo")).toBeInTheDocument();
    expect(screen.getByText("large, $100/mo")).toBeInTheDocument();
    expect(screen.getByText("Per month")).toBeInTheDocument();
    expect(screen.getByText("$12")).toBeInTheDocument();
    expect(screen.getByText("$120")).toBeInTheDocument();
    expect(
      screen.getByText(/In us-east-1, each environment in its own AWS account\. Planning figures\./),
    ).toBeInTheDocument();
    expect(screen.getByText(/Alarms that come with them/)).toBeInTheDocument();
    expect(screen.getByText(/visits-db \(cpu high\)/)).toBeInTheDocument();
  });

  it("falls back to the product's region when none is set", () => {
    render(<AwsResourcesTable aws={aws({ region: "" })} />);
    expect(screen.getByText(/In the product's region/)).toBeInTheDocument();
  });

  it("omits the alarms paragraph when no resource has alarms", () => {
    render(<AwsResourcesTable aws={aws({ resources: [aws().resources[1]] })} />);
    expect(screen.queryByText(/Alarms that come with them/)).not.toBeInTheDocument();
  });
});
