import { expect, test, type Page } from "@playwright/test";
import { StubApi } from "./helpers";

/**
 * Intake: an existing product's feature, the two-step path — pick the
 * entry ("Existing product — add a feature"), then complete the intent
 * and submit it. The stubbed intake agent replies in the chat, but the
 * intent panel is what makes the request submittable; the test fills it
 * the way a person would after the conversation.
 */

test.beforeEach(async ({ page }) => {
  const api = new StubApi();
  api.inboxItems = [];
  api.featureSummaries = [];
  api.connections = [];
  await api.install(page);
  (page as unknown as { __api: StubApi }).__api = api;
});

function apiOf(page: Page) {
  return (page as unknown as { __api: StubApi }).__api;
}

test("intake a feature for an existing product", async ({ page }) => {
  const api = apiOf(page);
  const intent = page.getByRole("region", { name: "The intent" });
  await page.goto("/intake");

  // Starting an intake redirects to its session id.
  await expect(page).toHaveURL(/\/intake\/intake-1$/);
  await expect(page.getByRole("heading", { name: "What do you want to do?" })).toBeVisible();

  // Step 1: the existing-product entry.
  await page.getByRole("button", { name: /Existing product — add a feature/ }).click();
  await expect(page.getByRole("heading", { name: "The intent" })).toBeVisible();
  await expect(page.locator("p.readiness")).toHaveText("0 of 4 in place");

  // The conversation: describe it, the agent replies.
  await page.getByLabel("Message to the intake agent").fill("Add a CSV export to the visits list");
  await page.keyboard.press("Enter");
  await expect(page.getByText("Got it — anything else to add before you review the intent?")).toBeVisible();

  // Step 2: complete the intent panel. Each field saves on blur, so type
  // like a person (click, type, Tab): fill() + blur races React's state
  // here and the save reads the previous value.
  await intent.getByLabel("Product").selectOption("CLN");
  await intent.getByLabel("Problem", { exact: true }).click();
  await page.keyboard.type("Visits can't leave the app as a file today.");
  await page.keyboard.press("Tab");
  await intent.getByLabel("Users", { exact: true }).click();
  await page.keyboard.type("Clinic admins, at a desk, at month end");
  await page.keyboard.press("Tab");
  await intent.getByLabel("Success measure", { exact: true }).click();
  await page.keyboard.type("Exports downloaded 20 times a month");
  await page.keyboard.press("Tab");
  await expect(intent.locator("p.readiness")).toHaveText("Ready to submit");

  await intent.getByRole("button", { name: "Submit intent" }).click();

  await expect(intent.locator("p.readiness")).toHaveText("Submitted");
  expect(api.intakeSubmitPosts).toEqual([{ id: "intake-1", feature_id: "VX-FEAT-201" }]);
  const started = page.getByRole("link", { name: "VX-FEAT-201" });
  await expect(started).toBeVisible();
  await expect(started).toHaveAttribute("href", "/features/VX-FEAT-201");
  await expect(page.getByText("Agents have started discovery.")).toBeVisible();
});

test("the submit stays disabled until the intent is complete", async ({ page }) => {
  const intent = page.getByRole("region", { name: "The intent" });
  await page.goto("/intake");
  await expect(page).toHaveURL(/\/intake\/intake-1$/);

  await page.getByRole("button", { name: /Existing product — add a feature/ }).click();
  await expect(intent.getByRole("button", { name: "Submit intent" })).toBeDisabled();
  await expect(intent.getByText(/Still needed: Product, Problem, Users, Success measure/)).toBeVisible();

  await intent.getByLabel("Product").selectOption("CLN");
  await expect(intent.getByText(/Still needed: Problem, Users, Success measure/)).toBeVisible();
  await expect(intent.getByRole("button", { name: "Submit intent" })).toBeDisabled();
});
