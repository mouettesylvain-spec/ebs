import { expect, test } from "@playwright/test";
import { ids, login, ui } from "./fixtures";

// R7: a user without the domain sees nothing; the service refuses, the page never gets data.
test("a user without a domain sees nothing", async ({ page }) => {
  const bodies: string[] = [];
  page.on("response", async (r) => {
    if (r.url().includes("/api/v1/") && r.ok()) bodies.push(await r.text());
  });
  await login(page, "dave");
  const id = await ids(page);
  await expect(page.getByText("You are not a member of any domain")).toBeVisible();

  await page.goto(ui("/d/dom-a/builds"));
  await expect(page.getByRole("alert")).toContainText("403");
  await page.goto(ui(`/d/dom-a/builds/${id.regression}`));
  await expect(page.getByRole("alert")).toContainText(`404: no build with id ${id.regression}`);
  await page.goto(ui("/d/dom-a/metrics"));
  await expect(page.getByRole("alert")).toContainText("403");
  await page.goto(ui("/d/dom-a/releases"));
  await expect(page.getByRole("alert")).toContainText("403");
  // The only successful API answers were dave's own identity.
  expect(bodies.every((b) => b.includes('"name":"dave"'))).toBe(true);
  expect(bodies.join("")).not.toContain("rv-core");
});

// R7: a member of one domain cannot reach another's builds, even by id.
test("another domain's build looks missing", async ({ page }) => {
  await login(page, "alice");
  const id = await ids(page);
  await page.goto(ui(`/d/dom-b/builds/${id.other}`));
  await expect(page.getByRole("alert")).toContainText(`404: no build with id ${id.other}`);
  await page.goto(ui("/d/dom-b/builds"));
  await expect(page.getByRole("alert")).toContainText("not a member of domain 'dom-b'");
  await expect(page.getByRole("combobox", { name: "Domain" }).locator("option")).toHaveText(["dom-a"]); // dom-b is not offered
});
