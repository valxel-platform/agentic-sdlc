import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import type { ScreensAtRelease } from "../api";
import { ScreensBuilt } from "./ScreensBuilt";

function built(over: Partial<ScreensAtRelease> = {}): ScreensAtRelease {
  return {
    scanned: true,
    state: "passed",
    screens: [
      {
        id: "M1",
        name: "Visits list",
        app: "web",
        staging: { route: "/visits", desktop: "shot", phone: "shot" },
        violations: [],
      },
      {
        id: "M2",
        name: "Visit detail",
        app: "web",
        violations: [
          {
            fingerprint: "v1",
            severity: "critical",
            rule: "color-contrast",
            title: "M2: Low contrast on the header",
          },
          { fingerprint: "v2", severity: "low", rule: "label", title: "M2: Missing label" },
        ],
      },
    ],
    ...over,
  };
}

describe("ScreensBuilt", () => {
  it("renders screens with mockup, staging screenshot and phone link", () => {
    render(<ScreensBuilt featureId="CLN-1" s={built()} />);
    expect(
      screen.getByRole("heading", { name: "The screens, as approved and as built" }),
    ).toBeInTheDocument();
    expect(screen.getByTitle("Approved mockup of M1")).toHaveAttribute(
      "src",
      "/api/features/CLN-1/mockups/M1",
    );
    expect(screen.getByRole("img", { name: "M1 Visits list in staging" })).toHaveAttribute(
      "src",
      "/api/features/CLN-1/screens/M1/desktop",
    );
    expect(screen.getByRole("link", { name: "phone" })).toHaveAttribute(
      "href",
      "/api/features/CLN-1/screens/M1/phone",
    );
    expect(screen.getByText("/visits")).toBeInTheDocument();
    expect(screen.getByText("No accessibility problems found")).toBeInTheDocument();
  });

  it("renders violation chips with severities and stripped titles", () => {
    render(<ScreensBuilt featureId="CLN-1" s={built()} />);
    expect(screen.getByText("Critical")).toBeInTheDocument();
    expect(screen.getByText("Minor")).toBeInTheDocument();
    expect(screen.getByText(/color-contrast: Low contrast on the header/)).toBeInTheDocument();
    expect(screen.getByText(/label: Missing label/)).toBeInTheDocument();
    expect(screen.getByText("No screenshot")).toBeInTheDocument();
  });

  it("notes when no staging scan reported", () => {
    render(<ScreensBuilt featureId="CLN-1" s={built({ scanned: false })} />);
    expect(screen.getByText(/No staging scan reported for these screens/)).toBeInTheDocument();
  });

  it("renders nothing extra for a screen with no staging and no violations", () => {
    render(
      <ScreensBuilt
        featureId="CLN-1"
        s={built({ screens: [{ id: "M9", name: "Phone only", app: "web", violations: [] }] })}
      />,
    );
    expect(screen.getByText("No screenshot")).toBeInTheDocument();
    expect(screen.queryByText("No accessibility problems found")).not.toBeInTheDocument();
  });
});
