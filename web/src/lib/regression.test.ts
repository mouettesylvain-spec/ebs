import { describe, expect, it } from "vitest";
import type { ActionRow, ResultSummary } from "../api/types";
import { buildGrid, cellKey, regressionSteps } from "./regression";

const row = (action_id: string, state: ActionRow["state"], params?: ActionRow["params"]): ActionRow =>
  ({ action_id, step: "sim", state, params: params ?? null, rule: "questa.sim" }) as ActionRow;

const res = (action_id: string, status: "passed" | "failed", summary = {}): ResultSummary => ({
  action_id,
  status,
  exit_code: status === "passed" ? 0 : 1,
  log: null,
  summary,
});

// R2: test × seed cells with pass / fail / infra / cached kinds and the failure signature.
describe("buildGrid", () => {
  it("places actions by test and seed", () => {
    const grid = buildGrid(
      [
        row("sim[a,2]", "done", { test: "a", seed: "2" }),
        row("sim[a,10]", "failed", { test: "a", seed: "10" }),
        row("sim[b,2]", "cached", { test: "b", seed: "2" }),
        row("sim[b,3]", "infra_failed", { test: "b", seed: "3" }),
        row("sim[c]", "done"), // test and seed only in the result summary
        row("sim[x]", "running"),
      ],
      [
        res("sim[a,2]", "passed"),
        res("sim[a,10]", "failed", { signature: "UVM_ERROR <n>" }),
        res("sim[b,2]", "failed"),
        res("sim[c]", "passed", { test: "c", seed: 2 }),
      ],
    );
    expect(grid.tests).toEqual(["a", "b", "c"]);
    expect(grid.seeds).toEqual(["2", "3", "10"]); // numeric order
    expect(grid.cells.get(cellKey("a", "2"))!.kind).toBe("pass");
    expect(grid.cells.get(cellKey("a", "10"))).toEqual({
      action_id: "sim[a,10]",
      kind: "fail",
      signature: "UVM_ERROR <n>",
    });
    expect(grid.cells.get(cellKey("b", "2"))!.kind).toBe("cached-fail");
    expect(grid.cells.get(cellKey("b", "3"))!.kind).toBe("infra");
    expect(grid.cells.get(cellKey("c", "2"))!.kind).toBe("pass");
    expect(grid.unplaced).toEqual(["sim[x]"]);
  });

  it("finds the steps that are regressions", () => {
    const rows = [
      row("sim[1]", "done", { test: "a" }),
      { ...row("compile", "done"), step: "compile", rule: "questa.compile" } as ActionRow,
    ];
    expect(regressionSteps(rows)).toEqual(["sim"]);
  });
});
