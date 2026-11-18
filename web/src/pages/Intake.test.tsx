import { beforeEach, describe, expect, it, vi } from "vitest";
import { configure, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { Draft, IntakeSession, Me } from "../api";
import { apiMock, resetApiMock } from "../test/apiMock";
import { Intake } from "./Intake";

// Multi-step user-event flows and async fetches exceed Vitest's 5s default
// under parallel load on the shared VM; give them room.
vi.setConfig({ testTimeout: 20000 });
configure({ asyncUtilTimeout: 5000 });

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock };
});
vi.mock("../live", () => ({ useLive: () => {}, useLiveStatus: () => true, closeLive: vi.fn() }));

const me: Me = {
  login: "ada",
  name: "Ada",
  avatar_url: "",
  orgs: [],
  teams: [],
  lanes: [
    { id: "CLN", name: "Clinic" },
    { id: "PAY", name: "Payments" },
  ],
};

function draft(over: Partial<Draft> = {}): Draft {
  return {
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
    size: "feature",
    size_suggested: "",
    size_why: "",
    horizon: "",
    research: "",
    name: "",
    org: "",
    budget_usd: 0,
    ...over,
  };
}

function session(over: Partial<IntakeSession> = {}): IntakeSession {
  return {
    id: "sess-1",
    owner: "ada",
    lanes: ["CLN", "PAY"],
    draft: draft(),
    ready: false,
    missing: ["product_id", "problem", "users", "success_metric"],
    attachments: [],
    feature_id: "",
    created_at: "2026-11-01T00:00:00Z",
    starter: false,
    kind: "",
    awaiting: "",
    messages: [{ role: "agent", text: "Hi — what do you want to build?", at: "2026-11-01T00:00:00Z" }],
    ...over,
  };
}

/** Render Intake at /intake/:sid (or /intake for the entry test). */
function renderIntake(sid: string | null = "sess-1", route?: string) {
  const start = route ?? (sid ? `/intake/${sid}` : "/intake");
  render(
    <MemoryRouter initialEntries={[start]}>
      <Routes>
        <Route path="/intake" element={<Intake me={me} />} />
        <Route path="/intake/:sid" element={<Intake me={me} />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  resetApiMock();
  apiMock.products.mockResolvedValue({
    CLN: {
      name: "Clinic",
      kind: "product",
      ok: true,
      profiles: ["feature", "express"],
      default_profile: "feature",
    },
    PAY: { name: "Payments", kind: "product", ok: true, profiles: ["feature"], default_profile: "feature" },
  });
});

describe("entry and loading states", () => {
  it("shows the no-lanes empty state when the person has no products", async () => {
    render(
      <MemoryRouter initialEntries={["/intake/sess-1"]}>
        <Routes>
          <Route path="/intake/:sid" element={<Intake me={{ ...me, lanes: [] }} />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(await screen.findByText("You can't submit intents yet")).toBeInTheDocument();
  });

  it("creates a session and redirects from /intake", async () => {
    const s = session();
    apiMock.intakeStart.mockResolvedValue(s);
    apiMock.intake.mockResolvedValue(s);
    renderIntake(null);
    expect(apiMock.intakeStart).toHaveBeenCalledWith();
    expect(await screen.findByText("Hi — what do you want to build?")).toBeInTheDocument();
    expect(apiMock.intake).toHaveBeenCalledWith("sess-1");
  });

  it("shows the starting state while the session loads", () => {
    apiMock.intake.mockReturnValue(new Promise(() => {}));
    renderIntake();
    expect(screen.getByText("Starting…")).toBeInTheDocument();
  });

  it("shows a load error as an alert", async () => {
    apiMock.intake.mockRejectedValue(new Error("Session gone"));
    renderIntake();
    expect(await screen.findByRole("alert")).toHaveTextContent("Session gone");
  });

  it("still renders when the product profiles fail to load", async () => {
    apiMock.products.mockRejectedValue(new Error("nope"));
    apiMock.intake.mockResolvedValue(session());
    renderIntake();
    expect(await screen.findByText("Hi — what do you want to build?")).toBeInTheDocument();
  });
});

describe("chat", () => {
  it("sends a message and shows the agent thinking", async () => {
    const user = userEvent.setup();
    const s = session();
    const replied = session({
      messages: [
        ...s.messages,
        { role: "user", text: "Add a CSV export", at: "2026-11-01T00:01:00Z" },
        { role: "agent", text: "Got it. Which product?", at: "2026-11-01T00:01:05Z" },
      ],
    });
    apiMock.intake.mockResolvedValue(s);
    apiMock.intakeSay.mockResolvedValue(replied);
    renderIntake();
    await screen.findByText("Hi — what do you want to build?");
    const box = screen.getByLabelText("Message to the intake agent");
    await user.type(box, "Add a CSV export");
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(apiMock.intakeSay).toHaveBeenCalledWith("sess-1", "Add a CSV export");
    expect(await screen.findByText("Got it. Which product?")).toBeInTheDocument();
    // The optimistic user message is in the transcript.
    expect(screen.getAllByText("Add a CSV export").length).toBeGreaterThan(0);
  });

  it("disables send when the box is empty", async () => {
    apiMock.intake.mockResolvedValue(session());
    renderIntake();
    await screen.findByText("Hi — what do you want to build?");
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
  });

  it("sends on Enter but not on Shift+Enter", async () => {
    const user = userEvent.setup();
    apiMock.intake.mockResolvedValue(session());
    apiMock.intakeSay.mockResolvedValue(session());
    renderIntake();
    const box = await screen.findByLabelText("Message to the intake agent");
    await user.click(box);
    await user.keyboard("hello{Shift>}{Enter}{/Shift}");
    expect(apiMock.intakeSay).not.toHaveBeenCalled();
    await user.keyboard("{Enter}");
    expect(apiMock.intakeSay).toHaveBeenCalledWith("sess-1", "hello");
  });

  it("shows a send error as an alert", async () => {
    const user = userEvent.setup();
    apiMock.intake.mockResolvedValue(session());
    apiMock.intakeSay.mockRejectedValue(new Error("Agent is busy"));
    renderIntake();
    await user.type(await screen.findByLabelText("Message to the intake agent"), "hello");
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Agent is busy");
  });

  it("fills the box from a starter example", async () => {
    const user = userEvent.setup();
    apiMock.intake.mockResolvedValue(session());
    renderIntake();
    const starter = await screen.findByRole("button", { name: /Add a CSV export to the visits list/ });
    await user.click(starter);
    expect(screen.getByLabelText("Message to the intake agent")).toHaveValue(
      "Add a CSV export to the visits list",
    );
  });

  it("sends a quick reply from the agent's options", async () => {
    const user = userEvent.setup();
    const s = session({
      messages: [
        {
          role: "agent",
          text: "Is this for the Clinic product?",
          at: "2026-11-01T00:00:00Z",
          options: ["Yes", "No, another"],
        },
      ],
    });
    apiMock.intake.mockResolvedValue(s);
    apiMock.intakeSay.mockResolvedValue(s);
    renderIntake();
    await user.click(await screen.findByRole("button", { name: "Yes" }));
    expect(apiMock.intakeSay).toHaveBeenCalledWith("sess-1", "Yes");
  });

  it("starts over with a new session", async () => {
    const user = userEvent.setup();
    const s = session();
    const s2 = session({ id: "sess-2" });
    apiMock.intake.mockResolvedValue(s);
    apiMock.intakeStart.mockResolvedValue(s2);
    renderIntake();
    await screen.findByText("Hi — what do you want to build?");
    await user.click(screen.getByRole("button", { name: "Start over" }));
    expect(apiMock.intakeStart).toHaveBeenCalled();
    expect(await screen.findByText("Hi — what do you want to build?")).toBeInTheDocument();
  });

  it("shows the submitted banner with a link to the feature", async () => {
    const s = session({
      feature_id: "CLN-5",
      kind: "feature",
      ready: true,
      missing: [],
      messages: [{ role: "agent", text: "Done.", at: "2026-11-01T00:00:00Z" }],
    });
    apiMock.intake.mockResolvedValue(s);
    renderIntake();
    const link = await screen.findByRole("link", { name: "CLN-5" });
    expect(link).toHaveAttribute("href", "/features/CLN-5");
    expect(screen.getByText(/Agents have started discovery/)).toBeInTheDocument();
    expect(screen.getAllByText("Submitted")).toHaveLength(2);
    expect(screen.getByLabelText("Message to the intake agent")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Submit intent" })).not.toBeInTheDocument();
  });
});

describe("entry-choice cards", () => {
  it("offers a new product or an existing product while the chat is fresh", async () => {
    const user = userEvent.setup();
    const s = session();
    apiMock.intake.mockResolvedValue(s);
    apiMock.intakeDraft.mockResolvedValue(session({ draft: draft({ size: "product" }) }));
    renderIntake();
    const cards = await screen.findByLabelText("New or existing product");
    expect(within(cards).getByRole("button", { name: /A new product/ })).toBeInTheDocument();
    expect(
      within(cards).getByRole("button", { name: /Existing product — add a feature/ }),
    ).toBeInTheDocument();
    await user.click(within(cards).getByRole("button", { name: /A new product/ }));
    expect(apiMock.intakeDraft).toHaveBeenCalledWith("sess-1", { size: "product" });
    await user.click(within(cards).getByRole("button", { name: /Existing product — add a feature/ }));
    expect(apiMock.intakeDraft).toHaveBeenCalledWith("sess-1", { size: "feature" });
  });

  it("hides the entry cards once the person has written", async () => {
    const s = session({
      messages: [
        { role: "agent", text: "Hi.", at: "2026-11-01T00:00:00Z" },
        { role: "user", text: "Add a CSV export", at: "2026-11-01T00:01:00Z" },
      ],
    });
    apiMock.intake.mockResolvedValue(s);
    renderIntake();
    await screen.findByText("Add a CSV export");
    expect(screen.queryByLabelText("New or existing product")).not.toBeInTheDocument();
    expect(screen.queryByText(/Not sure how to start/)).not.toBeInTheDocument();
  });
});

describe("intent panel", () => {
  it("renders the needs checklist with the count in place", async () => {
    apiMock.intake.mockResolvedValue(session());
    renderIntake();
    expect(await screen.findByRole("heading", { name: "The intent" })).toBeInTheDocument();
    expect(screen.getAllByText("0 of 4 in place")).toHaveLength(2);
    expect(screen.getByText("Still needed: Product, Problem, Users, Success measure")).toBeInTheDocument();
  });

  it("switches size through the radio picker", async () => {
    const user = userEvent.setup();
    const s = session();
    apiMock.intake.mockResolvedValue(s);
    apiMock.intakeDraft.mockResolvedValue(session({ draft: draft({ size: "initiative" }) }));
    renderIntake();
    await user.click(await screen.findByRole("radio", { name: "Initiative" }));
    expect(apiMock.intakeDraft).toHaveBeenCalledWith("sess-1", { size: "initiative" });
  });

  it("shows the agent's suggestion when no size is chosen yet", async () => {
    apiMock.intake.mockResolvedValue(
      session({
        draft: draft({ size: "", size_suggested: "initiative", size_why: "it spans several features" }),
      }),
    );
    renderIntake();
    expect(await screen.findByText(/Size: the agent suggests initiative/)).toBeInTheDocument();
    expect(screen.getByText("It spans several features")).toBeInTheDocument();
  });

  it("saves a field edit on blur", async () => {
    const user = userEvent.setup();
    const s = session();
    apiMock.intake.mockResolvedValue(s);
    apiMock.intakeDraft.mockResolvedValue(s);
    renderIntake();
    const problem = await screen.findByLabelText("Problem");
    await user.type(problem, "Visits can't be exported");
    fireEvent.blur(problem);
    expect(apiMock.intakeDraft).toHaveBeenCalledWith("sess-1", { problem: "Visits can't be exported" });
  });

  it("saves the product choice immediately", async () => {
    const user = userEvent.setup();
    const s = session();
    apiMock.intake.mockResolvedValue(s);
    apiMock.intakeDraft.mockResolvedValue(s);
    renderIntake();
    const select = await screen.findByLabelText("Product");
    await user.selectOptions(select, "CLN");
    expect(apiMock.intakeDraft).toHaveBeenCalledWith("sess-1", { product_id: "CLN" });
  });

  it("shows the experience fields only when the intent changes what people see", async () => {
    const user = userEvent.setup();
    const s = session({ draft: draft({ product_id: "CLN", ui: false }) });
    apiMock.intake.mockResolvedValue(s);
    apiMock.intakeDraft.mockResolvedValue(s);
    renderIntake();
    const toggle = await screen.findByLabelText("Changes what people see");
    expect(screen.queryByLabelText("Screen or flow")).not.toBeInTheDocument();
    await user.click(toggle);
    expect(apiMock.intakeDraft).toHaveBeenCalledWith("sess-1", { ui: true });
  });

  it("renders experience fields when ui is on and saves them on blur", async () => {
    const user = userEvent.setup();
    const s = session({
      draft: draft({ product_id: "CLN", ui: true, experience: { where: "Visits list" } }),
      missing: ["users", "success_metric"],
    });
    apiMock.intake.mockResolvedValue(s);
    apiMock.intakeDraft.mockResolvedValue(s);
    renderIntake();
    const where = await screen.findByLabelText("Screen or flow");
    expect(where).toHaveValue("Visits list");
    expect(screen.getByLabelText("Main task")).toBeInTheDocument();
    expect(screen.getAllByText("3 of 5 in place")).toHaveLength(2);
    await user.type(screen.getByLabelText("Main task"), "Export the list");
    fireEvent.blur(screen.getByLabelText("Main task"));
    expect(apiMock.intakeDraft).toHaveBeenCalledWith("sess-1", {
      experience: { main_task: "Export the list" },
    });
  });

  it("edits every field of a new-product intent", async () => {
    const user = userEvent.setup();
    const sized = session({ starter: true, draft: draft({ size: "product" }) });
    apiMock.intake.mockResolvedValue(sized);
    apiMock.intakeDraft.mockResolvedValue(sized);
    renderIntake();
    const name = await screen.findByLabelText("Name");
    await user.type(name, "VetBook");
    fireEvent.blur(name);
    expect(apiMock.intakeDraft).toHaveBeenCalledWith("sess-1", { name: "VetBook" });
    const org = screen.getByLabelText("GitHub org");
    await user.type(org, "vetbook");
    fireEvent.blur(org);
    expect(apiMock.intakeDraft).toHaveBeenCalledWith("sess-1", { org: "vetbook" });
    const idea = screen.getByLabelText("The idea");
    await user.type(idea, "Booking for small vet practices");
    fireEvent.blur(idea);
    expect(apiMock.intakeDraft).toHaveBeenCalledWith("sess-1", {
      problem: "Booking for small vet practices",
    });
    const users = screen.getByLabelText("Users");
    await user.type(users, "Vet receptionists");
    fireEvent.blur(users);
    expect(apiMock.intakeDraft).toHaveBeenCalledWith("sess-1", { users: "Vet receptionists" });
    const goal = screen.getByLabelText("First goal");
    await user.type(goal, "100 bookings a week");
    fireEvent.blur(goal);
    expect(apiMock.intakeDraft).toHaveBeenCalledWith("sess-1", { success_metric: "100 bookings a week" });
    const budget = screen.getByLabelText(/First-year budget/);
    await user.type(budget, "500");
    fireEvent.blur(budget);
    expect(apiMock.intakeDraft).toHaveBeenCalledWith("sess-1", { budget_usd: 500 });
    // Clearing the budget saves zero, not NaN.
    await user.clear(budget);
    fireEvent.blur(budget);
    expect(apiMock.intakeDraft).toHaveBeenCalledWith("sess-1", { budget_usd: 0 });
    await user.selectOptions(screen.getByLabelText("Research first"), "quick");
    expect(apiMock.intakeDraft).toHaveBeenCalledWith("sess-1", { research: "quick" });
  });

  it("edits every field of an initiative intent", async () => {
    const user = userEvent.setup();
    const sized = session({
      draft: draft({ size: "initiative" }),
      lanes: ["CLN", "PAY", "ZZZ"],
      missing: ["product_id", "problem", "users", "success_metric", "research"],
    });
    apiMock.intake.mockResolvedValue(sized);
    apiMock.intakeDraft.mockResolvedValue(sized);
    renderIntake();
    const title = await screen.findByLabelText("Title");
    await user.type(title, "Cut no-shows");
    fireEvent.blur(title);
    expect(apiMock.intakeDraft).toHaveBeenCalledWith("sess-1", { title: "Cut no-shows" });
    await user.selectOptions(screen.getByLabelText("Product"), "CLN");
    expect(apiMock.intakeDraft).toHaveBeenCalledWith("sess-1", { product_id: "CLN" });
    const outcome = screen.getByLabelText("Outcome");
    await user.type(outcome, "Fewer missed appointments");
    fireEvent.blur(outcome);
    expect(apiMock.intakeDraft).toHaveBeenCalledWith("sess-1", { problem: "Fewer missed appointments" });
    const users = screen.getByLabelText(/Users/);
    await user.type(users, "Clinic patients");
    fireEvent.blur(users);
    expect(apiMock.intakeDraft).toHaveBeenCalledWith("sess-1", { users: "Clinic patients" });
    const measure = screen.getByLabelText("Measure");
    await user.type(measure, "No-show rate under 5%");
    fireEvent.blur(measure);
    expect(apiMock.intakeDraft).toHaveBeenCalledWith("sess-1", { success_metric: "No-show rate under 5%" });
    // A lane with no known name falls back to its id in the options.
    expect(screen.getByRole("option", { name: "ZZZ" })).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("When"), "now");
    expect(apiMock.intakeDraft).toHaveBeenCalledWith("sess-1", { horizon: "now" });
    await user.selectOptions(screen.getByLabelText("Research first"), "thorough");
    expect(apiMock.intakeDraft).toHaveBeenCalledWith("sess-1", { research: "thorough" });
  });

  it("edits the title and product of a feature intent", async () => {
    const user = userEvent.setup();
    const base = session({ lanes: ["CLN", "PAY", "ZZZ"], draft: draft({ problem: undefined as never }) });
    apiMock.intake.mockResolvedValue(base);
    apiMock.intakeDraft.mockResolvedValue(base);
    renderIntake();
    const title = await screen.findByLabelText("Title");
    await user.type(title, "Add a CSV export");
    fireEvent.blur(title);
    expect(apiMock.intakeDraft).toHaveBeenCalledWith("sess-1", { title: "Add a CSV export" });
    await user.selectOptions(screen.getByLabelText("Product"), "PAY");
    expect(apiMock.intakeDraft).toHaveBeenCalledWith("sess-1", { product_id: "PAY" });
    expect(screen.getByRole("option", { name: "ZZZ" })).toBeInTheDocument();
  });

  it("delegates the remaining experience fields to the design agent", async () => {
    const user = userEvent.setup();
    const s = session({
      draft: draft({ product_id: "CLN", ui: true, experience: { where: "Visits list" } }),
    });
    apiMock.intake.mockResolvedValue(s);
    apiMock.intakeDraft.mockResolvedValue(s);
    renderIntake();
    await user.click(await screen.findByRole("button", { name: "Leave the rest to the design agent" }));
    expect(apiMock.intakeDraft).toHaveBeenCalledWith(
      "sess-1",
      expect.objectContaining({
        experience: expect.objectContaining({ main_task: "Left to the design agent" }),
      }),
    );
  });
});

describe("initiative and product sizes", () => {
  it("renders the initiative needs and fields", async () => {
    apiMock.intake.mockResolvedValue(
      session({
        draft: draft({
          size: "initiative",
          title: "Cut no-shows",
          problem: "Too many no-shows",
          horizon: "now",
          research: "quick",
        }),
        missing: ["success_metric"],
      }),
    );
    renderIntake();
    expect(await screen.findByRole("heading", { name: "The initiative" })).toBeInTheDocument();
    expect(screen.getByLabelText("Outcome")).toHaveValue("Too many no-shows");
    expect(screen.getByLabelText("When")).toHaveValue("now");
    expect(screen.getAllByText("4 of 5 in place")).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Draft the initiative" })).toBeInTheDocument();
  });

  it("offers to leave the rest to the agent for an initiative", async () => {
    const user = userEvent.setup();
    const s = session({ draft: draft({ size: "initiative" }), missing: ["success_metric", "horizon"] });
    apiMock.intake.mockResolvedValue(s);
    apiMock.intakeSay.mockResolvedValue(s);
    renderIntake();
    await user.click(await screen.findByRole("button", { name: "Leave the rest to the agent" }));
    expect(apiMock.intakeSay).toHaveBeenCalledWith("sess-1", "Leave the rest to the agent");
  });

  it("renders the product fields and warns non-starters", async () => {
    apiMock.intake.mockResolvedValue(
      session({
        draft: draft({
          size: "product",
          name: "Vet books",
          org: "acme-vets",
          problem: "Small vets can't book online",
        }),
        starter: false,
        missing: ["users", "success_metric"],
      }),
    );
    renderIntake();
    expect(await screen.findByRole("heading", { name: "The new product" })).toBeInTheDocument();
    expect(screen.getByLabelText("Name")).toHaveValue("Vet books");
    expect(screen.getByLabelText("GitHub org")).toHaveValue("acme-vets");
    expect(screen.getByLabelText("The idea")).toHaveValue("Small vets can't book online");
    expect(screen.getByText(/Only the inception starters can start a new product/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start the product" })).toBeInTheDocument();
  });

  it("does not warn inception starters about the product size", async () => {
    apiMock.intake.mockResolvedValue(
      session({
        draft: draft({ size: "product" }),
        starter: true,
        missing: ["name", "org", "problem", "users", "success_metric"],
      }),
    );
    renderIntake();
    await screen.findByRole("heading", { name: "The new product" });
    expect(screen.queryByText(/Only the inception starters/)).not.toBeInTheDocument();
  });
});

describe("duplicates", () => {
  it("blocks submit until the person says how it differs", async () => {
    apiMock.intake.mockResolvedValue(
      session({
        draft: draft({
          similar: [
            {
              kind: "feature",
              id: "CLN-2",
              title: "CSV export v1",
              status: "released",
              stage: "",
              score: 0.9,
              duplicate: true,
            },
          ],
        }),
        missing: [],
        ready: false,
      }),
    );
    renderIntake();
    expect(await screen.findByText(/Looks like existing work. Add to it instead/)).toBeInTheDocument();
    expect(screen.getByText("Likely the same")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Submit intent" })).toBeDisabled();
    expect(screen.getByText("Say how this differs from the existing work")).toBeInTheDocument();
  });

  it("shows related work that is not a duplicate", async () => {
    apiMock.intake.mockResolvedValue(
      session({
        draft: draft({
          similar: [
            {
              kind: "feature",
              id: "CLN-2",
              title: "CSV export v1",
              status: "released",
              stage: "",
              score: 0.7,
              duplicate: false,
            },
          ],
          not_duplicate_reason: "",
        }),
      }),
    );
    renderIntake();
    expect(await screen.findByText("Related work the agents will build on")).toBeInTheDocument();
    expect(screen.queryByText("Likely the same")).not.toBeInTheDocument();
  });

  it("shows the reason once the person has given one", async () => {
    apiMock.intake.mockResolvedValue(
      session({
        draft: draft({
          similar: [
            {
              kind: "feature",
              id: "CLN-2",
              title: "CSV export v1",
              status: "released",
              stage: "",
              score: 0.9,
              duplicate: true,
            },
          ],
          not_duplicate_reason: "This one is for clinics, not hospitals.",
        }),
        missing: [],
        ready: true,
      }),
    );
    renderIntake();
    expect(
      await screen.findByText(/You said why it's different: This one is for clinics, not hospitals/),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Submit intent" })).toBeEnabled();
  });
});

describe("submit", () => {
  it("submits with pilot and the chosen profile", async () => {
    const user = userEvent.setup();
    const s = session({ draft: draft({ product_id: "CLN" }), missing: [], ready: true });
    apiMock.intake.mockResolvedValue(s);
    apiMock.intakeSubmit.mockResolvedValue(
      session({ feature_id: "CLN-5", kind: "feature", ready: true, missing: [] }),
    );
    renderIntake();
    const workflow = await screen.findByLabelText(/Workflow/);
    expect(workflow).toHaveValue("feature");
    await user.selectOptions(workflow, "express");
    await user.click(screen.getByLabelText(/Pilot feature/));
    await user.click(screen.getByRole("button", { name: "Submit intent" }));
    expect(apiMock.intakeSubmit).toHaveBeenCalledWith("sess-1", true, "express");
  });

  it("hides the workflow picker when only one profile is allowed", async () => {
    apiMock.intake.mockResolvedValue(
      session({ draft: draft({ product_id: "PAY" }), missing: [], ready: true }),
    );
    renderIntake();
    await screen.findByRole("button", { name: "Submit intent" });
    expect(screen.queryByLabelText("Workflow")).not.toBeInTheDocument();
  });

  it("shows a submit error as an alert", async () => {
    const user = userEvent.setup();
    apiMock.intake.mockResolvedValue(session({ missing: [], ready: true }));
    apiMock.intakeSubmit.mockRejectedValue(new Error("Submit failed"));
    renderIntake();
    await user.click(await screen.findByRole("button", { name: "Submit intent" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Submit failed");
  });
});

describe("attachments", () => {
  it("lists attachments and removes one", async () => {
    const user = userEvent.setup();
    const s = session({
      attachments: [
        { id: "a1", name: "shot.png", kind: "image", size: 2048, read_by_agents: false },
        { id: "a2", name: "notes.txt", kind: "text", size: 120, read_by_agents: true },
      ],
    });
    apiMock.intake.mockResolvedValue(s);
    apiMock.intakeDetach.mockResolvedValue(s);
    renderIntake();
    expect(await screen.findByText("shot.png")).toBeInTheDocument();
    expect(screen.getByText(/Image, 2 KB\. For people; agents see only its name\./)).toBeInTheDocument();
    expect(screen.getByText(/Text, 120 B\. Agents read it at discovery and spec\./)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Remove shot.png" }));
    expect(apiMock.intakeDetach).toHaveBeenCalledWith("sess-1", "a1");
  });

  it("attaches a file from the picker", async () => {
    const s = session();
    apiMock.intake.mockResolvedValue(s);
    const withFile = session({
      attachments: [{ id: "a1", name: "shot.png", kind: "image", size: 2048, read_by_agents: false }],
    });
    apiMock.intakeAttach.mockResolvedValue(withFile);
    renderIntake();
    await screen.findByText("Hi — what do you want to build?");
    const picker = document.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(["fake"], "shot.png", { type: "image/png" });
    fireEvent.change(picker, { target: { files: [file] } });
    expect(apiMock.intakeAttach).toHaveBeenCalledWith("sess-1", file);
    expect(await screen.findByText("shot.png")).toBeInTheDocument();
  });

  it("shows an attach error as an alert", async () => {
    apiMock.intake.mockResolvedValue(session());
    apiMock.intakeAttach.mockRejectedValue(new Error("Too big"));
    renderIntake();
    await screen.findByText("Hi — what do you want to build?");
    const picker = document.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(picker, { target: { files: [new File(["x"], "big.png", { type: "image/png" })] } });
    expect(await screen.findByRole("alert")).toHaveTextContent("Too big");
  });

  it("attaches a dropped file", async () => {
    apiMock.intake.mockResolvedValue(session());
    apiMock.intakeAttach.mockResolvedValue(session());
    renderIntake();
    const chat = await screen.findByLabelText("Conversation with the intake agent");
    const file = new File(["fake"], "drop.png", { type: "image/png" });
    fireEvent.drop(chat, { dataTransfer: { files: [file], types: ["Files"] } });
    expect(apiMock.intakeAttach).toHaveBeenCalledWith("sess-1", file);
  });

  it("does not attach once submitted", async () => {
    apiMock.intake.mockResolvedValue(session({ feature_id: "CLN-5", kind: "feature" }));
    renderIntake();
    expect(await screen.findAllByText("Submitted")).toHaveLength(2);
    const picker = document.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(picker, { target: { files: [new File(["x"], "late.png", { type: "image/png" })] } });
    expect(apiMock.intakeAttach).not.toHaveBeenCalled();
  });
});

describe("attachments, quick replies and change highlights", () => {
  it("highlights the fields the agent changed, then clears the highlight", async () => {
    const user = userEvent.setup();
    const s = session({ draft: draft({ title: "CSV export", problem: "" }) });
    const replied = session({
      draft: draft({ title: "Export visits as CSV", problem: "Exports are manual", product_id: "CLN" }),
      missing: ["users", "success_metric"],
      messages: [
        ...s.messages,
        { role: "user", text: "Add a CSV export", at: "2026-11-01T00:01:00Z" },
        { role: "agent", text: "Filled in the title and problem.", at: "2026-11-01T00:01:05Z" },
      ],
    });
    apiMock.intake.mockResolvedValue(s);
    apiMock.intakeSay.mockResolvedValue(replied);
    renderIntake();
    await screen.findByText("Hi — what do you want to build?");
    await user.type(screen.getByLabelText("Message to the intake agent"), "Add a CSV export");
    await user.click(screen.getByRole("button", { name: "Send" }));
    await screen.findByText("Filled in the title and problem.");
    const titleLabel = document.querySelector("label.ifield.title");
    expect(titleLabel).toHaveClass("lit");
    expect(screen.getByLabelText("Product").closest("label")).toHaveClass("lit");
    await new Promise((r) => setTimeout(r, 2000));
    expect(titleLabel).not.toHaveClass("lit");
  });

  it("highlights the fields the agent changed on an initiative intent", async () => {
    const user = userEvent.setup();
    const s = session({ draft: draft({ size: "initiative", title: "", success_metric: "" }) });
    const replied = session({
      draft: draft({ size: "initiative", title: "Cut no-shows", success_metric: "No-show rate under 5%" }),
      missing: ["users"],
      messages: [
        ...s.messages,
        { role: "user", text: "Cut no-shows", at: "2026-11-01T00:01:00Z" },
        { role: "agent", text: "Filled in the title and measure.", at: "2026-11-01T00:01:05Z" },
      ],
    });
    apiMock.intake.mockResolvedValue(s);
    apiMock.intakeSay.mockResolvedValue(replied);
    renderIntake();
    await screen.findByText("Hi — what do you want to build?");
    await user.type(screen.getByLabelText("Message to the intake agent"), "Cut no-shows");
    await user.click(screen.getByRole("button", { name: "Send" }));
    await screen.findByText("Filled in the title and measure.");
    expect(document.querySelector("label.ifield.title")).toHaveClass("lit");
    expect(screen.getByLabelText("Measure").closest("label")).toHaveClass("lit");
  });

  it("marks the measure delegated when the agent proposes it", async () => {
    apiMock.intake.mockResolvedValue(
      session({ draft: draft({ size: "initiative", success_metric: "Proposed by the agent" }) }),
    );
    renderIntake();
    expect(await screen.findByLabelText("Measure")).toBeInTheDocument();
    expect(screen.getByLabelText("Measure").closest("label")).toHaveClass("delegated");
  });

  it("renders a message with an image and a pdf attachment", async () => {
    const att = [
      { id: "a1", name: "shot.png", kind: "image" as const, size: 1200, read_by_agents: false },
      { id: "a2", name: "spec.pdf", kind: "pdf" as const, size: 3400, read_by_agents: false },
    ];
    apiMock.intake.mockResolvedValue(
      session({
        attachments: att,
        messages: [
          { role: "agent", text: "Hi — what do you want to build?", at: "2026-11-01T00:00:00Z" },
          { role: "agent", text: "See the screenshot", at: "2026-11-01T00:01:00Z", attachment: "a1" },
          { role: "agent", text: "And the spec", at: "2026-11-01T00:02:00Z", attachment: "a2" },
        ],
      }),
    );
    renderIntake();
    expect(await screen.findByText("See the screenshot")).toBeInTheDocument();
    // Once in the message, once in the attachment list.
    expect(screen.getAllByText("IMG")).toHaveLength(2);
    expect(screen.getAllByText("PDF")).toHaveLength(2);
    expect(screen.getByText("And the spec")).toBeInTheDocument();
  });

  it("attaches a file from the picker and removes it", async () => {
    const user = userEvent.setup();
    const file = new File(["fake image"], "shot.png", { type: "image/png" });
    const attached = session({
      attachments: [{ id: "a1", name: "shot.png", kind: "image", size: 10, read_by_agents: false }],
    });
    apiMock.intake.mockResolvedValue(session());
    apiMock.intakeAttach.mockResolvedValue(attached);
    apiMock.intakeDetach.mockResolvedValue(session());
    renderIntake();
    await screen.findByText("Hi — what do you want to build?");
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [file] } });
    expect(apiMock.intakeAttach).toHaveBeenCalledWith("sess-1", file);
    expect(await screen.findByText("shot.png")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Remove shot.png" }));
    expect(apiMock.intakeDetach).toHaveBeenCalledWith("sess-1", "a1");
  });

  it("sends a quick reply when the agent offers one", async () => {
    const user = userEvent.setup();
    apiMock.intake.mockResolvedValue(
      session({
        messages: [
          { role: "agent", text: "Hi — what do you want to build?", at: "2026-11-01T00:00:00Z" },
          {
            role: "agent",
            text: "Which product?",
            at: "2026-11-01T00:01:00Z",
            options: ["Clinic visits", "Payments"],
          },
        ],
      }),
    );
    apiMock.intakeSay.mockResolvedValue(session());
    renderIntake();
    await user.click(await screen.findByRole("button", { name: "Clinic visits" }));
    expect(apiMock.intakeSay).toHaveBeenCalledWith("sess-1", "Clinic visits");
  });

  it("opens the file picker from the clip button", async () => {
    apiMock.intake.mockResolvedValue(session());
    renderIntake();
    await screen.findByLabelText("Conversation with the intake agent");
    const picker = document.querySelector('input[type="file"]') as HTMLInputElement;
    expect(picker).not.toBeNull();
    const click = vi.spyOn(picker, "click");
    await userEvent.setup().click(screen.getByRole("button", { name: "Attach files" }));
    expect(click).toHaveBeenCalled();
  });

  it("shows the drop hint and attaches a dropped file", async () => {
    const file = new File(["fake"], "notes.txt", { type: "text/plain" });
    apiMock.intake.mockResolvedValue(session());
    apiMock.intakeAttach.mockResolvedValue(session());
    renderIntake();
    const chat = await screen.findByLabelText("Conversation with the intake agent");
    fireEvent.dragOver(chat, { dataTransfer: { types: ["Files"] } });
    expect(screen.getByText("Drop files to attach them to the intent")).toBeInTheDocument();
    fireEvent.dragLeave(chat);
    expect(screen.queryByText("Drop files to attach them to the intent")).not.toBeInTheDocument();
    fireEvent.dragOver(chat, { dataTransfer: { types: ["Files"] } });
    expect(screen.getByText("Drop files to attach them to the intent")).toBeInTheDocument();
    fireEvent.drop(chat, { dataTransfer: { files: [file] } });
    expect(apiMock.intakeAttach).toHaveBeenCalledWith("sess-1", file);
    expect(screen.queryByText("Drop files to attach them to the intent")).not.toBeInTheDocument();
  });
});
