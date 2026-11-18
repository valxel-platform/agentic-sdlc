import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { apiMock, resetApiMock } from "../test/apiMock";
import { ArtifactBody } from "./Artifact";

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock };
});

beforeEach(() => {
  resetApiMock();
});

describe("ArtifactBody", () => {
  it("shows loading, then the rendered markdown", async () => {
    apiMock.artifact.mockResolvedValue({
      id: "PRD",
      content: "# Spec\n\nThe export lists visits.",
      version: 2,
      constitution_version: "v1",
    });
    render(<ArtifactBody featureId="CLN-1" kind="PRD" />);
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(await screen.findByText("The export lists visits.")).toBeInTheDocument();
    expect(apiMock.artifact).toHaveBeenCalledWith("CLN-1", "PRD");
  });

  it("shows an error when the artifact fails to load", async () => {
    apiMock.artifact.mockRejectedValue(new Error("Artifact gone"));
    render(<ArtifactBody featureId="CLN-1" kind="PRD" />);
    expect(await screen.findByText("Artifact gone")).toBeInTheDocument();
  });

  it("reloads when the kind changes", async () => {
    apiMock.artifact.mockResolvedValue({
      id: "PRD",
      content: "Body.",
      version: 1,
      constitution_version: "v1",
    });
    const { rerender } = render(<ArtifactBody featureId="CLN-1" kind="PRD" />);
    await screen.findByText("Body.");
    rerender(<ArtifactBody featureId="CLN-1" kind="OPP" />);
    expect(apiMock.artifact).toHaveBeenCalledWith("CLN-1", "OPP");
  });
});
