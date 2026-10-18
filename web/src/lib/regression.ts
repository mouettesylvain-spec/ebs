// The regression grid (R2): one row per test, one column per seed, from a step's actions (their
// plan params `test` and `seed`, else the result summary's) and their recorded results.

import type { ActionRow, ResultSummary } from "../api/types";
import { cellKind, type CellKind } from "./states";

export interface Cell {
  action_id: string;
  kind: CellKind;
  signature: string | null;
}

export interface Grid {
  tests: string[];
  seeds: string[];
  cells: Map<string, Cell>; // `${test}\u0000${seed}`
  unplaced: string[]; // actions without a test param
}

export const cellKey = (test: string, seed: string) => `${test}\u0000${seed}`;

function param(row: ActionRow, result: ResultSummary | undefined, name: string): string | null {
  const v = row.params?.[name] ?? result?.summary[name];
  return v === undefined || v === null ? null : String(v);
}

const bySeed = (a: string, b: string) =>
  /^\d+$/.test(a) && /^\d+$/.test(b) ? Number(a) - Number(b) : a < b ? -1 : a > b ? 1 : 0;

export function buildGrid(rows: ActionRow[], results: ResultSummary[]): Grid {
  const res = new Map(results.map((r) => [r.action_id, r]));
  const tests = new Set<string>();
  const seeds = new Set<string>();
  const cells = new Map<string, Cell>();
  const unplaced: string[] = [];
  for (const row of rows) {
    const r = res.get(row.action_id);
    const test = param(row, r, "test");
    if (test === null) {
      unplaced.push(row.action_id);
      continue;
    }
    const seed = param(row, r, "seed") ?? "—";
    tests.add(test);
    seeds.add(seed);
    const sig = r?.summary.signature;
    cells.set(cellKey(test, seed), {
      action_id: row.action_id,
      kind: cellKind(row.state, r?.status),
      signature: typeof sig === "string" && sig ? sig : null,
    });
  }
  return { tests: [...tests].sort(), seeds: [...seeds].sort(bySeed), cells, unplaced };
}

/** The steps worth a grid: those with a `*.sim` rule or a `test` param, most actions first. */
export function regressionSteps(rows: ActionRow[]): string[] {
  const count = new Map<string, number>();
  for (const r of rows) {
    if (r.rule?.endsWith(".sim") || (r.params && "test" in r.params)) {
      count.set(r.step, (count.get(r.step) ?? 0) + 1);
    }
  }
  return [...count.entries()].sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1)).map(([s]) => s);
}
