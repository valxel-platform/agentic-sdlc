import { beforeEach, describe, expect, it, vi } from "vitest";
import { configure, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import type { Feature } from "../api";
import { apiMock, resetApiMock } from "../test/apiMock";
import {
  Delivery,
  DesignReviewPanel,
  Diagrams,
  NewRepos,
  RepoPlans,
  SimilarList,
  WorkItems,
} from "./External";

// Multi-step user-event flows and async fetches exceed Vitest's 5s default
// under parallel load on the shared VM; give them room.
vi.setConfig({ testTimeout: 20000 });
configure({ asyncUtilTimeout: 5000 });

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock };
});

function renderWithRouter(ui: React.ReactElement) {
  return render(<MemoryRouter>{ui}</MemoryRouter>);
}

beforeEach(() => {
  resetApiMock();
});

describe("Diagrams", () => {
  it("renders nothing for no diagrams", () => {
    const { container } = renderWithRouter(<Diagrams diagrams={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("lists versions newest first with a Latest chip", () => {
    renderWithRouter(
      <Diagrams
        diagrams={[
          {
            provider: "lucid",
            title: "v1",
            version: 1,
            url: "https://example.invalid/d/1",
            at: "2026-11-01T00:00:00Z",
          },
          {
            provider: "lucid",
            title: "v2",
            version: 2,
            url: "https://example.invalid/d/2",
            file: "arch.png",
            file_url: "https://example.invalid/arch.png",
            at: "2026-11-02T00:00:00Z",
          },
        ]}
      />,
    );
    expect(screen.getByRole("heading", { name: "Architecture diagrams" })).toBeInTheDocument();
    const links = screen.getAllByRole("link");
    expect(links[0]).toHaveTextContent("Version 2 in Lucid");
    expect(links[0]).toHaveAttribute("href", "https://example.invalid/d/2");
    expect(screen.getByText("Latest")).toBeInTheDocument();
    expect(screen.getByText("v1")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "arch.png" })).toHaveAttribute(
      "href",
      "https://example.invalid/arch.png",
    );
  });

  it("notes an offline stand-in and an unknown provider", () => {
    renderWithRouter(
      <Diagrams
        diagrams={[
          {
            provider: "mystery",
            title: "Diagram",
            version: 1,
            url: "https://example.invalid/d/1",
            stub: true,
            at: "2026-11-01T00:00:00Z",
          },
        ]}
      />,
    );
    expect(screen.getByText("Version 1 in mystery")).toBeInTheDocument();
    expect(screen.getByText(/Offline stand-in: Lucid isn't connected/)).toBeInTheDocument();
  });
});

describe("WorkItems", () => {
  const work = {
    provider: "jira",
    project: "CLN",
    items: [
      {
        item: "1",
        kind: "epic" as const,
        key: "CLN-1",
        url: "https://example.invalid/jira/CLN-1",
        summary: "[CLN] Export epic",
        status: "todo" as const,
      },
      {
        item: "2",
        kind: "story" as const,
        key: "CLN-2",
        url: "",
        summary: "Export story",
        status: "in_progress" as const,
      },
    ],
    error: null,
  };

  it("renders items with kind, status and stripped summary", () => {
    renderWithRouter(<WorkItems featureId="CLN-1" work={work} canSync onSynced={() => {}} />);
    expect(screen.getByRole("heading", { name: /Jira items/ })).toBeInTheDocument();
    expect(screen.getByText("Export epic")).toBeInTheDocument();
    expect(screen.getByText("To do")).toBeInTheDocument();
    expect(screen.getByText("In progress")).toBeInTheDocument();
    expect(screen.getByText("Epic")).toBeInTheDocument();
    expect(screen.getByText("Story")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "CLN-1" })).toHaveAttribute(
      "href",
      "https://example.invalid/jira/CLN-1",
    );
  });

  it("says when the epic is not created yet", () => {
    renderWithRouter(
      <WorkItems featureId="CLN-1" work={{ ...work, items: [] }} canSync onSynced={() => {}} />,
    );
    expect(
      screen.getByText("The epic is created in Jira when the intent is approved at G1."),
    ).toBeInTheDocument();
  });

  it("syncs when allowed and reports the error", async () => {
    const user = userEvent.setup();
    const onSynced = vi.fn();
    renderWithRouter(<WorkItems featureId="CLN-1" work={work} canSync onSynced={onSynced} />);
    await user.click(screen.getByRole("button", { name: "Sync now" }));
    expect(apiMock.syncWork).toHaveBeenCalledWith("CLN-1");
    expect(onSynced).toHaveBeenCalled();

    apiMock.syncWork.mockRejectedValue(new Error("Jira is down"));
    await user.click(screen.getByRole("button", { name: "Sync now" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't update Jira: Jira is down.");
  });

  it("hides the sync button without permission or without anything to sync", () => {
    renderWithRouter(<WorkItems featureId="CLN-1" work={work} canSync={false} onSynced={() => {}} />);
    expect(screen.queryByRole("button", { name: "Sync now" })).not.toBeInTheDocument();
    renderWithRouter(
      <WorkItems featureId="CLN-1" work={{ ...work, items: [], error: null }} canSync onSynced={() => {}} />,
    );
    expect(screen.queryByRole("button", { name: "Sync now" })).not.toBeInTheDocument();
  });

  it("falls back to raw provider and status names", () => {
    renderWithRouter(
      <WorkItems
        featureId="CLN-1"
        work={
          {
            provider: "mystery",
            project: "CLN",
            items: [
              {
                item: "1",
                kind: "story",
                key: "MYS-1",
                url: "",
                summary: "Mystery story",
                status: "mystery",
              },
            ],
            error: null,
          } as never
        }
        canSync
        onSynced={() => {}}
      />,
    );
    expect(screen.getByRole("heading", { name: /mystery items/ })).toBeInTheDocument();
    expect(screen.getByText("Mystery story")).toBeInTheDocument();
    expect(screen.getByText("mystery")).toBeInTheDocument();
  });

  it("shows the work-tracking error from the server", () => {
    renderWithRouter(
      <WorkItems
        featureId="CLN-1"
        work={{ ...work, error: "Jira token expired" }}
        canSync
        onSynced={() => {}}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Couldn't update Jira: Jira token expired. Agents carry on; the next step syncs again.",
    );
  });
});

describe("Delivery", () => {
  function featureWith(over: Partial<Feature>): Feature {
    return {
      feature_id: "CLN-1",
      product_id: "CLN",
      title: "T",
      status: "running",
      running: true,
      rail: [],
      cost_usd: 0,
      budget_usd: 0,
      pending: [],
      updated_at: "",
      description: "",
      constitution_version: "v1",
      artifacts: {},
      pull_requests: {},
      change_set: {},
      release: {},
      gate_decisions: [],
      scores: {},
      loop_count: {},
      human_active_minutes: 0,
      usage: { calls: 0, cache_hit_rate: 0, est_cost_usd: 0, cost_by_phase: {} },
      events: [],
      diagrams: [],
      delivery: {},
      deployments: {},
      quality: {},
      waivers: [],
      new_repos: [],
      runs: {},
      work_tracking: null,
      ...over,
    } as Feature;
  }

  it("renders nothing with no stages and no deployments", () => {
    const { container } = renderWithRouter(<Delivery f={featureWith({})} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders stage results and deployments", () => {
    renderWithRouter(
      <Delivery
        f={featureWith({
          delivery: {
            ci: {
              status: "success",
              wait: "ci",
              at: "2026-11-01T00:00:00Z",
              results: [{ repo: "org/svc", state: "success", url: "https://example.invalid/ci/1" }],
            },
            build: {
              status: "failure",
              wait: "build",
              at: "2026-11-01T00:00:00Z",
              results: [{ repo: "org/svc", state: "failure", message: "npm failed" }],
            },
          },
          deployments: { a: { repo: "org/svc", component: "svc", env: "production", release: "v1.2.0" } },
        })}
      />,
    );
    expect(screen.getByRole("heading", { name: "Delivery" })).toBeInTheDocument();
    expect(screen.getByText("CI checks")).toBeInTheDocument();
    expect(screen.getByText("Passed")).toBeInTheDocument();
    expect(screen.getByText("OpenChoreo build")).toBeInTheDocument();
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(screen.getByText(/npm failed/)).toBeInTheDocument();
    expect(screen.getAllByText("svc")).toHaveLength(3);
    expect(screen.getByText("production, release v1.2.0")).toBeInTheDocument();
  });

  it("falls back on missing status, results, repo and release", () => {
    renderWithRouter(
      <Delivery
        f={featureWith({
          delivery: {
            ci: { wait: "ci", at: "2026-11-01T00:00:00Z", results: [{ component: "svc", state: "failure" }] },
            build: { status: "mystery", wait: "build", at: "2026-11-01T00:00:00Z", results: undefined },
          } as never,
          deployments: { a: { repo: "org/svc", component: "svc", env: "production" } },
        })}
      />,
    );
    expect(screen.getByRole("heading", { name: "Delivery" })).toBeInTheDocument();
    // No status on the CI stage means it is still in progress.
    expect(screen.getByText("CI checks")).toBeInTheDocument();
    expect(screen.getByText("In progress")).toBeInTheDocument();
    // A result with a component but no repo falls back to the component,
    // and a failed result without a message shows its state.
    // "svc" appears twice: once in the CI stage row (component fallback),
    // once in the deployment list.
    expect(screen.getAllByText("svc")).toHaveLength(2);
    expect(screen.getByText(/: failure/)).toBeInTheDocument();
    // An unknown build status renders raw.
    expect(screen.getByText("mystery")).toBeInTheDocument();
    // A deployment without a release shows just the environment.
    expect(screen.getByText("production")).toBeInTheDocument();
  });

  it("renders stages with no deployments at all", () => {
    renderWithRouter(
      <Delivery
        f={featureWith({
          delivery: { ci: { status: "success", wait: "ci", at: "2026-11-01T00:00:00Z", results: [] } },
          deployments: undefined,
        } as never)}
      />,
    );
    expect(screen.getByRole("heading", { name: "Delivery" })).toBeInTheDocument();
    expect(screen.getByText("Passed")).toBeInTheDocument();
  });

  it("shows a waiting external stage in progress", () => {
    renderWithRouter(
      <Delivery
        f={featureWith({
          pending: [
            {
              interrupt_id: "int-1",
              type: "external",
              wait: "dev",
              approvers: [],
              can_decide: false,
              targets: [{ repo: "org/svc", url: "https://example.invalid/dev/1", number: 3 }],
            } as never,
          ],
        })}
      />,
    );
    expect(screen.getByText("Dev")).toBeInTheDocument();
    expect(screen.getByText("Deploying to dev")).toBeInTheDocument();
    expect(screen.getByText("svc #3")).toBeInTheDocument();
  });
});

describe("SimilarList", () => {
  it("links features, bolds capabilities, and flags duplicates", () => {
    renderWithRouter(
      <SimilarList
        items={[
          {
            kind: "feature",
            id: "CLN-2",
            title: "CSV export v1",
            status: "released",
            stage: "",
            score: 0.9,
            duplicate: true,
          },
          {
            kind: "capability",
            id: "CAP-1",
            title: "Export capability",
            status: "in_progress",
            stage: "build",
            score: 0.6,
            duplicate: false,
          },
        ]}
      />,
    );
    expect(screen.getByRole("link", { name: "CLN-2" })).toHaveAttribute("href", "/features/CLN-2");
    expect(screen.getByText("Likely the same")).toBeInTheDocument();
    expect(screen.getByText("CAP-1")).toBeInTheDocument();
    expect(screen.getByText(/\(in progress, at build\)/)).toBeInTheDocument();
  });
});

it("falls back to in progress for an unknown status with no stage", () => {
  renderWithRouter(
    <SimilarList
      items={
        [
          {
            kind: "feature",
            id: "CLN-9",
            title: "Odd one",
            status: "mystery",
            stage: "",
            score: 0.1,
            duplicate: false,
          },
        ] as never
      }
    />,
  );
  expect(screen.getByText(/\(in progress\)/)).toBeInTheDocument();
});

describe("DesignReviewPanel", () => {
  it("renders findings, builds-on, contracts and the record", () => {
    render(
      <DesignReviewPanel
        review={{
          findings: ["Adds a new table."],
          builds_on: [{ id: "ADR-1", title: "Use Postgres", status: "accepted" }],
          supersedes: [{ adr: "ADR-9", supersedes: "ADR-2", title: "New storage" }],
          contracts: [
            { repo: "org/api", current: "v1", next: "v2", breaking: true, note: "" },
            { repo: "org/other", current: "", next: "", breaking: false, note: "no contract found" },
          ],
          related: [{ id: "ADR-3", title: "Caching", status: "accepted" }],
          in_flight: [
            {
              feature: "CLN-2",
              title: "Other export",
              repos: ["org/api"],
              contracts: [{ repo: "org/api", next: "v3" }],
            },
          ],
          record: { repo: "org/arch", sha: "abcdef123456" },
          errors: { diagram: "timeout" },
        }}
      />,
    );
    expect(screen.getByText("Against the existing design")).toBeInTheDocument();
    expect(screen.getByText("Adds a new table.")).toBeInTheDocument();
    expect(screen.getByText("ADR-1")).toBeInTheDocument();
    expect(screen.getByText("ADR-9 replaces ADR-2 (New storage)")).toBeInTheDocument();
    expect(screen.getByText("api: v1 to v2")).toBeInTheDocument();
    expect(screen.getByText("Breaking")).toBeInTheDocument();
    expect(screen.getByText("other: no contract found")).toBeInTheDocument();
    expect(screen.getByText("ADR-3")).toBeInTheDocument();
    expect(screen.getByText(/CLN-2 \(Other export\) also changes api to v3/)).toBeInTheDocument();
    expect(
      screen.getByText(/Read from org\/arch@abcdef1\. Couldn't read: diagram \(timeout\)\./),
    ).toBeInTheDocument();
  });

  it("falls back on a record without a sha, a supersedes entry without a title, and in-flight repos without contracts", () => {
    render(
      <DesignReviewPanel
        review={
          {
            findings: [],
            builds_on: [],
            supersedes: [{ adr: "ADR-9", supersedes: "ADR-2" }],
            contracts: [],
            related: [],
            in_flight: [
              { feature: "CLN-2", title: "Other export", repos: ["org/api"], contracts: undefined },
            ],
            record: { repo: "org/arch" },
            errors: undefined,
          } as never
        }
      />,
    );
    expect(screen.getByText("ADR-9 replaces ADR-2")).toBeInTheDocument();
    expect(screen.getByText(/CLN-2 \(Other export\) also changes api/)).toBeInTheDocument();
    expect(screen.getByText(/Read from org\/arch@\./)).toBeInTheDocument();
    expect(screen.queryByText(/Couldn't read/)).not.toBeInTheDocument();
  });

  it("says when there is no architecture record and nothing to build on", () => {
    render(
      <DesignReviewPanel
        review={{
          findings: [],
          builds_on: [],
          supersedes: [],
          contracts: [],
          related: [],
          in_flight: [],
          record: {},
          errors: {},
        }}
      />,
    );
    expect(screen.getByText("No existing decisions")).toBeInTheDocument();
    expect(screen.getByText("None")).toBeInTheDocument();
    expect(screen.getByText("This product has no architecture record yet.")).toBeInTheDocument();
  });
});

describe("RepoPlans", () => {
  it("renders each plan with defaults filled in", () => {
    render(
      <RepoPlans
        plans={[
          {
            repo: "org/visits-svc",
            role: "service",
            purpose: "Serves visits",
            template: "template-service",
            visibility: "private",
            ruleset: { required_approvals: 2, required_checks: ["ci", "lint"] },
            teams: { backend: "write" },
            security: { secret_scanning: true, push_protection: false },
          },
          { repo: "org/plain", role: "library", purpose: "Shared code" },
        ]}
      />,
    );
    expect(screen.getByText("org/visits-svc")).toBeInTheDocument();
    expect(
      screen.getByText(/From template-service, private\. Merges need 2 approvals and checks ci, lint\./),
    ).toBeInTheDocument();
    expect(screen.getByText(/Turns on secret scanning\. Teams: backend \(write\)\./)).toBeInTheDocument();
    expect(
      screen.getByText(/From an empty repo, private\. Merges need 1 approval and checks none\./),
    ).toBeInTheDocument();
  });
});

describe("NewRepos", () => {
  it("renders nothing for no repos", () => {
    const { container } = render(<NewRepos repos={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders provisioned and failed repos with steps and governance PR", () => {
    render(
      <NewRepos
        repos={[
          {
            repo: "org/visits-svc",
            role: "service",
            purpose: "Serves visits",
            status: "provisioned",
            url: "https://example.invalid/org/visits-svc",
            steps: [
              { step: "create", ok: true },
              { step: "ruleset", ok: false, error: "timeout" },
            ],
            governance_pr: "https://example.invalid/pr/9",
          },
          { repo: "org/broken", role: "service", purpose: "Broken", status: "failed" },
        ]}
      />,
    );
    expect(screen.getByRole("heading", { name: "New repositories" })).toBeInTheDocument();
    expect(screen.getByText("Ready")).toBeInTheDocument();
    expect(screen.getByText("Setup failed")).toBeInTheDocument();
    expect(screen.getByText("create")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "this pull request" })).toHaveAttribute(
      "href",
      "https://example.invalid/pr/9",
    );
  });
});

it("renders contract fallbacks and in-flight repos without matching contracts", () => {
  render(
    <DesignReviewPanel
      review={{
        findings: [],
        builds_on: [],
        supersedes: [],
        contracts: [
          { repo: "org/api", current: "v1", next: "", breaking: false, note: "" },
          { repo: "org/other", current: "", next: "", breaking: false, note: "" },
        ],
        related: [],
        in_flight: [
          {
            feature: "CLN-2",
            title: "Other export",
            repos: ["org/api"],
            contracts: [{ repo: "org/unrelated", next: "v9" }],
          },
        ],
        record: {},
        errors: {},
      }}
    />,
  );
  expect(screen.getByText("api: v1 to ?")).toBeInTheDocument();
  expect(screen.getByText("other: no contract found")).toBeInTheDocument();
  expect(screen.getByText(/CLN-2 \(Other export\) also changes api/)).toBeInTheDocument();
});

it("renders unknown security flags by their key", () => {
  render(
    <RepoPlans
      plans={[{ repo: "org/x", role: "service", purpose: "X", security: { mystery_flag: true } }]}
    />,
  );
  expect(screen.getByText(/Turns on mystery_flag\./)).toBeInTheDocument();
});
