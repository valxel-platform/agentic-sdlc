import { expect, test, type Page } from "@playwright/test";
import { inceptionView, StubApi } from "./helpers";

/**
 * Product inception: start a new product from the Products page, and start
 * its first feature once setup is done.
 *
 * The real form is a single page, not a multi-step wizard — the "steps"
 * are the wizard strip at the top (Start → Product inception → First
 * feature → Review) and the fields below it: name and GitHub org, the
 * auto-proposed product id, the idea, goals/users/constraints, budget and
 * research depth. There is no workspace picker or repo-prefix field in
 * this form: the workspace is the signed-in user's, and the copy on the
 * form states it plainly — no repos are created during inception; design
 * proposes them, the architecture gate approves, and provisioning creates
 * them.
 */

test.beforeEach(async ({ page }) => {
  const api = new StubApi();
  api.inceptions = [];
  api.connections = [];
  api.featureDetails["VX-PRD-301"] = inceptionView();
  await api.install(page);
  (page as unknown as { __api: StubApi }).__api = api;
});

function apiOf(page: Page) {
  return (page as unknown as { __api: StubApi }).__api;
}

test("start a new product: brief, then the product appears in the list", async ({ page }) => {
  const api = apiOf(page);
  await page.goto("/products");
  await expect(page.getByRole("heading", { name: "Products", exact: true })).toBeVisible();
  await expect(page.getByText("No products started here yet")).toBeVisible();

  await page.getByRole("button", { name: "Start a product" }).click();
  await expect(page.getByRole("heading", { name: "Start a product" })).toBeVisible();
  const steps = page.getByRole("list", { name: "Product inception steps" });
  await expect(steps.locator("li", { hasText: "Product inception" })).toHaveClass(/current/);
  await expect(page.getByText("No repos are created during inception.")).toBeVisible();

  // The name proposes a product id; the org must already exist.
  await page.getByLabel("Name", { exact: true }).fill("Clean Water Monitor");
  await expect(page.getByLabel(/Product ID/)).toHaveValue("CLEANW");
  await page.getByLabel(/GitHub org/).fill("acme-water");
  await page
    .getByLabel(/The idea/)
    .fill("Farmers lose crops to dry fields they only discover on a walk-round.");
  await page.getByLabel(/Goals, one per line/).fill("Fewer dry-field incidents (measure: under 2 a month)");
  await page.getByLabel(/Users, one per line/).fill("Farm managers: out in the fields on a phone");
  await page.getByLabel(/Budget for the first year/).fill("50000");
  await page.getByRole("radio", { name: /Thorough: more alternatives/ }).check();

  const created = page.waitForURL("**/products/VX-PRD-301");
  await page.getByRole("button", { name: "Write the brief" }).click();
  await created;

  expect(api.inceptionPosts).toHaveLength(1);
  expect(api.inceptionPosts[0]).toMatchObject({
    name: "Clean Water Monitor",
    org: "acme-water",
    product_id: "CLEANW",
    research: "thorough",
    budget_usd: 50000,
  });

  // The inception opens: researching, brief still being written.
  await expect(page.getByRole("heading", { name: "Clean Water Monitor" })).toBeVisible();
  await expect(page.getByText("Researching the problem and the market")).toBeVisible();
  await expect(page.getByText("Agents are writing the brief.")).toBeVisible();

  // And the product is now in the list, marked new (in its org, not
  // describing an existing product) with no repos yet.
  await page.goto("/products");
  const row = page.getByRole("link", { name: /Clean Water Monitor/ });
  await expect(row).toBeVisible();
  await expect(row).toContainText("in acme-water");
  await expect(row).toContainText("Researching the problem and the market");
  await expect(page.getByText("No products started here yet")).toHaveCount(0);
});

test("once setup is done, the first feature starts from the inception", async ({ page }) => {
  const api = apiOf(page);
  api.featureDetails["VX-PRD-301"] = inceptionView({
    status: "set_up",
    setup: {
      product_id: "CLEANW",
      steps: [
        { step: "governance_repo", ok: true, repo: "acme-water/cleanw-governance" },
        { step: "governance_pr", ok: true, url: "https://example.invalid/pr/1" },
      ],
    },
  });
  await page.goto("/products/VX-PRD-301");

  // Setup is the open tab for a set-up product.
  await expect(page.getByRole("tab", { name: "Setup" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByText("Governance repo")).toBeVisible();

  const started = page.waitForURL("**/features/VX-FEAT-401");
  await page.getByRole("button", { name: "Start the first feature" }).click();
  await started;

  expect(api.firstFeaturePosts).toEqual(["VX-PRD-301"]);
});
