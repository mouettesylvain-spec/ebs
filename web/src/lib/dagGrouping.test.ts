import { describe, expect, it } from "vitest";
import { applyEvent, groupBySteps, summaryState, type ActionLite } from "./dagGrouping";

const a = (action_id: string, state: ActionLite["state"], step?: string): ActionLite => ({
  action_id,
  step: step ?? action_id.split("[")[0]!,
  state,
});

// R1: actions are grouped by step with per-state counts, steps in DAG order.
describe("groupBySteps", () => {
  it("groups, counts and orders steps topologically", () => {
    const actions = [
      a("sim[2]", "failed"),
      a("sim[1]", "done"),
      a("compile[b]", "cached"),
      a("compile[a]", "done"),
      a("elab", "running"),
      a("lint", "queued"),
    ];
    const graph = { compile: [], elab: ["compile"], sim: ["elab"], lint: [] };
    const { groups, edges } = groupBySteps(actions, graph);
    expect(groups.map((g) => g.step)).toEqual(["compile", "lint", "elab", "sim"]);
    const sim = groups.find((g) => g.step === "sim")!;
    expect(sim.total).toBe(2);
    expect(sim.counts).toEqual({ failed: 1, done: 1 });
    expect(sim.actions.map((x) => x.action_id)).toEqual(["sim[1]", "sim[2]"]);
    expect(sim.state).toBe("failed");
    expect(groups.find((g) => g.step === "compile")!.state).toBe("done");
    expect(edges).toEqual([
      ["compile", "elab"],
      ["elab", "sim"],
    ]);
  });

  it("keeps steps missing from the graph, and drops edges to unknown steps", () => {
    const { groups, edges } = groupBySteps([a("b", "done"), a("a", "done")], { b: ["gone"] });
    expect(groups.map((g) => g.step)).toEqual(["a", "b"]);
    expect(edges).toEqual([]);
  });

  it("breaks cycles instead of losing steps", () => {
    const { groups } = groupBySteps([a("x", "done"), a("y", "done")], { x: ["y"], y: ["x"] });
    expect(groups.map((g) => g.step).sort()).toEqual(["x", "y"]);
  });
});

// R1: the summary of a step shows its most urgent state.
describe("summaryState", () => {
  it("ranks failures over running over pending over done", () => {
    expect(summaryState({ done: 3, failed: 1, running: 2 })).toBe("failed");
    expect(summaryState({ done: 3, infra_failed: 1, running: 2 })).toBe("infra_failed");
    expect(summaryState({ done: 3, running: 2, pending: 1 })).toBe("running");
    expect(summaryState({ cached: 3 })).toBe("cached");
    expect(summaryState({ cached: 3, done: 1 })).toBe("done");
    expect(summaryState({})).toBe("queued");
  });
});

// R1: SSE events move actions between states live.
describe("applyEvent", () => {
  it("follows the driver's events", () => {
    const actions = new Map([["sim[1]", a("sim[1]", "queued")]]);
    const ev = (type: string, data: Record<string, unknown> = {}) => ({
      v: 1 as const,
      ts: "2026-10-01T00:00:00Z",
      build: 1,
      type,
      action_id: "sim[1]",
      data,
    });
    expect(applyEvent(actions, ev("pending", { reason: "licenses" }))).toBe(true);
    expect(actions.get("sim[1]")!.state).toBe("pending");
    expect(actions.get("sim[1]")!.pending_reason).toBe("licenses");
    applyEvent(actions, ev("running", { job_id: "7" }));
    expect(actions.get("sim[1]")!.state).toBe("running");
    applyEvent(actions, ev("finished", { state: "failed", exit_code: 1 }));
    expect(actions.get("sim[1]")!.state).toBe("failed");
    // An unknown action is added; build-level events change nothing.
    expect(applyEvent(actions, { ...ev("cache_hit"), action_id: "lint" })).toBe(true);
    expect(actions.get("lint")).toEqual({ action_id: "lint", step: "lint", state: "cached" });
    expect(applyEvent(actions, { ...ev("build_finished"), action_id: null })).toBe(false);
  });

  it("treats a retry as queued again", () => {
    const actions = new Map([["x", a("x", "infra_failed")]]);
    applyEvent(actions, {
      v: 1,
      ts: "2026-10-01T00:00:00Z",
      build: 1,
      type: "retrying",
      action_id: "x",
      data: { attempt: 2, delay_ms: 10 },
    });
    expect(actions.get("x")!.state).toBe("queued");
  });
});
