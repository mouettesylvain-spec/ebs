# P2-06 — Web dashboard v1

Status: todo · Phase: 2 · Depends on: P2-05, P2-04 · Size: L

## Goal
A read-mostly web UI for builds, regressions, logs, cache savings, license waits, releases and provenance.

## Read first
- docs/architecture.md § "Debugging, CLI, dashboard and CI" → Web dashboard (feature list)
- `.claude/skills` has no frontend skill yet: follow `web/README.md` conventions created here

## Scope (files)
- create `web/` (React + TypeScript + Vite; routing; API client generated from OpenAPI; ELK.js for DAG layout)
- tests: Vitest unit tests for components/data transforms, Playwright e2e against the service with seeded data

## Requirements
- R1 Build list per team/domain with filters (user, status, flow, date); build page with a live DAG grouped by
  step (counts per state, expand to instances) updated over SSE.
- R2 Regression grid: test × seed matrix with pass/fail/infra/cached colours, failure signatures clustered by
  normalized first error line, click-through to logs.
- R3 Log viewer: streams live log or CAS log with search and jump-to-first-error; large logs virtualized.
- R4 Metrics pages: cache hit rate and saved CPU-hours (from cached actions' recorded resources), license wait
  time per feature (pending reason durations).
- R5 Release/channel browser and provenance explorer (why / used-by / tests views).
- R6 Accessibility: keyboard navigation, colour-blind-safe palette (state never encoded by colour alone), dark mode.
- R7 Everything scoped by the same authz as the API (no client-side filtering of forbidden data).

## Tests
| Test | Covers | Kind |
| --- | --- | --- |
| Vitest: `dagGrouping.test.ts`, `signatureCluster.test.ts`, `savings.test.ts` | R1, R2, R4 | unit |
| Playwright: `build-live.spec.ts`, `regression-grid.spec.ts`, `logs.spec.ts`, `releases.spec.ts` | R1–R5 | e2e |
| Playwright + axe-core accessibility checks on each page | R6 | e2e |
| `authz.spec.ts` (user without domain sees nothing) | R7 | e2e |
