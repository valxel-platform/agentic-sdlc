import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { api, type Lesson, type LessonsOverview, type Me } from "../api";
import { LessonsPage } from "./Lessons";

vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  const mocked = Object.fromEntries(Object.keys(actual.api).map((k) => [k, vi.fn()]));
  return { ...actual, api: mocked };
});

type MockFn = { mockRejectedValue: (e: unknown) => unknown };

function defaultRejectAll() {
  for (const fn of Object.values(api)) {
    (fn as unknown as MockFn).mockRejectedValue(new Error("not mocked"));
  }
}

const me: Me = {
  login: "ada",
  name: "Ada",
  avatar_url: "",
  orgs: [],
  teams: [],
  lanes: [
    { id: "cln", name: "Clinic" },
    { id: "ops", name: "Ops" },
  ],
};

function lesson(overrides: Record<string, unknown> = {}): Lesson {
  return {
    id: "L-1",
    phase: "design",
    text: "Every spec says how long data is kept.",
    check: true,
    sources: [],
    pr: "https://example.com/pr/7",
    feature_id: "CLN-1",
    why: "remember",
    ...overrides,
  } as Lesson;
}

function overviewFixture(overrides: Record<string, unknown> = {}): LessonsOverview {
  return {
    product_id: "cln",
    proposed: [lesson()],
    approved: [
      lesson({
        id: "L-2",
        phase: "build",
        text: "Pin dependency versions.",
        check: false,
        pr: "https://example.com/pr/8",
        feature_id: "CLN-2",
        why: undefined,
      }),
      lesson({
        id: "L-3",
        phase: "mystery",
        text: "Unknown phase lesson.",
        check: false,
        pr: undefined,
        feature_id: undefined,
        why: undefined,
      }),
    ],
    corrections: 5,
    ...overrides,
  } as unknown as LessonsOverview;
}

function renderLessons(initial = "/lessons?product=cln", meOverride: Me = me) {
  return render(
    <MemoryRouter initialEntries={[initial]}>
      <LessonsPage me={meOverride} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  defaultRejectAll();
});

afterEach(() => cleanup());

describe("LessonsPage", () => {
  it("shows the no-lane message when the user has no lanes", () => {
    renderLessons("/lessons", { ...me, lanes: [] });
    expect(screen.getByText("You aren't a member of any product's organization.")).toBeInTheDocument();
    expect(vi.mocked(api.lessons)).not.toHaveBeenCalled();
  });

  it("shows an error alert when the lessons load fails", async () => {
    vi.mocked(api.lessons).mockRejectedValue(new Error("lessons down"));
    renderLessons();
    expect(await screen.findByRole("alert")).toHaveTextContent("lessons down");
  });

  it("renders proposed and approved lessons with counts, phases and links", async () => {
    vi.mocked(api.lessons).mockResolvedValue(overviewFixture());
    renderLessons();
    expect(await screen.findByRole("heading", { name: "Waiting for review (1)" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "In use (2)" })).toBeInTheDocument();
    expect(screen.getByText("Every spec says how long data is kept.")).toBeInTheDocument();
    expect(screen.getByText(/Someone asked to remember it\./)).toBeInTheDocument();
    expect(screen.getByText("Design")).toBeInTheDocument();
    expect(screen.getByText("On the checklist")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Review the pull request" })).toHaveAttribute(
      "href",
      "https://example.com/pr/7",
    );
    expect(screen.getByRole("link", { name: "CLN-1" })).toHaveAttribute("href", "/features/CLN-1");
    expect(screen.getByText("Pin dependency versions.")).toBeInTheDocument();
    expect(screen.getByText("Build")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Pull request" })).toHaveAttribute(
      "href",
      "https://example.com/pr/8",
    );
    // Unknown phase falls back to the raw phase string.
    expect(screen.getByText("mystery")).toBeInTheDocument();
    expect(screen.getByText("5 corrections recorded for this product so far.")).toBeInTheDocument();
  });

  it("shows the empty states for both lists", async () => {
    vi.mocked(api.lessons).mockResolvedValue(overviewFixture({ proposed: [], approved: [], corrections: 0 }));
    renderLessons();
    expect(await screen.findByText(/Nothing to review/)).toBeInTheDocument();
    expect(screen.getByText(/No lessons yet/)).toBeInTheDocument();
    expect(screen.getByText("0 corrections recorded for this product so far.")).toBeInTheDocument();
  });

  it("uses the singular correction wording for one correction", async () => {
    vi.mocked(api.lessons).mockResolvedValue(overviewFixture({ proposed: [], approved: [], corrections: 1 }));
    renderLessons();
    expect(await screen.findByText("1 correction recorded for this product so far.")).toBeInTheDocument();
  });

  it("shows the why text for takeover and repeat proposals", async () => {
    vi.mocked(api.lessons).mockResolvedValue(
      overviewFixture({
        proposed: [
          lesson({ id: "L-9", why: "takeover" }),
          lesson({ id: "L-10", why: "repeat", text: "Repeat lesson." }),
        ],
        approved: [],
      }),
    );
    renderLessons();
    expect(await screen.findByText(/Someone took the phase over and rewrote it\./)).toBeInTheDocument();
    expect(screen.getByText(/The same correction came up on several features\./)).toBeInTheDocument();
  });

  it("switching the product refetches", async () => {
    const user = userEvent.setup();
    vi.mocked(api.lessons).mockResolvedValue(overviewFixture());
    renderLessons();
    await screen.findByText("Every spec says how long data is kept.");
    await user.selectOptions(screen.getByLabelText("Product"), "ops");
    await waitFor(() => expect(vi.mocked(api.lessons)).toHaveBeenCalledWith("ops"));
  });
});
