import { expect, test } from "@playwright/test";
import { ids, login, ui } from "./fixtures";

// R2: test × seed grid with pass / fail / infra states (as text, not only colour), failure
// signatures clustered, click-through to the log.
test("regression grid and failure clusters", async ({ page }) => {
  await login(page);
  const id = await ids(page);
  await page.goto(ui(`/d/dom-a/builds/${id.regression}/regression`));
  const grid = page.getByRole("table", { name: "Regression grid of step sim" });
  await expect(grid.locator("tbody tr")).toHaveCount(3);
  await expect(grid.getByRole("columnheader")).toHaveText(["Test", "seed 1", "seed 2", "seed 3"]);
  await expect(grid.getByRole("link", { name: /^t_lsu seed 2: failed \(UVM_ERROR <path>/ })).toBeVisible();
  await expect(grid.getByRole("link", { name: "t_alu seed 3: infrastructure failure" })).toBeVisible();
  await expect(grid.getByRole("link", { name: "t_alu seed 1: passed" })).toBeVisible();

  const clusters = page.getByRole("table", { name: "Failure signatures" }).locator("tbody tr");
  await expect(clusters).toHaveCount(2);
  await expect(clusters.nth(0)).toContainText("2");
  await expect(clusters.nth(0)).toContainText("UVM_ERROR <path>(<n>) @ <n>: top.env.scb [SCB] data mismatch");
  await expect(clusters.nth(1)).toContainText("** Fatal: timeout after <n> us");

  await page.getByLabel("Only tests with failures").check();
  await expect(grid.locator("tbody tr")).toHaveCount(2);

  await grid.getByRole("link", { name: /^t_lsu seed 3: failed/ }).click();
  await expect(page.getByRole("heading", { level: 1 })).toContainText("sim[test=t_lsu,seed=3]");
});
