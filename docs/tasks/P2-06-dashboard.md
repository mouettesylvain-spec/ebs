# P2-06 — Web dashboard v1

Status: review · Phase: 2 · Depends on: P2-05, P2-04 · Size: L

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

## Notes
- Scope grew beyond `web/` (agreed with the maintainer, 2026-10-18): the service had no build
  list, log, result-summary or stats reads. Added read-only routes and the `DashboardReader`
  store protocol (interfaces.md § 6.3), `Whoami.domains`, `plan_ready.steps` (the step graph for
  the DAG) and `pending.licenses` (license waits per feature), and `[meta_service].dashboard_dir`
  to serve `web/dist` at `/ui/`. Each new route is in the I20 matrix.
- Extra tests beyond the table: `regression.test.ts`, `logModel.test.ts` (Vitest);
  `metrics` (in `releases.spec.ts`), `a11y.spec.ts` (keyboard, dark mode); store contract
  `tests/helpers/dashboard.py` on memory and PostgreSQL; `test_dashboard_routes.py`;
  `test_openapi_dump.py` keeps `web/openapi.json` (the source of `schema.d.ts`) current.
- Web dependencies (exact versions in `web/package.json`, listed in overview.md): react,
  react-dom, react-router-dom (routing), openapi-fetch + openapi-typescript (client generated from
  OpenAPI, as the task asks), elkjs (DAG layout, as the task asks; lazy-loaded, 1.4 MB), Vite,
  Vitest + jsdom, Playwright + @axe-core/playwright (e2e and a11y), @types/node.
- Failure signatures: `questa.sim` results carry `summary.signature`; `signatureCluster.ts`
  ports `error_signature` (same test cases) for results that only have a first error line.
- Follow-ups: list/stats queries load the whole window and paginate in Python (as P2-05's
  routes do), so push limits/aggregation into SQL when domains reach many thousand builds; the
  log route ignores the `log` of an `infra_failed` event (`ebs logs` falls back to it); there is
  no "list channels of a release name" route, so the releases page probes `stable`/`nightly`
  plus channels the user names; provenance views stay within one domain (exports are the CLI's).
- Review (task-reviewer, test-critic) fixed: the log route opens files without following any
  symlink below the domain dir (a user-owned debug dir could point at another domain's files)
  and serves regular files only (no FIFO hang); unknown `pending` reasons count as `other`; the
  log viewer restarts when a retry truncates the live log; the stats route uses the injected
  clock; tests added for build/domain scoping of results and stats, per-build waits, unordered
  events, window clamps, the results route, outage 503, CAS-to-live fallback, headers.
  Left: e2e specs share one seeded server (`workers: 1`; a `/e2e/reset` would allow sharding);
  the FIFO test would hang rather than fail if `O_NONBLOCK` were dropped; `list_builds` loads
  the whole domain per page.
- Environment: `tests/integration/slurm/test_slurm_executor.py::test_fake_regression_2000`
  times out on this workstation on main as well (unrelated to this task).
