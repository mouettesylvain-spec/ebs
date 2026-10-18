import { describe, expect, it } from "vitest";
import { licenseWaits, savings } from "./savings";

const step = (s: string, cached: number, ran: number, cached_cpu_s: number, ran_cpu_s = 0) => ({
  step: s,
  cached,
  ran,
  cached_cpu_s,
  cached_wall_s: cached_cpu_s,
  ran_cpu_s,
});

// R4: hit rate and saved CPU-hours from the cached actions' recorded resources.
describe("savings", () => {
  it("totals and ranks steps by CPU saved", () => {
    const s = savings([step("compile", 3, 1, 7200, 100), step("sim", 1, 3, 1800, 900)]);
    expect(s.cached).toBe(4);
    expect(s.ran).toBe(4);
    expect(s.hitRate).toBe(0.5);
    expect(s.savedCpuHours).toBe(2.5);
    expect(s.ranCpuHours).toBeCloseTo(0.2778, 4);
    expect(s.steps.map((x) => [x.step, x.hitRate, x.savedCpuHours])).toEqual([
      ["compile", 0.75, 2],
      ["sim", 0.25, 0.5],
    ]);
  });

  it("has no hit rate without executed or cached actions", () => {
    const s = savings([step("lint", 0, 0, 0)]);
    expect(s.hitRate).toBeNull();
    expect(s.steps[0]!.hitRate).toBeNull();
    expect(savings([]).savedCpuHours).toBe(0);
  });
});

// R4: license wait time per feature, longest first; other reasons apart.
describe("licenseWaits", () => {
  it("splits license waits by feature", () => {
    const w = licenseWaits([
      { reason: "licenses", feature: "simfeat", waits: 2, wait_s: 600 },
      { reason: "licenses", feature: "", waits: 1, wait_s: 30 },
      { reason: "licenses", feature: "lintfeat", waits: 4, wait_s: 7200 },
      { reason: "resources", feature: null, waits: 3, wait_s: 90 },
    ]);
    expect(w.features).toEqual([
      { feature: "lintfeat", waits: 4, hours: 2, meanMinutes: 30 },
      { feature: "simfeat", waits: 2, hours: 600 / 3600, meanMinutes: 5 },
      { feature: "(not named)", waits: 1, hours: 30 / 3600, meanMinutes: 0.5 },
    ]);
    expect(w.otherReasons).toEqual([{ reason: "resources", waits: 3, hours: 90 / 3600 }]);
  });
});
