# P1-06 — Metadata HTTP service and client

Status: review · Phase: 1 · Depends on: P0-10 · Parallel-safe with: P0 tasks after P0-10 · Size: L

## Goal
Runners on compute nodes and CLIs on login nodes talk to shared state through a small FastAPI service
(no DB credentials on nodes); the HTTP client is a drop-in `MetadataStore`.

## Read first
- docs/architecture.md § "System architecture" (Metadata service row); docs/design/interfaces.md §6
- open decision D1 (authentication)

## Scope (files)
- create `src/ebs/meta/service/app.py`, `routes/*.py`, `auth.py`, `deps.py`; `src/ebs/meta/client.py`
  (`HttpMetadataStore`, httpx, retries)
- create `deploy/meta/compose.yaml` service entry + systemd unit example
- tests: `tests/contract/test_metadata_store.py[http]` (service running in-process with ASGI transport over the
  PG store), `tests/unit/meta/service/test_auth.py`, `test_routes.py`

## Requirements
- R1 `HttpMetadataStore` passes the full MetadataStore contract suite.
- R2 REST API under `/api/v1`, OpenAPI published; request/response bodies are the Pydantic models from
  `ebs.meta.api`; batch endpoints for `add_actions`, `touch`, `cache_get_many` (driver uses batches of ≤ 500).
- R3 D1 auth: a `MUNGE` credential header (`X-EBS-Munge`) verified via `unmunge` subprocess (uid/gid ⇒ username
  through NSS), or `Authorization: Bearer` tokens (hashed in DB) for CI/services. Auth backend is pluggable;
  tests use a fake munge verifier. Unauthenticated requests get 401.
- R4 Principals may only write builds they own (or CI tokens for their team) — ownership check on every
  mutating route; domain membership checks arrive in P2-01 (leave a single `authorize()` hook called everywhere).
- R5 Client retries idempotent calls on connect errors/5xx with backoff; `cache_put` and `record_result` are
  idempotent by design (safe to retry); requests time out after 10 s.
- R6 Service health `/healthz` (process) and `/readyz` (DB reachable, migrations at head).
- R7 Runner uses the HTTP client when `[metadata].url` is http(s); direct PG only for dev.

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `tests/contract/test_metadata_store.py[http]` | R1 | contract |
| `test_routes.py::test_openapi_models`, `::test_batch_limits` | R2 | unit |
| `test_auth.py::test_munge_ok`, `::test_munge_replay_or_expired_rejected`, `::test_bearer`, `::test_401` | R3 | unit |
| `test_routes.py::test_ownership_enforced[...]` | R4 | unit |
| `tests/unit/meta/test_client.py::test_retry_idempotent` (respx) | R5 | unit |
| `test_routes.py::test_health_ready` | R6 | unit |
| `tests/unit/runner/test_main.py::test_selects_http_client` | R7 | unit |

## Done when
- [x] `make check-all` passes

## Notes
D1 must be confirmed by the human before merge (MUNGE availability on login nodes).

Implementation notes (P1-06):
- D1 default implemented as specified: `X-EBS-Munge` verified by `unmunge --numeric` (uid → NSS
  name), bearer tokens hashed in `ebs.api_tokens` (revision 0005; the table moved from phase 2 to
  phase 1 in data-model.md, with an extra `team` column). Both schemes sit behind the
  `Authenticator` protocol and are switchable in `[meta_service]` (`munge`, `bearer`).
- R4 interpretation: "CI tokens for their team" = a `ci` token whose `team` equals the build's
  `project` (the flow's project is the team's SLURM account). `service` tokens never write builds.
- Dependencies added (all on the overview.md allowed list): runtime `fastapi`, `uvicorn`, `httpx`;
  dev `respx`. FastAPI's own body parsing is bypassed (`deps.body`): it validates in Python mode,
  where the strict `ebs.meta.api` models refuse ISO datetimes and UUID strings.
- `add_actions` over HTTP is atomic per batch of 500, not for the whole call (documented in
  interfaces.md § 6.1); duplicates are refused client-side before anything is sent.
- Local integration runs used PostgreSQL 16 (no Docker daemon in the session); the target is 17.

Follow-ups:
- Add `cache_get_many` to the `MetadataStore` protocol (one query in PG) and make the driver use
  it; today it exists only on `HttpMetadataStore` and the service loops over `cache_get`.
- `cache_replace_failed` is not retried after a 5xx (its False answer would be ambiguous); an
  idempotency key would make it safely retryable.
- Starlette 1.x deprecates `httpx` in its TestClient (in favour of `httpx2`); the tests silence the
  warning. Revisit when httpx2 is stable.
- P2-01: put domain membership into `authorize()`; P2-05: OIDC for the dashboard.
- Before D1 is confirmed: bind MUNGE credentials to this service (munge a payload such as
  `ebs-meta:<METHOD>:<path>` and check it server-side). Today a credential seen in other
  plaintext MUNGE traffic is accepted until its TTL ends, because this host's replay cache has
  not seen it. Payloads are refused for now (`LENGTH` must be 0).
- The `http` contract backend uses Starlette's TestClient, which decodes paths a second time; an
  action id containing a literal `%2F` reaches the store as `/` there (uvicorn decodes once).
  Switch to an ASGI transport when a sync one is available in httpx.
