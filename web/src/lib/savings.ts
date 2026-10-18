// Cache savings and license waits (R4) from `GET /domains/{domain}/stats`.

import type { PendingWait, StepStats } from "../api/types";

export interface StepSavings {
  step: string;
  cached: number;
  ran: number;
  hitRate: number | null; // null: nothing cached or executed
  savedCpuHours: number;
}

export interface Savings {
  cached: number;
  ran: number;
  hitRate: number | null;
  savedCpuHours: number; // CPU time the cached results recorded when they were produced
  ranCpuHours: number;
  steps: StepSavings[]; // most CPU saved first
}

const rate = (cached: number, ran: number): number | null =>
  cached + ran === 0 ? null : cached / (cached + ran);

export function savings(steps: StepStats[]): Savings {
  let cached = 0;
  let ran = 0;
  let saved = 0;
  let spent = 0;
  for (const s of steps) {
    cached += s.cached;
    ran += s.ran;
    saved += s.cached_cpu_s;
    spent += s.ran_cpu_s;
  }
  return {
    cached,
    ran,
    hitRate: rate(cached, ran),
    savedCpuHours: saved / 3600,
    ranCpuHours: spent / 3600,
    steps: steps
      .map((s) => ({
        step: s.step,
        cached: s.cached,
        ran: s.ran,
        hitRate: rate(s.cached, s.ran),
        savedCpuHours: s.cached_cpu_s / 3600,
      }))
      .sort((x, y) => y.savedCpuHours - x.savedCpuHours || (x.step < y.step ? -1 : 1)),
  };
}

export const UNNAMED_FEATURE = "(not named)";

export interface FeatureWait {
  feature: string;
  waits: number;
  hours: number;
  meanMinutes: number;
}

export interface ReasonWait {
  reason: string;
  waits: number;
  hours: number;
}

/** License waits per feature (longest total first) and the other pending reasons. */
export function licenseWaits(waits: PendingWait[]): {
  features: FeatureWait[];
  otherReasons: ReasonWait[];
} {
  const features = waits
    .filter((w) => w.reason === "licenses")
    .map((w) => ({
      feature: w.feature || UNNAMED_FEATURE,
      waits: w.waits,
      hours: w.wait_s / 3600,
      meanMinutes: w.waits === 0 ? 0 : w.wait_s / w.waits / 60,
    }))
    .sort((x, y) => y.hours - x.hours || (x.feature < y.feature ? -1 : 1));
  const otherReasons = waits
    .filter((w) => w.reason !== "licenses")
    .map((w) => ({ reason: w.reason, waits: w.waits, hours: w.wait_s / 3600 }))
    .sort((x, y) => y.hours - x.hours);
  return { features, otherReasons };
}
