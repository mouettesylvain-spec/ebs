import { expect, test } from "@playwright/test";
import { expectAccessible, ids, login, ui } from "./fixtures";

const pages = (id: { regression: number }) => [
  ["home", "/"],
  ["builds", "/d/dom-a/builds"],
  ["build", `/d/dom-a/builds/${id.regression}`],
  ["regression", `/d/dom-a/builds/${id.regression}/regression`],
  ["log", `/d/dom-a/builds/${id.regression}/log/${encodeURIComponent("sim[test=t_lsu,seed=2]")}`],
  ["metrics", "/d/dom-a/metrics"],
  ["releases", "/d/dom-a/releases"],
  ["provenance", "/d/dom-a/provenance"],
];

// R6: axe-core finds no WCAG 2.1 AA violation (contrast included) on any page, light and dark.
for (const scheme of ["light", "dark"] as const) {
  test(`every page is accessible (${scheme})`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: scheme });
    await login(page);
    const id = await ids(page);
    for (const [name, route] of pages(id)) {
      await test.step(name!, async () => {
        await page.goto(ui(route!));
        await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
        await expect(page.getByText("Loading…")).toHaveCount(0);
        await expect(page.getByText("Laying out the graph…")).toHaveCount(0);
        await expectAccessible(page);
      });
    }
  });
}

// R6: the theme toggle overrides the system preference.
test("dark mode toggle", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "light" });
  await login(page);
  await page.getByLabel("Theme").selectOption("dark");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  expect(bg).toBe("rgb(21, 23, 27)");
});

// R6: the DAG and grid work from the keyboard alone.
test("keyboard navigation", async ({ page }) => {
  await login(page);
  const id = await ids(page);
  await page.goto(ui(`/d/dom-a/builds/${id.regression}`));
  const compile = page.getByRole("button", { name: /^compile / });
  await compile.focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.getByRole("button", { name: /^lint / })).toBeFocused();
  await page.keyboard.press("ArrowRight");
  await page.keyboard.press("ArrowRight");
  const sim = page.getByRole("button", { name: /^sim / });
  await expect(sim).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(sim).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("region", { name: "Actions of step sim" })).toBeVisible();
  // Tab reaches the log links of the expanded step.
  await page.keyboard.press("Tab");
  await expect(page.getByRole("region", { name: "Actions of step sim" }).getByRole("link").first()).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Log of");
});
