import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Draft, IntakeSession } from "../api";
import { DraftCloudAccounts as CloudAccounts, WorkspaceCloudAccounts } from "./CloudAccounts";

const apiMock = vi.hoisted(() => ({
  intakeDraft: vi.fn(),
  cloudOnboarding: vi.fn(),
  connections: vi.fn(),
  gcpLaunch: vi.fn(),
  awsLaunch: vi.fn(),
  azureLaunch: vi.fn(),
  connectionPoll: vi.fn(),
  workspaceCloudAccounts: vi.fn(),
  setWorkspaceCloudAccounts: vi.fn(),
}));
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock };
});

function session(draft: Partial<Draft> = {}, over: Partial<IntakeSession> = {}): IntakeSession {
  return {
    id: "s1",
    owner: "ada",
    lanes: [],
    workspace: "acme",
    cloud: "gcp",
    draft: {
      product_id: "",
      title: "",
      problem: "",
      users: "",
      success_metric: "",
      related_capability: "",
      similar: [],
      not_duplicate_reason: "",
      ui: false,
      experience: {},
      size: "product",
      size_suggested: "",
      size_why: "",
      horizon: "",
      research: "",
      name: "CLN",
      org: "acme",
      budget_usd: 0,
      cloud: "",
      cloud_accounts: {},
      region: "",
      ...draft,
    },
    ready: false,
    missing: ["cloud"],
    attachments: [],
    feature_id: "",
    created_at: "",
    starter: true,
    kind: "",
    awaiting: "",
    messages: [],
    ...over,
  };
}

const GIVEN = {
  cloud: "given" as const,
  cloud_accounts: { non_production: "acme-cln-np", production: "acme-cln-prod" },
  region: "us-central1",
};

beforeEach(() => {
  Object.values(apiMock).forEach((m) => m.mockReset());
  apiMock.connections.mockResolvedValue([]);
  apiMock.workspaceCloudAccounts.mockResolvedValue({
    cloud: "gcp",
    name: "Google Cloud",
    noun: "project",
    planes: { non_production: "acme-nonprod", production: "acme-prod" },
  });
  vi.spyOn(window, "open").mockImplementation(() => null);
});
afterEach(() => vi.useRealTimers());

describe("CloudAccounts", () => {
  it("saves the two IDs and the region once all three are in", async () => {
    const onSaved = vi.fn();
    apiMock.intakeDraft.mockResolvedValue(session(GIVEN));
    render(<CloudAccounts s={session()} ws="acme" disabled={false} onSaved={onSaved} />);
    fireEvent.change(screen.getByLabelText("Non-production project"), { target: { value: "acme-cln-np" } });
    fireEvent.blur(screen.getByLabelText("Non-production project"));
    expect(apiMock.intakeDraft).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("Production project"), { target: { value: "acme-cln-prod" } });
    fireEvent.change(screen.getByLabelText("Region"), { target: { value: "us-central1" } });
    fireEvent.blur(screen.getByLabelText("Region"));
    await waitFor(() =>
      expect(apiMock.intakeDraft).toHaveBeenCalledWith("s1", {
        cloud: "non-production: acme-cln-np, production: acme-cln-prod, region: us-central1",
      }),
    );
    expect(onSaved).toHaveBeenCalled();
  });

  it("says what's wrong, and can be set up later", async () => {
    apiMock.intakeDraft.mockRejectedValueOnce(new Error("Production needs a project of its own"));
    apiMock.intakeDraft.mockResolvedValueOnce(session({ cloud: "later" }));
    render(<CloudAccounts s={session(GIVEN)} ws="acme" disabled={false} onSaved={vi.fn()} />);
    fireEvent.blur(screen.getByLabelText("Region"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Production needs a project of its own");
    fireEvent.click(screen.getByLabelText("Set them up later"));
    await waitFor(() => expect(apiMock.intakeDraft).toHaveBeenLastCalledWith("s1", { cloud: "later" }));
  });

  it("shows the cloud's own steps for creating them", async () => {
    apiMock.cloudOnboarding.mockResolvedValue({
      cloud: "gcp",
      name: "Google Cloud",
      noun: "project",
      links: {},
      steps: ["Create two projects for CLN"],
    });
    render(<CloudAccounts s={session()} ws="acme" disabled={false} onSaved={vi.fn()} />);
    fireEvent.click(screen.getByText("How do I create these projects?"));
    expect(await screen.findByText("Create two projects for CLN")).toBeInTheDocument();
  });

  it("connects each account with the template filled in, then watches until it connects", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    apiMock.connections.mockResolvedValue([
      { tool: "gcp", connections: [{ account: "acme-cln-prod", status: "connected" }] },
    ]);
    apiMock.gcpLaunch.mockResolvedValue({
      url: "https://shell",
      command: "terraform apply -var project=acme-cln-np",
      mode: "full",
    });
    apiMock.connectionPoll.mockResolvedValue({ status: "connected" });
    render(<CloudAccounts s={session(GIVEN)} ws="acme" disabled={false} onSaved={vi.fn()} />);
    expect(await screen.findByText("Connected")).toBeInTheDocument(); // production already was
    fireEvent.click(screen.getByText("Connect"));
    await waitFor(() =>
      expect(apiMock.gcpLaunch).toHaveBeenCalledWith("acme", "full", "us-central1", "acme-cln-np"),
    );
    expect(window.open).toHaveBeenCalledWith("https://shell", "_blank", "noopener");
    expect(screen.getByLabelText("Connect command for acme-cln-np")).toHaveTextContent("project=acme-cln-np");
    await act(async () => {
      vi.advanceTimersByTime(10_000);
    });
    await waitFor(() => expect(screen.getAllByText("Connected")).toHaveLength(2));
    expect(apiMock.connectionPoll).toHaveBeenCalledWith("acme", "gcp", "acme-cln-np");
  });

  it("on AWS says which account to sign in to", async () => {
    apiMock.awsLaunch.mockResolvedValue({
      url: "https://console",
      mode: "full",
      sign_in: "Sign in to AWS account 111111111111",
    });
    apiMock.connectionPoll.mockResolvedValue({ status: "waiting" });
    const aws = session(
      {
        cloud: "given",
        cloud_accounts: { non_production: "111111111111", production: "222222222222" },
        region: "us-east-1",
      },
      { cloud: "aws" },
    );
    render(<CloudAccounts s={aws} ws="acme" disabled={false} onSaved={vi.fn()} />);
    fireEvent.click(screen.getAllByText("Connect")[0]);
    expect(await screen.findByText("Sign in to AWS account 111111111111")).toBeInTheDocument();
    expect(apiMock.awsLaunch).toHaveBeenCalledWith("acme", "full", "us-east-1", "111111111111");
  });

  it("offers the workspace's shared clusters first when it has them", async () => {
    apiMock.workspaceCloudAccounts.mockResolvedValue({
      cloud: "gcp",
      name: "Google Cloud",
      noun: "project",
      accounts: { non_production: "acme-np", production: "acme-p" },
      region: "us-central1",
      planes: { non_production: "acme-nonprod", production: "acme-prod" },
    });
    apiMock.intakeDraft.mockResolvedValue(session({ cloud: "shared", placement: "shared" }));
    render(<CloudAccounts s={session()} ws="acme" disabled={false} onSaved={vi.fn()} />);
    const share = await screen.findByLabelText("Share acme's clusters (cheapest)");
    expect(screen.queryByLabelText("Non-production project")).not.toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Its own projects"));
    await waitFor(() => expect(apiMock.intakeDraft).toHaveBeenLastCalledWith("s1", { cloud: "dedicated" }));
    expect(screen.getByLabelText("Non-production project")).toBeInTheDocument();
    fireEvent.click(share);
    await waitFor(() => expect(apiMock.intakeDraft).toHaveBeenLastCalledWith("s1", { cloud: "shared" }));
    expect(screen.getByText(/PHI or payments data always gets its own/)).toBeInTheDocument();
  });

  it("lets a workspace admin set the shared accounts", async () => {
    apiMock.setWorkspaceCloudAccounts.mockResolvedValue({
      cloud: "gcp",
      name: "Google Cloud",
      noun: "project",
      accounts: { non_production: "acme-np", production: "acme-p" },
      region: "us-central1",
      planes: { non_production: "acme-nonprod", production: "acme-prod" },
    });
    render(<WorkspaceCloudAccounts ws="acme" admin />);
    expect(await screen.findByText("Shared projects")).toBeInTheDocument();
    expect(screen.getByText(/acme-nonprod and acme-prod/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Set them up later")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Non-production project"), { target: { value: "acme-np" } });
    fireEvent.change(screen.getByLabelText("Production project"), { target: { value: "acme-p" } });
    fireEvent.change(screen.getByLabelText("Region"), { target: { value: "us-central1" } });
    fireEvent.blur(screen.getByLabelText("Region"));
    await waitFor(() =>
      expect(apiMock.setWorkspaceCloudAccounts).toHaveBeenCalledWith(
        "acme",
        "non-production: acme-np, production: acme-p, region: us-central1",
      ),
    );
    expect(await screen.findAllByText("Connect")).toHaveLength(2);
  });
});
