import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

test("workspace meets automated WCAG accessibility checks", async ({
  page,
}) => {
  await page.goto("/");
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
    .analyze();
  expect(
    results.violations.map((v) => ({
      id: v.id,
      nodes: v.nodes.map((n) => ({
        target: n.target,
        summary: n.failureSummary,
      })),
    })),
  ).toEqual([]);
});

test("unsafe migration is analyzed, blocked, and persisted through the real API", async ({
  page,
}) => {
  const title = `Browser destructive migration ${Date.now()}`;
  await page.goto("/");
  await page.getByLabel("Migration name").fill(title);
  await page
    .getByLabel("PostgreSQL migration SQL")
    .fill("ALTER TABLE accounts DROP COLUMN balance;");
  await page.getByRole("button", { name: "Analyze migration" }).click();
  await expect(
    page.getByText("Migration parsed and evaluated by deterministic policies."),
  ).toBeVisible();
  await expect(
    page.getByText("Execution blocked", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Deterministic findings" }),
  ).toBeVisible();
  const analyzedAccessibility = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
    .analyze();
  expect(
    analyzedAccessibility.violations.map((v) => ({
      id: v.id,
      nodes: v.nodes.map((n) => ({
        target: n.target,
        summary: n.failureSummary,
      })),
    })),
  ).toEqual([]);
  await page.evaluate(() => {
    (document.activeElement as HTMLElement)?.blur();
    window.scrollTo(0, 0);
  });
  await page.screenshot({ path: "evidence/dashboard.png", fullPage: true });
  await page.getByRole("button", { name: "Request recommendation" }).click();
  await expect(
    page.getByText(
      "AI is optional and disabled; deterministic analysis and planning remain available",
    ),
  ).toBeVisible();
  await page.getByRole("tab", { name: "Validation & execution" }).click();
  await expect(
    page.getByRole("button", { name: "Approve verified plan" }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Execute in disposable database" }),
  ).toBeDisabled();
  await page
    .getByRole("button", { name: "Migration history", exact: true })
    .click();
  await expect(page.getByRole("cell", { name: title })).toBeVisible();
  await page.getByRole("button", { name: "Audit trail", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "migration · analyzed" }).first(),
  ).toBeVisible();
});

test("eligible index operation produces a real plan and reports runtime capability", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByLabel("Migration name").fill("Browser index migration");
  await page
    .getByLabel("PostgreSQL migration SQL")
    .fill("CREATE INDEX accounts_balance_idx ON accounts (balance);");
  await page.getByRole("button", { name: "Analyze migration" }).click();
  await expect(
    page.getByText("Migration parsed and evaluated by deterministic policies."),
  ).toBeVisible();
  await page.getByRole("tab", { name: "Staged plan" }).click();
  await page.getByRole("button", { name: "Generate safer plan" }).click();
  await expect(
    page.getByText(
      "Plan generated. Review all preconditions and application handoffs.",
    ),
  ).toBeVisible();
  await expect(
    page.locator(".sql-block").filter({ hasText: "CONCURRENTLY" }),
  ).toBeVisible();
  await page.getByRole("tab", { name: "Validation & execution" }).click();
  if (process.env.SCHEMASHIELD_E2E_RUNTIME === "1")
    await expect(
      page.getByRole("button", { name: "Validate plan" }),
    ).toBeEnabled();
  else
    await expect(
      page.getByRole("button", { name: "Validate plan" }),
    ).toBeDisabled();
  await expect(page.getByText("No runtime report collected.")).toBeVisible();
  await page.getByRole("tab", { name: "Lock & performance" }).click();
  await expect(
    page.getByRole("heading", { name: "No measurements available" }),
  ).toBeVisible();
});

test("authentication errors are actionable and secrets are not persisted", async ({
  page,
}) => {
  await page.goto("/");
  await page.locator(".connection-settings summary").click();
  await page.getByLabel("Local API bearer token").fill("invalid-token");
  await page.getByRole("button", { name: "Refresh workspace" }).click();
  await expect(
    page
      .getByRole("alert")
      .filter({ hasText: "A valid local API token is required" }),
  ).toBeVisible();
  expect(await page.evaluate(() => Object.keys(localStorage).length)).toBe(0);
  expect(await page.evaluate(() => Object.keys(sessionStorage).length)).toBe(0);
});

test("edited SQL cannot reuse the saved artifact for planning", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByLabel("Migration name").fill("Browser artifact freshness");
  await page
    .getByLabel("PostgreSQL migration SQL")
    .fill("CREATE INDEX accounts_balance_idx ON accounts (balance);");
  await page.getByRole("button", { name: "Analyze migration" }).click();
  await expect(
    page.getByText("Migration parsed and evaluated by deterministic policies."),
  ).toBeVisible();
  await page.getByRole("tab", { name: "Staged plan" }).click();
  await expect(
    page.getByRole("button", { name: "Generate safer plan" }),
  ).toBeEnabled();
  await page
    .getByLabel("PostgreSQL migration SQL")
    .fill("ALTER TABLE accounts DROP COLUMN balance;");
  await expect(
    page.getByText("The editor changed.", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Generate safer plan" }),
  ).toBeDisabled();
  await page.getByRole("tab", { name: "Validation & execution" }).click();
  await expect(
    page.getByRole("button", { name: "Validate plan" }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Execute in disposable database" }),
  ).toBeDisabled();
});

test("mobile layout exposes usable navigation and SQL editor", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await expect(page.getByLabel("PostgreSQL migration SQL")).toBeVisible();
  await page.getByRole("button", { name: "Toggle navigation" }).click();
  await page
    .getByRole("button", { name: "Backfill & jobs", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Backfill progress & execution jobs" }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
});
