import { test, expect, type APIRequestContext } from "@playwright/test";

const enabled = process.env.SCHEMASHIELD_E2E_RUNTIME === "1";
const api = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const token =
  process.env.SCHEMASHIELD_E2E_TOKEN ?? "local-development-token-change-me";
type RuntimeJob = {
  id: string;
  status: string;
  kind: string;
  report?: {
    checks: { name: string; passed: boolean }[];
    metrics: Record<string, unknown>;
    progress?: { status: string };
    resume_evidence?: {
      interrupted: { status: string };
      completed: { status: string };
      original_backend_pid: number;
      resumed_backend_pid: number;
    };
  };
};

async function completedJob(
  request: APIRequestContext,
  id: string,
): Promise<RuntimeJob> {
  let terminal: RuntimeJob | undefined;
  await expect
    .poll(
      async () => {
        const response = await request.get(`${api}/api/jobs/${id}`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        expect(response.ok()).toBe(true);
        const job = (await response.json()) as RuntimeJob;
        if (["passed", "failed", "cancelled"].includes(job.status))
          terminal = job;
        return terminal?.status ?? "running";
      },
      {
        timeout: 120_000,
        intervals: [1000, 1500, 2000],
        message:
          "Disposable PostgreSQL job must finish with actual passing checks",
      },
    )
    .toBe("passed");
  if (!terminal?.report) throw new Error("Missing actual runtime report");
  expect(terminal.report.checks.length).toBeGreaterThan(0);
  expect(terminal.report.checks.every((check) => check.passed)).toBe(true);
  return terminal;
}

test.describe("real disposable PostgreSQL browser workflow", () => {
  test.skip(
    !enabled,
    "Set SCHEMASHIELD_E2E_RUNTIME=1 with externally started PostgreSQL backend and dashboard.",
  );
  test("unsafe index → concurrent plan → validate → human approval → execute → interrupted backfill recovery", async ({
    page,
    request,
  }) => {
    test.setTimeout(360_000);
    const capabilities = await request.get(`${api}/api/capabilities`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    expect(capabilities.ok()).toBe(true);
    const caps = await capabilities.json();
    expect(caps.sandbox_available).toBe(true);
    expect(caps.sandbox_enabled).toBe(true);
    expect(caps.persistence).toContain("postgres");
    await page.goto("/");
    await page.locator(".connection-settings summary").click();
    await page.getByLabel("Local API bearer token").fill(token);
    await page
      .getByLabel("Migration name")
      .fill(`Runtime index workflow ${Date.now()}`);
    await page
      .getByLabel("PostgreSQL migration SQL")
      .fill("CREATE INDEX accounts_email_runtime_idx ON accounts (email);");
    await page.getByRole("button", { name: "Analyze migration" }).click();
    await expect(
      page.getByText("Execution blocked", { exact: true }),
    ).toBeVisible();
    await page.getByRole("tab", { name: "Staged plan" }).click();
    await page.getByRole("button", { name: "Generate safer plan" }).click();
    await expect(
      page.locator(".sql-block").filter({ hasText: "CONCURRENTLY" }),
    ).toBeVisible();
    await page.getByRole("tab", { name: "Validation & execution" }).click();
    const validationResponse = page.waitForResponse(
      (response) =>
        response.url().endsWith("/validate") &&
        response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Validate plan" }).click();
    const validation = await validationResponse;
    expect(validation.ok()).toBe(true);
    const validationJob = await completedJob(
      request,
      (await validation.json()).id,
    );
    await expect(page.locator(".report-heading .badge")).toHaveText("passed", {
      timeout: 20_000,
    });
    await page.getByRole("tab", { name: "Lock & performance" }).click();
    await expect(page.locator(".tabs-content .json")).toBeVisible();
    expect(Object.keys(validationJob.report!.metrics).length).toBeGreaterThan(
      0,
    );
    await page.getByRole("tab", { name: "Validation & execution" }).click();
    await page.getByLabel("Reviewer name").fill("Runtime browser reviewer");
    await page.getByRole("button", { name: "Approve verified plan" }).click();
    await expect(
      page.getByText("Human approval recorded for the verified artifact."),
    ).toBeVisible();
    const executionResponse = page.waitForResponse(
      (response) =>
        response.url().endsWith("/execute") &&
        response.request().method() === "POST",
    );
    await page
      .getByRole("button", { name: "Execute in disposable database" })
      .click();
    const execution = await executionResponse;
    expect(execution.ok()).toBe(true);
    const executed = await completedJob(request, (await execution.json()).id);
    expect(executed.kind).toBe("execution");
    await expect(page.locator(".report-heading .badge")).toHaveText("passed", {
      timeout: 20_000,
    });
    await expect(
      page.getByRole("button", { name: "Execute in disposable database" }),
    ).toBeDisabled();
    await page
      .getByRole("button", { name: "Backfill & jobs", exact: true })
      .click();
    const backfillResponse = page.waitForResponse(
      (response) =>
        response.url().endsWith("/backfill-demo") &&
        response.request().method() === "POST",
    );
    await page
      .getByRole("button", { name: "Run backfill recovery demo" })
      .click();
    const backfill = await backfillResponse;
    expect(backfill.ok()).toBe(true);
    const backfilled = await completedJob(request, (await backfill.json()).id);
    expect(backfilled.kind).toBe("backfill");
    expect(backfilled.report!.progress?.status).toBe("completed");
    const recovery = backfilled.report!.resume_evidence;
    expect(recovery?.interrupted.status).toBe("paused");
    expect(recovery?.completed.status).toBe("completed");
    expect(recovery?.original_backend_pid).not.toBe(
      recovery?.resumed_backend_pid,
    );
    await expect(
      page
        .locator(".job")
        .filter({ hasText: backfilled.id })
        .locator(".json")
        .first(),
    ).toContainText('"completed"', { timeout: 20_000 });
    await page.evaluate(() => {
      (document.activeElement as HTMLElement)?.blur();
      window.scrollTo(0, 0);
    });
    await page.screenshot({
      path: "evidence/runtime-backfill.png",
      fullPage: true,
    });
    await page
      .getByRole("button", { name: "Migration workspace", exact: true })
      .click();
    await page.getByRole("tab", { name: "Validation & execution" }).click();
    await page.screenshot({
      path: "evidence/runtime-validation.png",
      fullPage: true,
    });
  });
});
