import { expect, test, type Page } from "@playwright/test";
import { featureSummary, pendingItem, rail, StubApi } from "./helpers";

/**
 * Approvals: a decision is approved end to end, and a decision is sent
 * back with a reason. The stubbed API flips state when the decide POST
 * lands, so the suite proves the UI follows the server: the waiting count
 * drops, the rail badge drops, and the feature's stepper moves.
 */

test.beforeEach(async ({ page }) => {
  const api = new StubApi();
  api.inboxItems = [
    pendingItem(),
    pendingItem({
      interrupt_id: "int-2",
      gate: "g1",
      feature_id: "VX-FEAT-102",
      title: "Improve onboarding for new clinics",
    }),
  ];
  api.featureSummaries = [
    featureSummary(),
    featureSummary({ feature_id: "VX-FEAT-102", title: "Improve onboarding for new clinics" }),
  ];
  api.connections = [];
  // Approving the G2 gate moves the feature on: the gate is done and the
  // design phase is current, which the Today stepper renders.
  api.onDecide = (post) => {
    if (post.body.decision !== "approve") return;
    api.inboxItems = api.inboxItems.filter((i) => i.interrupt_id !== post.interruptId);
    api.featureSummaries = api.featureSummaries.map((f) =>
      f.feature_id === post.featureId ? { ...f, rail: rail({ g2: "done", design: "current" }) } : f,
    );
  };
  await api.install(page);
  // Hand the stub to the test through the page (each test has its own page,
  // so this stays safe with fullyParallel on).
  (page as unknown as { __api: StubApi }).__api = api;
});

function apiOf(page: Page) {
  return (page as unknown as { __api: StubApi }).__api;
}

test("approve a decision: the project advances and the waiting count drops", async ({ page }) => {
  const api = apiOf(page);
  await page.goto("/inbox");

  await expect(page.getByRole("heading", { name: "Decisions" })).toBeVisible();
  await expect(page.getByText("2 waiting", { exact: true })).toBeVisible();
  // The first decision is selected on open: G2 asks "Is the scope right?".
  await expect(page.getByRole("heading", { name: "Is the scope right?" })).toBeVisible();
  await expect(page.getByText("In two minutes")).toBeVisible();

  const decided = page.waitForResponse(
    (r) => r.url().includes("/decisions/int-1") && r.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Approve the scope" }).click();
  await decided;

  // The decide POST carries the approval…
  expect(api.decidePosts).toHaveLength(1);
  expect(api.decidePosts[0]).toMatchObject({
    featureId: "VX-FEAT-101",
    interruptId: "int-1",
    body: { decision: "approve" },
  });
  // …and the inbox follows it: one left, the approved one gone.
  await expect(page.getByText("1 waiting", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("listitem").filter({ hasText: "Add a CSV export to the visits list" }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("listitem").filter({ hasText: "Improve onboarding for new clinics" }),
  ).toBeVisible();

  // The rail badge refreshes on load, so reload and check it too.
  await page.reload();
  await expect(page.locator("aside nav").getByLabel("1 waiting on you")).toBeVisible();

  // The feature itself advanced: on Today its Plan stage is done and
  // Design is the current stage.
  await page.goto("/today");
  const stepper = page.getByRole("list", { name: "Where this is, from idea to live" }).first();
  await expect(stepper.locator("li", { hasText: "Plan" })).toHaveClass(/done/);
  await expect(stepper.locator("li", { hasText: "Design" })).toHaveClass(/current/);
  await expect(stepper.locator("li", { hasText: "Design" })).toHaveAttribute("aria-current", "step");
});

test("send a decision back: request changes with a reason", async ({ page }) => {
  const api = apiOf(page);
  await page.goto("/inbox");
  await expect(page.getByText("2 waiting", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "Request changes" }).click();
  await page.getByLabel("What should the agents change?").fill("The scope misses offline CSV import.");
  await page.getByLabel("Main reason").selectOption("missing_requirement");
  const decided = page.waitForResponse(
    (r) => r.url().includes("/decisions/int-1") && r.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Send back with changes" }).click();
  await decided;

  expect(api.decidePosts).toHaveLength(1);
  expect(api.decidePosts[0]).toMatchObject({
    featureId: "VX-FEAT-101",
    interruptId: "int-1",
    body: {
      decision: "revise",
      feedback: "The scope misses offline CSV import.",
      reason: "missing_requirement",
    },
  });
  // Sent back, not approved: it is still waiting.
  await expect(page.getByText("2 waiting", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("listitem").filter({ hasText: "Add a CSV export to the visits list" }),
  ).toBeVisible();
});
