# P1-06 — Metadata HTTP service and client

Status: todo · Phase: 1 · Depends on: P0-10 · Parallel-safe with: P0 tasks after P0-10 · Size: L

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
- [ ] `make check-all` passes

## Notes
D1 must be confirmed by the human before merge (MUNGE availability on login nodes).
