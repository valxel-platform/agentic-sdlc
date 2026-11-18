import { expect, test, type Page } from "@playwright/test";
import { featureSummary, StubApi } from "./helpers";

/**
 * Navigation: the desktop rail, the ⌘K palette, and the phone journey
 * (bottom tab bar + More screen). Each hop asserts the URL and the page
 * heading, so a renamed route or a dead link fails here, not in a demo.
 */

test.beforeEach(async ({ page }) => {
  const api = new StubApi();
  api.inboxItems = [];
  api.featureSummaries = [featureSummary()];
  api.connections = [];
  await api.install(page);
});

function rail(page: Page) {
  return page.getByRole("navigation", { name: "Main" }).first();
}

function tabBar(page: Page) {
  return page.getByRole("navigation", { name: "Main" }).last();
}

test("rail navigation reaches every main section", async ({ page }) => {
  await page.goto("/today");
  await expect(page.getByRole("heading", { name: /Here.s what needs you/ })).toBeVisible();

  await rail(page).getByRole("link", { name: "Products" }).click();
  await expect(page).toHaveURL(/\/products$/);
  await expect(page.getByRole("heading", { name: "Products", exact: true })).toBeVisible();

  await rail(page).getByRole("link", { name: "Approvals" }).click();
  await expect(page).toHaveURL(/\/inbox$/);
  await expect(page.getByRole("heading", { name: "Decisions", exact: true })).toBeVisible();

  await rail(page).getByRole("link", { name: "Connected apps" }).click();
  await expect(page).toHaveURL(/\/connected-apps$/);
  await expect(page.getByRole("heading", { name: "Connected apps", exact: true })).toBeVisible();

  await rail(page).getByRole("link", { name: "Today" }).click();
  await expect(page).toHaveURL(/\/today$/);
  await expect(page.getByRole("heading", { name: /Here.s what needs you/ })).toBeVisible();
});

test("⌘K palette jumps to a section by typing, and by keyboard", async ({ page }) => {
  await page.goto("/today");
  await expect(page.getByRole("heading", { name: /Here.s what needs you/ })).toBeVisible();

  // The search button opens the same palette the ⌘K shortcut opens.
  await page.getByRole("button", { name: "Search (Command K)" }).click();
  const palette = page.getByRole("dialog", { name: "Search and go to" });
  await expect(palette).toBeVisible();
  await palette.getByLabel("Search projects, approvals, apps").fill("Roadmap");
  await palette.getByRole("option", { name: "Roadmap" }).click();
  await expect(page).toHaveURL(/\/roadmap$/);
  await expect(page.getByRole("heading", { name: "Roadmap", exact: true })).toBeVisible();

  // The page is settled by now, so the ⌘K shortcut opens it.
  await page.keyboard.press("Control+k");
  await expect(palette).toBeVisible();
  await palette.getByLabel("Search projects, approvals, apps").fill("Costs");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/costs$/);
  await expect(page.getByRole("heading", { name: "Costs", exact: true })).toBeVisible();
});

test("mobile: bottom tab bar and the More screen reach the rest", async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const page = await context.newPage();
  const api = new StubApi();
  api.inboxItems = [];
  api.featureSummaries = [featureSummary()];
  api.connections = [];
  await api.install(page);

  await page.goto("/today");
  await expect(tabBar(page).getByRole("link", { name: "More" })).toBeVisible();

  await tabBar(page).getByRole("link", { name: "More" }).click();
  await expect(page).toHaveURL(/\/more$/);
  await expect(page.getByRole("heading", { name: "More", exact: true })).toBeVisible();

  const more = page.getByRole("navigation", { name: "More sections" });
  await more.getByRole("link", { name: "Costs" }).click();
  await expect(page).toHaveURL(/\/costs$/);
  await expect(page.getByRole("heading", { name: "Costs", exact: true })).toBeVisible();

  await page.goBack();
  await expect(page).toHaveURL(/\/more$/);
  await more.getByRole("link", { name: "Connected apps" }).click();
  await expect(page).toHaveURL(/\/connected-apps$/);
  await expect(page.getByRole("heading", { name: "Connected apps", exact: true })).toBeVisible();

  await tabBar(page).getByRole("link", { name: "Today" }).click();
  await expect(page).toHaveURL(/\/today$/);

  await context.close();
});
