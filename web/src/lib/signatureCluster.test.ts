import { describe, expect, it } from "vitest";
import { clusterFailures, errorSignature, firstErrorLine, NO_SIGNATURE } from "./signatureCluster";

// R2: the same normalization as ebs.rules.questa.transcript.error_signature (its test cases),
// so a signature computed here clusters with the ones the rule recorded.
describe("errorSignature", () => {
  it.each([
    ["# ** Error: /a/b/c.sv(12): bad", "** Error: <path>(<n>): bad"],
    [
      "UVM_ERROR x.sv(7) @ 1250: top.env [ID] got 0xDEAD_beef",
      "UVM_ERROR x.sv(<n>) @ <n>: top.env [ID] got <hex>",
    ],
    ["** Fatal: value 8'hff != 8'b0000_0001 at 12.5 ns", "** Fatal: value <hex> != <hex> at <n> ns"],
    ["** Error: agent_1 and core0 differ by 3", "** Error: agent_1 and core0 differ by <n>"],
    ["** Error:   lots    of   space  ", "** Error: lots of space"],
    ["** Error: Undefined 'data_q' and 'bad'", "** Error: Undefined 'data_q' and 'bad'"],
    ["** Error: a.sv(3): (vlog-2110) x", "** Error: a.sv(<n>): (vlog-2110) x"],
    ["** Error: 3 / 4 lanes", "** Error: <n> / <n> lanes"],
  ])("%s", (line, signature) => {
    expect(errorSignature(line)).toBe(signature);
    expect(errorSignature(signature)).toBe(signature); // a fixed point
  });

  it("caps the signature at 512 characters", () => {
    expect(errorSignature("** Error: " + "x".repeat(5000)).length).toBe(512);
  });
});

// R2: the first error line of a log, as the transcript parser picks it.
describe("firstErrorLine", () => {
  it("skips UVM summary counts and finds the first report or ** Error/Fatal", () => {
    const log = [
      "# vsim -c top",
      "# UVM_INFO tb.sv(1) @ 0: top [START] go",
      "# UVM_ERROR :    0",
      "# UVM_ERROR tb.sv(9) @ 20: top.scb [SCB] mismatch",
      "# ** Error: later",
    ].join("\n");
    expect(firstErrorLine(log)).toBe("UVM_ERROR tb.sv(9) @ 20: top.scb [SCB] mismatch");
    expect(firstErrorLine("all good\n# ** Note: $finish")).toBeNull();
    expect(firstErrorLine("** Fatal: (SIGSEGV) bad handle")).toBe("** Fatal: (SIGSEGV) bad handle");
  });
});

// R2: failures cluster by signature, the biggest cluster first.
describe("clusterFailures", () => {
  it("groups by recorded or computed signature", () => {
    const clusters = clusterFailures([
      { action_id: "sim[a]", signature: "UVM_ERROR x.sv(<n>) @ <n>: top [SCB] mismatch" },
      { action_id: "sim[b]", firstError: "UVM_ERROR x.sv(12) @ 99: top [SCB] mismatch" },
      { action_id: "sim[c]", firstError: "** Fatal: timeout at 100 ns" },
      { action_id: "sim[d]" },
      { action_id: "sim[e]", signature: "** Fatal: timeout at <n> ns" },
      { action_id: "sim[f]", signature: "UVM_ERROR x.sv(<n>) @ <n>: top [SCB] mismatch" },
    ]);
    expect(clusters.map((c) => [c.signature, c.actions])).toEqual([
      ["UVM_ERROR x.sv(<n>) @ <n>: top [SCB] mismatch", ["sim[a]", "sim[b]", "sim[f]"]],
      ["** Fatal: timeout at <n> ns", ["sim[c]", "sim[e]"]],
      [NO_SIGNATURE, ["sim[d]"]],
    ]);
  });
});
