import { expect, test } from "@playwright/test";
import { ids, login, ui } from "./fixtures";

const logUrl = (build: number, action: string) => ui(`/d/dom-a/builds/${build}/log/${encodeURIComponent(action)}`);

// R3: a stored log with search and jump-to-first-error, drawn virtualized.
test("stored log: search, first error, virtualized", async ({ page }) => {
  await login(page);
  const id = await ids(page);
  await page.goto(logUrl(id.regression, "sim[test=t_lsu,seed=2]"));
  const log = page.getByRole("log");
  await expect(page.getByRole("status").filter({ hasText: "Stored log" })).toContainText("3003 lines");
  await expect(log).toHaveAttribute("data-lines", "3003");
  expect(await log.locator(".row").count()).toBeLessThan(200); // 3003 lines, few rows drawn

  await page.getByRole("button", { name: "Jump to first error" }).click();
  await expect(log.locator(".row.err")).toBeVisible();
  await expect(log.locator(".row.err")).toContainText("UVM_ERROR tb/scb.sv(42) @ 2000");

  await page.getByLabel("Search").fill("item 2999");
  await expect(page.getByText("1 of 1")).toBeVisible();
  await expect(log.locator(".row.cur")).toContainText("item 2999");
  await page.getByLabel("Search").fill("UVM_INFO");
  await expect(page.getByText("1 of 3000")).toBeVisible();
  await page.getByLabel("Search").press("Enter");
  await expect(page.getByText("2 of 3000")).toBeVisible();
});

// R3: a running action's live log, refreshed while it grows.
test("live log streams", async ({ page }) => {
  await login(page);
  const id = await ids(page);
  await page.goto(logUrl(id.live, id.live_action));
  const log = page.getByRole("log");
  await expect(log).toContainText("UVM_INFO started");
  // The live build test may already have advanced the script; either way the log grows to
  // the late mismatch (polled every 5 s).
  await page.request.post("/e2e/live");
  await expect(log).toContainText("late mismatch", { timeout: 15_000 });
});

// R3: no log yet.
test("an action without a log says so", async ({ page }) => {
  await login(page);
  const id = await ids(page);
  await page.goto(logUrl(id.regression, "sim[test=t_alu,seed=3]"));
  await expect(page.getByRole("status").filter({ hasText: "No log yet" })).toBeVisible();
});
