// How states look (R6): an Okabe–Ito colour (safe for the common colour-vision deficiencies)
// AND a glyph AND a text label. Colour never carries a state alone.

export interface StateStyle {
  label: string;
  glyph: string;
  cls: string; // CSS class: colour tokens in styles.css, light and dark
}

export const STATE_STYLES: Record<string, StateStyle> = {
  done: { label: "done", glyph: "✓", cls: "st-pass" },
  passed: { label: "passed", glyph: "✓", cls: "st-pass" },
  cached: { label: "cached", glyph: "◆", cls: "st-cached" },
  failed: { label: "failed", glyph: "✗", cls: "st-fail" },
  infra_failed: { label: "infra failed", glyph: "⚠", cls: "st-infra" },
  running: { label: "running", glyph: "▶", cls: "st-run" },
  pending: { label: "pending", glyph: "⏸", cls: "st-wait" },
  queued: { label: "queued", glyph: "○", cls: "st-wait" },
  cancelled: { label: "cancelled", glyph: "⊘", cls: "st-off" },
  skipped: { label: "skipped", glyph: "↷", cls: "st-off" },
};

export function stateStyle(state: string): StateStyle {
  return STATE_STYLES[state] ?? { label: state, glyph: "?", cls: "st-off" };
}

/** A regression cell: the action's state, refined by its result (a cached failure is a fail). */
export type CellKind = "pass" | "fail" | "infra" | "cached-pass" | "cached-fail" | "running" | "waiting" | "other";

export const CELL_STYLES: Record<CellKind, StateStyle> = {
  pass: { label: "passed", glyph: "✓", cls: "st-pass" },
  fail: { label: "failed", glyph: "✗", cls: "st-fail" },
  infra: { label: "infrastructure failure", glyph: "⚠", cls: "st-infra" },
  "cached-pass": { label: "passed (cached)", glyph: "◆", cls: "st-cached" },
  "cached-fail": { label: "failed (cached)", glyph: "✗◆", cls: "st-fail" },
  running: { label: "running", glyph: "▶", cls: "st-run" },
  waiting: { label: "waiting", glyph: "○", cls: "st-wait" },
  other: { label: "not run", glyph: "–", cls: "st-off" },
};

export function cellKind(state: string, status: "passed" | "failed" | undefined): CellKind {
  switch (state) {
    case "done":
      return status === "failed" ? "fail" : "pass";
    case "failed":
      return "fail";
    case "infra_failed":
      return "infra";
    case "cached":
      return status === "failed" ? "cached-fail" : "cached-pass";
    case "running":
      return "running";
    case "queued":
    case "pending":
      return "waiting";
    default:
      return "other";
  }
}
