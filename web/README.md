# EBS dashboard (`web/`)

A read-mostly web UI for the metadata service (task P2-06): builds and their live DAG, regression
grids with clustered failure signatures, action logs, cache and license-wait metrics, releases,
channels and provenance. React + TypeScript, built with Vite, served by the metadata service at
`/ui/` (`[meta_service].dashboard_dir = "/path/to/web/dist"`). The API and the session cookie are
then same-origin; login is the service's OIDC flow (`/auth/login`, P2-05).

## Commands

| Command | What |
| --- | --- |
| `npm ci` | install (Node 24) |
| `npm run dev` | dev server on :5173, proxying `/api` and `/auth` to `EBS_META_URL` (default `http://localhost:8000`) |
| `npm run build` | typecheck + production build to `dist/` |
| `npm test` | Vitest unit tests (`src/**/*.test.ts`) |
| `npm run e2e` | Playwright + axe-core against `scripts/dashboard_e2e_server.py` (seeded, in-memory) |
| `npm run gen:api` | regenerate `openapi.json` and `src/api/schema.d.ts` from the service |
| `make check-web` (repo root) | all of the above, as CI's `web` job |

`npx playwright install chromium` once before `npm run e2e` (`--with-deps` where you may install
system packages).

## Conventions

- **API types are generated, never hand-written.** `src/api/schema.d.ts` comes from the
  service's OpenAPI document (`scripts/dump_openapi.py`); `src/api/types.ts` only names its
  schemas. A route or model change in `ebs.meta` regenerates both in the same commit
  (`tests/unit/meta/service/test_openapi_dump.py` and `npm run check:api` fail otherwise).
- **The service authorizes; the dashboard never filters.** Every list is what the API returned
  for the caller (R7). No client-side hiding of other domains' data, no "admin mode".
- **Pure logic lives in `src/lib/`** (grouping, clustering, savings, log lines) and has Vitest
  tests; pages in `src/pages/` fetch and render. Keep transforms free of React.
- **State is never colour alone** (R6): every state has a glyph and a text label
  (`src/lib/states.ts`); colours are Okabe–Ito hues, as CSS tokens in `src/styles.css` with
  light and dark values. New pages get a row in `e2e/a11y.spec.ts` (axe, WCAG 2.1 AA, both themes).
- **Keyboard first:** interactive things are buttons or links; custom widgets (the DAG) handle
  Enter/Space and arrow keys. Accessible names start with the visible text.
- **Routes are hash routes** (`/ui/#/d/<domain>/builds/<id>`) so the static server needs no
  fallback; the domain is always in the route, filters are in the query string (linkable).
- **Large data streams in:** paginated lists follow `next`; logs are fetched in byte ranges
  (`GET /builds/{b}/actions/{a}/log?offset=`) and drawn virtualized; live views use the SSE
  streams (`src/lib/sse.ts`) and redraw at most five times a second.
- **Dependencies** are pinned exactly in `package.json`; adding one needs a line in the task's
  notes saying why. Runtime: react, react-dom, react-router-dom, openapi-fetch, elkjs (DAG
  layout, loaded lazily).
- Test data is synthetic: no real designs, logs, hosts or license servers.
