import { expect, test, type Page } from "@playwright/test";
import { ME, StubApi } from "./helpers";

/**
 * Smoke: the shortest path through the app, run after every deploy with
 * `--grep @smoke`. This is the ONLY @smoke test in the suite.
 *
 * Known limitation (see helpers.ts): there is no test identity in this
 * repo. The "signed-in session" below is a stubbed GET /api/me — no real
 * GitHub OAuth, passkey, or credentials are involved, and the dev-mode
 * sign-in form is exercised against the stubbed API only. Real sign-in
 * cannot be covered until a test identity exists.
 */

async function signedInSession(page: Page) {
  const api = new StubApi();
  api.inboxItems = [];
  api.featureSummaries = [];
  api.connections = [];
  await api.install(page);
  return api;
}

test("sign-in lands on Today @smoke", async ({ page }) => {
  await signedInSession(page);
  await page.goto("/");
  await expect(page).toHaveURL(/\/today$/);
  await expect(page.getByRole("heading", { name: /Here.s what needs you/ })).toBeVisible();
  await expect(page.getByText("Nothing is waiting on you right now.")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Decisions waiting" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Your projects" })).toBeVisible();
  await expect(
    page.getByRole("navigation", { name: "Main" }).first().getByRole("link", { name: "Approvals" }),
  ).toBeVisible();
});

test("signed-out user sees the sign-in screen, dev sign-in returns them to Today", async ({ page }) => {
  const api = new StubApi();
  api.signedIn = false;
  api.authConfig = { mode: "dev", teams: ["acme-water/approvers"] };
  await api.install(page);

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Welcome back to Valxel" })).toBeVisible();
  await expect(page.getByText("Development sign-in: choose any identity. Not for production.")).toBeVisible();

  await page.getByLabel("GitHub login").fill(ME.login);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();

  await expect(page).toHaveURL(/\/today$/);
  await expect(page.getByRole("heading", { name: /Here.s what needs you/ })).toBeVisible();
});
