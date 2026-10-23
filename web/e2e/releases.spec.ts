import { expect, test } from "@playwright/test";
import { ids, login, ui } from "./fixtures";

// R5: releases with their channels, and the provenance explorer (why / used-by / tests).
test("releases, channels and provenance", async ({ page }) => {
  await login(page);
  const id = await ids(page);
  await page.goto(ui("/d/dom-a/releases"));
  const rel = page.getByRole("region", { name: "Release rtl/core" });
  await expect(rel.getByRole("listitem").filter({ hasText: "stable" })).toContainText("2026.10.1");
  await expect(rel.getByRole("listitem").filter({ hasText: "nightly" })).toContainText("not set");
  // P3-05: the latest revalidation of each release, with its verdict and counts.
  const version = rel.getByRole("row").filter({ hasText: "2026.10.1" }).first();
  await expect(version).toContainText("differences");
  await expect(version).toContainText("7 equivalent, 1 changed, 0 incomparable");
  await rel.getByRole("button", { name: "2026.10.1" }).click();
  // Source build column first; the revalidation column links the same build again.
  await expect(rel.getByRole("link", { name: `#${id.regression}` }).first()).toBeVisible();

  await rel.getByRole("link", { name: "used by" }).click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Provenance in dom-a");
  const actions = page.getByRole("table", { name: "Actions" });
  await expect(actions).toContainText("elab");
  await expect(actions).toContainText("sim[test=t_fpu,seed=1]");

  await page.getByRole("radio", { name: "tests" }).click();
  await expect(page.getByRole("radio", { name: "tests" })).toBeChecked();
  await expect(actions.locator("tbody tr")).toHaveCount(8); // the sims that ran (not the infra failure)
  await expect(actions.getByRole("columnheader", { name: "Test" })).toBeVisible();
  await expect(actions).not.toContainText("elab");

  await page.getByRole("radio", { name: "why" }).click();
  await expect(page.getByRole("radio", { name: "why" })).toBeChecked();
  // compile[core] made it, in both builds that hold its key (the second one a cache hit).
  await expect(actions.locator("tbody tr")).toHaveCount(2);
  await expect(actions.locator("tbody tr td:nth-child(2)")).toHaveText(["compile[core]", "compile[core]"]);
  await expect(page.getByText("rtl/core 2026.10.1")).toBeVisible();
});

// R4: cache hit rate, saved CPU-hours and license wait per feature.
test("metrics", async ({ page }) => {
  await login(page);
  await page.goto(ui("/d/dom-a/metrics"));
  await expect(page.locator(".tile").filter({ hasText: "Cache hit rate" })).toContainText("21.4 %"); // 3 of 14
  await expect(page.locator(".tile").filter({ hasText: "CPU-hours saved" })).toContainText("3.02");
  const lic = page.getByRole("table", { name: "License wait by feature" });
  await expect(lic.locator("tbody tr").first()).toContainText("simfeat");
  await expect(lic.locator("tbody tr")).toHaveCount(2);
});
