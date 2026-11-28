import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { api, type SupportDetail } from "../api";
import { SupportPage } from "./Support";

vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  const mocked = Object.fromEntries(Object.keys(actual.api).map((k) => [k, vi.fn()]));
  return { ...actual, api: mocked };
});

const grafana = {
  to: "grafana" as const,
  label: "Dashboards, logs and traces",
  detail: "Grafana: requests, errors and latency per route; logs; request traces",
  href: "/api/support/open?product=FARM&to=grafana",
  role: "Viewer" as const,
};

function detail(over: Partial<SupportDetail> = {}): SupportDetail {
  return { product: "FARM", links: [grafana], grafana_role: "Viewer", mlflow: false, ...over };
}

describe("SupportPage", () => {
  beforeEach(() => {
    vi.mocked(api.support).mockResolvedValue({
      products: [{ product: "FARM", name: "Farm", links: [grafana] }],
      configured: true,
    });
  });
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("opens a product's Grafana in a new window, through the tool", async () => {
    vi.mocked(api.supportProduct).mockResolvedValue(detail());
    render(<SupportPage />);
    const link = await screen.findByRole("link", { name: /open dashboards/i });
    expect(link.getAttribute("href")).toBe("/api/support/open?product=FARM&to=grafana");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toContain("noopener");
    expect(await screen.findByText(/only for groups a workspace admin allows/)).toBeTruthy();
    expect(screen.queryByText(/who may open agent traces/i)).toBeNull();
  });

  it("lets an admin choose who may open agent traces", async () => {
    vi.mocked(api.supportProduct).mockResolvedValue(
      detail({
        grafana_role: "Editor",
        settings: {
          product: "FARM",
          mlflow_groups: [],
          version: 0,
          groups: [
            { slug: "support", name: "Support" },
            { slug: "on-call", name: "On-call" },
          ],
        },
      }),
    );
    vi.mocked(api.setSupport).mockResolvedValue({
      product: "FARM",
      mlflow_groups: ["support"],
      version: 1,
      groups: [],
    });
    render(<SupportPage />);
    const save = await screen.findByRole("button", { name: "Save trace access" });
    expect((save as HTMLButtonElement).disabled).toBe(true);
    await userEvent.click(screen.getByLabelText("Support"));
    await userEvent.click(save);
    await waitFor(() => expect(api.setSupport).toHaveBeenCalledWith("FARM", ["support"]));
    expect(await screen.findByText(/from their next sign-in/)).toBeTruthy();
  });

  it("says who to ask when there's nothing to support", async () => {
    vi.mocked(api.support).mockResolvedValue({ products: [], configured: false });
    render(<SupportPage />);
    expect(await screen.findByText("No products to support")).toBeTruthy();
    expect(screen.getByRole("status").textContent).toMatch(/isn't set up/);
  });
});
