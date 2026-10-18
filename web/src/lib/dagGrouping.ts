// The build page's DAG (R1): actions grouped by step, steps ordered and linked by the step graph
// of the `plan_ready` event, kept live by applying the build's SSE events.

import type { ActionState, EbsEvent } from "../api/types";

export interface ActionLite {
  action_id: string;
  step: string;
  state: ActionState;
  pending_reason?: string | null;
}

export type StateCounts = Partial<Record<ActionState, number>>;

export interface StepGroup {
  step: string;
  total: number;
  counts: StateCounts;
  state: ActionState; // the most urgent state of its actions
  actions: ActionLite[]; // by action id
}

/** Most urgent first: what a step box shows when its actions differ. */
const URGENCY: ActionState[] = [
  "failed",
  "infra_failed",
  "running",
  "pending",
  "queued",
  "cancelled",
  "skipped",
  "done",
  "cached",
];

export function summaryState(counts: StateCounts): ActionState {
  for (const s of URGENCY) {
    if ((counts[s] ?? 0) > 0) return s;
  }
  return "queued";
}

/** Steps in topological order of `graph` (step -> steps it consumes), ties by name; steps of
 * a cycle or missing from the graph keep their name order at the end. */
function orderSteps(steps: string[], graph: Record<string, string[]>): string[] {
  const known = new Set(steps);
  const ups = new Map(steps.map((s) => [s, (graph[s] ?? []).filter((u) => known.has(u))]));
  const done = new Set<string>();
  const order: string[] = [];
  let ready = steps.filter((s) => ups.get(s)!.length === 0).sort();
  while (ready.length > 0) {
    const next: string[] = [];
    for (const s of ready) {
      done.add(s);
      order.push(s);
    }
    for (const s of steps) {
      if (!done.has(s) && !next.includes(s) && ups.get(s)!.every((u) => done.has(u))) next.push(s);
    }
    ready = next.sort();
  }
  return [...order, ...steps.filter((s) => !done.has(s)).sort()];
}

export function groupBySteps(
  actions: Iterable<ActionLite>,
  graph: Record<string, string[]> = {},
): { groups: StepGroup[]; edges: [string, string][] } {
  const by = new Map<string, ActionLite[]>();
  for (const a of actions) {
    const list = by.get(a.step) ?? [];
    list.push(a);
    by.set(a.step, list);
  }
  const groups = orderSteps([...by.keys()], graph).map((step) => {
    const list = by.get(step)!.sort((x, y) => (x.action_id < y.action_id ? -1 : 1));
    const counts: StateCounts = {};
    for (const a of list) counts[a.state] = (counts[a.state] ?? 0) + 1;
    return { step, total: list.length, counts, state: summaryState(counts), actions: list };
  });
  const edges: [string, string][] = [];
  for (const g of groups) {
    for (const up of [...(graph[g.step] ?? [])].sort()) {
      if (by.has(up) && up !== g.step) edges.push([up, g.step]);
    }
  }
  return { groups, edges };
}

const FINAL = new Set<string>(["done", "failed", "infra_failed", "cached", "cancelled", "skipped"]);

/** Apply one driver event to `actions` (by id); true if an action changed. */
export function applyEvent(actions: Map<string, ActionLite>, event: EbsEvent): boolean {
  const id = event.action_id;
  if (!id) return false;
  let state: ActionState | null = null;
  let reason: string | null | undefined;
  switch (event.type) {
    case "cache_hit":
      state = "cached";
      break;
    case "submitted":
    case "retrying":
      state = "queued";
      break;
    case "pending":
      state = "pending";
      reason = typeof event.data.reason === "string" ? event.data.reason : "other";
      break;
    case "running":
      state = "running";
      break;
    case "infra_failed":
      state = "infra_failed";
      break;
    case "finished": {
      const s = event.data.state;
      if (typeof s === "string" && FINAL.has(s)) state = s as ActionState;
      break;
    }
  }
  if (state === null) return false;
  const old = actions.get(id);
  const step = old?.step ?? id.split("[")[0]!;
  const next: ActionLite = { action_id: id, step, state };
  if (state === "pending") next.pending_reason = reason ?? null;
  actions.set(id, next);
  return true;
}
