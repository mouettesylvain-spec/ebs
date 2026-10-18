import { describe, expect, it } from "vitest";
import { LogLines, restarted, visibleRange } from "./logModel";

const enc = new TextEncoder();

// R3: chunks decode into lines, even with a character split between two chunks.
describe("LogLines", () => {
  it("joins chunks into lines", () => {
    const log = new LogLines();
    const bytes = enc.encode("héllo\nwörld\n** Error: x\n");
    log.append(bytes.slice(0, 2)); // splits the é
    log.append(bytes.slice(2, 9));
    log.append(bytes.slice(9));
    log.end();
    expect(log.lines.slice(0, log.count)).toEqual(["héllo", "wörld", "** Error: x"]);
    expect(log.bytes).toBe(bytes.length);
    expect(log.search("WÖR")).toEqual([1]);
    expect(log.search("")).toEqual([]);
    expect(log.firstError()).toBe(2);
  });

  it("has no first error in a clean log", () => {
    const log = new LogLines();
    log.append(enc.encode("# UVM_ERROR :    0\nok"));
    expect(log.count).toBe(2);
    expect(log.firstError()).toBe(-1);
  });
});

// R3: a large log draws only the rows in view.
describe("visibleRange", () => {
  it("windows a million lines", () => {
    expect(visibleRange(0, 400, 20, 1_000_000)).toEqual([0, 40]);
    expect(visibleRange(200_000, 400, 20, 1_000_000)).toEqual([9980, 10040]);
    expect(visibleRange(0, 400, 20, 5)).toEqual([0, 5]);
  });
});

// R3 (review): a retry truncates the live log; the viewer starts over instead of keeping the
// old attempt's lines.
describe("restarted", () => {
  it("detects a log shorter than what was read", () => {
    expect(restarted(100, 40)).toBe(true);
    expect(restarted(100, 100)).toBe(false);
    expect(restarted(100, 250)).toBe(false);
  });
});
