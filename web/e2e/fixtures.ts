import AxeBuilder from "@axe-core/playwright";
import { expect, type Page } from "@playwright/test";

export interface Ids {
  regression: number;
  live: number;
  live_action: string;
  other: number;
}

/** Log in as `user` (the e2e server sets the session cookie as the OIDC callback would). */
export async function login(page: Page, user = "alice"): Promise<void> {
  await page.goto(`/e2e/login?user=${user}`);
  await expect(page.getByText(user, { exact: true })).toBeVisible();
}

export async function ids(page: Page): Promise<Ids> {
  const r = await page.request.get("/e2e/ids");
  return (await r.json()) as Ids;
}

export const ui = (route: string) => `/ui/#${route}`;

/** No axe violations (WCAG 2.1 AA, which includes colour contrast) on the current page. */
export async function expectAccessible(page: Page): Promise<void> {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  const summary = results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ") + " " + (n.failureSummary ?? "") + " " + n.html.slice(0, 300)).join(", ")}`);
  expect(summary).toEqual([]);
}
