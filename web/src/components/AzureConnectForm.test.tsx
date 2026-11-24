import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import { AzureConnectForm } from "./AzureConnectForm";

vi.mock("../api", () => ({ api: { azureLaunch: vi.fn(), azureCheck: vi.fn() } }));

const SUB = "1b2c3d4e-0000-4000-8000-00000000aa01";
const DEPLOY = "2b2c3d4e-0000-4000-8000-00000000bb02";
const AGENTS = "3b2c3d4e-0000-4000-8000-00000000cc03";

describe("AzureConnectForm", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.open = vi.fn() as never;
  });

  it("opens Cloud Shell and shows the command for this workspace", async () => {
    const user = userEvent.setup();
    vi.mocked(api.azureLaunch).mockResolvedValue({
      url: "https://shell.azure.com/bash",
      command: "az deployment sub create --parameters workspace=ws1",
      mode: "full",
    });
    render(<AzureConnectForm ws="ws1" onDone={vi.fn()} onError={vi.fn()} />);
    await user.selectOptions(screen.getByLabelText("Mode"), "full");
    await user.selectOptions(screen.getByLabelText("Region"), "westeurope");
    await user.click(screen.getByRole("button", { name: "Open Cloud Shell with the connect template" }));
    await waitFor(() => expect(api.azureLaunch).toHaveBeenCalledWith("ws1", "full", "westeurope"));
    expect(window.open).toHaveBeenCalledWith("https://shell.azure.com/bash", "_blank", "noopener");
    expect(await screen.findByLabelText("Connect command")).toHaveTextContent("workspace=ws1");
  });

  it("checks only when all three IDs are GUIDs, and reports the result", async () => {
    const user = userEvent.setup();
    const onDone = vi.fn();
    vi.mocked(api.azureCheck).mockResolvedValue({ account: SUB } as never);
    render(<AzureConnectForm ws="ws1" onDone={onDone} onError={vi.fn()} />);
    const check = screen.getByRole("button", { name: "Check the connection" });
    await user.type(screen.getByLabelText("Subscription ID"), SUB);
    await user.type(screen.getByLabelText("Deploy client ID"), DEPLOY);
    expect(check).toBeDisabled();
    await user.type(screen.getByLabelText("Agents client ID"), AGENTS.toUpperCase());
    await user.click(check);
    await waitFor(() =>
      expect(api.azureCheck).toHaveBeenCalledWith("ws1", {
        subscription: SUB,
        deploy_client_id: DEPLOY,
        agents_client_id: AGENTS,
        mode: "lite",
      }),
    );
    expect(onDone).toHaveBeenCalledWith(`Connected Azure subscription ${SUB}.`);
  });

  it("shows a refusal", async () => {
    const user = userEvent.setup();
    const onError = vi.fn();
    vi.mocked(api.azureCheck).mockRejectedValue(new Error("set up for another workspace"));
    render(<AzureConnectForm ws="ws1" onDone={vi.fn()} onError={onError} />);
    await user.type(screen.getByLabelText("Subscription ID"), SUB);
    await user.type(screen.getByLabelText("Deploy client ID"), DEPLOY);
    await user.type(screen.getByLabelText("Agents client ID"), AGENTS);
    await user.click(screen.getByRole("button", { name: "Check the connection" }));
    await waitFor(() => expect(onError).toHaveBeenCalledWith("set up for another workspace"));
  });
});
