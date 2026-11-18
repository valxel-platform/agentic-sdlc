import { expect, test, type Dialog, type Page } from "@playwright/test";
import { StubApi, toolRow } from "./helpers";

/**
 * Connected apps: connect GitHub through all three modal steps
 * (permissions → scope → sign-in/authorize), then disconnect it.
 *
 * The last step is a full-page redirect to GitHub's own sign-in; no
 * E2E run can complete a real vendor OAuth flow, so the stub plays the
 * vendor's part: the install URL flips the app to connected and sends
 * the browser back to /connected-apps, exactly as the OAuth callback
 * would. The AWS card keeps its own two-step flow (stack, then account
 * check) and is not covered here.
 */

function githubCard(page: Page) {
  return page.locator("section", { has: page.getByRole("heading", { name: "GitHub" }) });
}

test.beforeEach(async ({ page }) => {
  const api = new StubApi();
  api.connections = [
    toolRow(),
    toolRow({ tool: "jira", name: "Jira", grants: "Read and write issues in the projects you choose." }),
    toolRow({
      tool: "aws",
      name: "AWS",
      grants: "Create the stacks products run on, in the accounts you choose.",
    }),
    toolRow({
      tool: "sentry",
      name: "Sentry",
      grants: "Read error reports for the products you choose.",
      available: false,
    }),
  ];
  api.inboxItems = [];
  api.featureSummaries = [];
  await api.install(page);
  (page as unknown as { __api: StubApi }).__api = api;
});

function apiOf(page: Page) {
  return (page as unknown as { __api: StubApi }).__api;
}

test("connect GitHub: permissions, scope, authorize — card flips to Connected", async ({ page }) => {
  await page.goto("/connected-apps");
  await expect(page.getByRole("heading", { name: "Connected apps", exact: true })).toBeVisible();
  await expect(page.locator("main").getByText("0 of 4 apps connected", { exact: true })).toBeVisible();
  await expect(githubCard(page).getByText("Not connected", { exact: true })).toBeVisible();
  // Only the tools the backend actually supports are listed: no Slack,
  // Figma or Vercel cards exist.
  await expect(page.getByRole("heading", { name: "Slack" })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Figma" })).toHaveCount(0);
  // Sentry has no OAuth client on this Valxel, so it says so instead of
  // offering Connect.
  await expect(page.getByText(/isn.t set up on this Valxel yet/)).toBeVisible();

  await githubCard(page).getByRole("button", { name: "Connect" }).click();
  const modal = page.getByRole("dialog", { name: "Connect GitHub" });
  await expect(modal).toBeVisible();

  // Step 1: what it can do, and what it can never do.
  await expect(modal.getByText("What it can never do")).toBeVisible();
  await expect(modal.getByText("See or store your passwords")).toBeVisible();
  await modal.getByRole("button", { name: "Continue" }).click();

  // Step 2: pick the scope.
  await expect(modal.getByText("Choose the scope", { exact: true })).toBeVisible();
  await modal.getByRole("radio", { name: /Only what you choose/ }).check();
  await expect(
    modal.getByText(/You confirm only what you choose on GitHub.s own sign-in page/),
  ).toBeVisible();
  await modal.getByRole("button", { name: "Continue" }).click();

  // Step 3: authorize on GitHub's own sign-in. The stub plays the
  // vendor: it connects the app and redirects back here.
  await expect(modal.getByText(/Approve on GitHub.s own sign-in/)).toBeVisible();
  await modal.getByRole("button", { name: "Continue to GitHub" }).click();

  await expect(page.locator("main").getByText("1 of 4 apps connected", { exact: true })).toBeVisible();
  await expect(githubCard(page).getByText("Connected", { exact: true })).toBeVisible();
  await expect(githubCard(page).getByText("acme-water")).toBeVisible();
  await expect(githubCard(page).getByRole("button", { name: "Manage" })).toBeVisible();
  await expect(page.getByRole("dialog", { name: "Connect GitHub" })).toHaveCount(0);
});

test("disconnect GitHub: confirm, and the card returns to disconnected", async ({ page }) => {
  const api = apiOf(page);
  api.connections[0].status = "connected";
  api.connections[0].connections = [
    {
      tool: "github",
      account: "acme-water",
      status: "connected",
      connected_by: "ada",
      connected_at: 1763400000,
      last_ok: 1763400000,
      last_error: "",
      reach: { selection: "all" },
    },
  ];
  await page.goto("/connected-apps");
  await expect(page.locator("main").getByText("1 of 4 apps connected", { exact: true })).toBeVisible();
  await expect(githubCard(page).getByRole("button", { name: "Manage" })).toBeVisible();

  // window.confirm blocks the renderer, so the dialog must be accepted
  // from a listener — awaiting the click first would deadlock.
  let confirm: Dialog | null = null;
  page.on("dialog", (d) => {
    confirm = d;
    void d.accept();
  });
  await githubCard(page).getByRole("button", { name: "Disconnect" }).click();
  expect(confirm?.message()).toContain("Disconnect GitHub for acme-water?");

  await expect(
    page.getByText(/Disconnected\. To remove Valxel's access on GitHub's side too:/),
  ).toBeVisible();
  expect(api.disconnectCalls).toEqual([{ tool: "github", account: "acme-water" }]);
  await expect(page.locator("main").getByText("0 of 4 apps connected", { exact: true })).toBeVisible();
  await expect(githubCard(page).getByText("Not connected", { exact: true })).toBeVisible();
  await expect(githubCard(page).getByRole("button", { name: "Connect" })).toBeVisible();
});
