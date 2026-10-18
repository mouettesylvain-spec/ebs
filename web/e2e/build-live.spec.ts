import { expect, test } from "@playwright/test";
import { ids, login, ui } from "./fixtures";

// R1: the build list of a domain, with filters, newest first.
test("build list filters by user, status and flow", async ({ page }) => {
  await login(page);
  const id = await ids(page);
  await page.goto(ui("/d/dom-a/builds"));
  const rows = page.getByRole("table").locator("tbody tr");
  await expect(rows).toHaveCount(2);
  await expect(rows.first()).toContainText(`#${id.live}`); // newest first
  await page.getByLabel("Status").selectOption("failed");
  await expect(rows).toHaveCount(1);
  await expect(rows.first()).toContainText(`#${id.regression}`);
  await expect(page).toHaveURL(/status=failed/); // linkable
  await page.getByLabel("Status").selectOption("");
  await page.getByLabel("User").fill("bob");
  await expect(rows).toHaveCount(0); // bob's build is in dom-b
  await page.getByLabel("User").fill("");
  await page.getByLabel("Flow (project)").fill("rv-core");
  await expect(rows).toHaveCount(2);
});

// R1: the DAG by step with counts per state; a step expands to its actions.
test("build page shows the DAG grouped by step", async ({ page }) => {
  await login(page);
  const id = await ids(page);
  await page.goto(ui(`/d/dom-a/builds/${id.regression}`));
  const sim = page.getByRole("button", { name: /^sim ✗ failed / });
  await expect(sim).toBeVisible();
  await expect(sim).toHaveAccessibleName("sim ✗ failed 5 done, 3 failed, 1 infra failed");
  await expect(sim).toHaveAccessibleDescription("Step sim, 9 actions: 5 done, 3 failed, 1 infra failed");
  await expect(page.getByRole("button", { name: "compile ✓ done 1 cached, 1 done" })).toBeVisible();
  await expect(page.locator(".dag path.edge")).toHaveCount(2); // compile → elab → sim
  await sim.click();
  const list = page.getByRole("region", { name: "Actions of step sim" });
  await expect(list.locator("tbody tr")).toHaveCount(9);
  await expect(list).toContainText("sim[test=t_lsu,seed=2]");
});

// R1: a running build is updated live over SSE.
test("a running build updates live", async ({ page }) => {
  await login(page);
  const id = await ids(page);
  await page.goto(ui(`/d/dom-a/builds/${id.live}`));
  await expect(page.getByRole("status").filter({ hasText: "Live" })).toBeVisible();
  const sim = page.getByRole("button", { name: /^sim / });
  await expect(sim).toHaveAccessibleName(/^sim ▶ running/);
  await page.request.post("/e2e/live"); // more log
  await page.request.post("/e2e/live"); // the sim fails, the build finishes
  await expect(sim).toHaveAccessibleName(/^sim ✗ failed/, { timeout: 15_000 });
  await expect(page.getByRole("status").filter({ hasText: "Finished." })).toBeVisible();
  await expect(page.getByRole("heading", { level: 1 })).toContainText("failed");
});
