import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import { GcpConnectForm } from "./GcpConnectForm";

vi.mock("../api", () => ({ api: { gcpLaunch: vi.fn(), gcpCheck: vi.fn() } }));

describe("GcpConnectForm", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.open = vi.fn() as never;
  });

  it("opens Cloud Shell and shows the command for this workspace", async () => {
    const user = userEvent.setup();
    vi.mocked(api.gcpLaunch).mockResolvedValue({
      url: "https://shell.cloud.google.com/cloudshell/editor?x=1",
      command: "terraform init && terraform apply -var workspace=ws1",
      mode: "full",
    });
    render(<GcpConnectForm ws="ws1" onDone={vi.fn()} onError={vi.fn()} />);
    await user.selectOptions(screen.getByLabelText("Mode"), "full");
    await user.selectOptions(screen.getByLabelText("Region"), "europe-west1");
    await user.click(screen.getByRole("button", { name: "Open Cloud Shell with the connect template" }));
    await waitFor(() => expect(api.gcpLaunch).toHaveBeenCalledWith("ws1", "full", "europe-west1"));
    expect(window.open).toHaveBeenCalledWith(
      "https://shell.cloud.google.com/cloudshell/editor?x=1",
      "_blank",
      "noopener",
    );
    expect(await screen.findByLabelText("Connect command")).toHaveTextContent("-var workspace=ws1");
  });

  it("checks only a valid project id, and reports the result", async () => {
    const user = userEvent.setup();
    const onDone = vi.fn();
    vi.mocked(api.gcpCheck).mockResolvedValue({ account: "acme-prod" } as never);
    render(<GcpConnectForm ws="ws1" onDone={onDone} onError={vi.fn()} />);
    const check = screen.getByRole("button", { name: "Check the connection" });
    await user.type(screen.getByLabelText("Your Google Cloud project ID"), "No");
    expect(check).toBeDisabled();
    await user.clear(screen.getByLabelText("Your Google Cloud project ID"));
    await user.type(screen.getByLabelText("Your Google Cloud project ID"), "acme-prod");
    await user.click(check);
    await waitFor(() => expect(api.gcpCheck).toHaveBeenCalledWith("ws1", "acme-prod", "lite"));
    expect(onDone).toHaveBeenCalledWith("Connected Google Cloud project acme-prod.");
  });

  it("shows a refusal", async () => {
    const user = userEvent.setup();
    const onError = vi.fn();
    vi.mocked(api.gcpCheck).mockRejectedValue(new Error("set up for another workspace"));
    render(<GcpConnectForm ws="ws1" onDone={vi.fn()} onError={onError} />);
    await user.type(screen.getByLabelText("Your Google Cloud project ID"), "acme-prod");
    await user.click(screen.getByRole("button", { name: "Check the connection" }));
    await waitFor(() => expect(onError).toHaveBeenCalledWith("set up for another workspace"));
  });
});
