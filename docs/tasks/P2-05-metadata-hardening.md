# P2-05 — Metadata service hardening

Status: review · Phase: 2 · Depends on: P1-06 · Size: M

## Goal
The service is production-ready for ~100 users, CI and a dashboard: browser login, live event stream,
pagination, limits, observability.

## Read first
- docs/architecture.md § "Debugging, CLI, dashboard and CI" → Web dashboard (GitLab OIDC)

## Scope (files)
- modify `src/ebs/meta/service/*`; create `oidc.py`, `events_stream.py`, `limits.py`, `metrics.py`
- tests `tests/unit/meta/service/*`, `tests/integration/meta/test_sse.py`

## Requirements
- R1 GitLab OIDC login for the dashboard (authorization code + PKCE), sessions in signed cookies, CSRF protection
  on mutating browser routes; OIDC identity mapped to the Unix username (config claim).
- R2 Server-Sent Events endpoint per build and per domain (`/api/v1/builds/{id}/events`), resumable with
  `Last-Event-ID`, backed by the `events` table (LISTEN/NOTIFY for wake-ups).
- R3 Cursor pagination on all list routes; max page size enforced.
- R4 Rate limits per principal (token bucket, config) returning 429 with `Retry-After`; request body size limits.
- R5 Prometheus `/metrics` (request latency, cache hit ratio, DB pool usage); structured JSON logs with request ids.
- R6 `events` table partitioned by month with a retention job (config, default 180 days).

## Tests
| Test | Covers | Kind |
| --- | --- | --- |
| `test_oidc.py` (fake IdP) | R1 | unit |
| `tests/integration/meta/test_sse.py::test_resume_last_event_id` | R2 | integration |
| `test_pagination.py` | R3 | unit |
| `test_limits.py` (FakeClock) | R4 | unit |
| `test_metrics.py` | R5 | unit |
| `tests/integration/meta/test_event_partitions.py` | R6 | integration |

## Notes

- Scope: beyond `src/ebs/meta/service/*`, R2/R6 need the store side (`EventReader` in `api.py`,
  `pg.py`, `memory.py`, `_common.py`, `models.py`, migration 0008), R3/R4 the client's page
  following and 429 retry (`client.py`), and R6 the retention timer in `deploy/meta/systemd/`.
- Built on `task/P2-01-domains-authz` (stacked: P2-02 → P2-01 → P2-05): the new routes use the
  P2-01 domain checks and pagination covers the P2-02 release routes. Merge in that order.
- No new dependencies: the Prometheus text format, the HMAC session cookies and the OIDC client
  (httpx) are written here. The ID token is not signature-checked: it comes straight from the
  token endpoint over TLS, which OIDC Core § 3.1.3.7 accepts; iss, aud/azp, exp and nonce are.
- Defaults chosen (no open decision applied): 100 requests/s with bursts of 2000 per principal,
  16 MiB bodies, 1000 items per page, 180 days of events, streams closed after 300 s.
- R6: events are partitioned by `stored_at`, the store's clock, not by the event's `ts` (the
  client's clock), so a skewed or forged `ts` cannot create partitions (review finding).
- R2 ordering: `PgMetadataStore.emit` takes a per-domain advisory lock until commit, so event ids
  commit in order within a domain and `Last-Event-ID` never skips a row. It serializes a domain's
  emits (a few ms each); measure under a full regression.
- CONTRACT CHANGES (interfaces.md § 6.2): `Principal.via` gains `session`; the five list models
  gain `next`; `EventReader` protocol (`events_after`, `prune_events`); the client retries 429 for
  every call; migration 0008 adds `events.stored_at` and changes the primary key to (id, stored_at).

### Follow-ups

- Pagination cuts pages from the store's full answer (bounded per build/release/channel): push
  `limit`/after-key into the store queries if a list grows large.
- Rate-limit buckets and metrics are per worker process (`--workers 4`): a shared limiter
  (PostgreSQL or Redis) and multi-process metrics if per-worker numbers prove misleading.
- Sessions are stateless: a copied cookie stays valid until it expires or the keys rotate. A
  server-side session table would allow revoking one user's sessions.
- `/metrics` needs no credentials (like the probes): restrict it at the proxy or firewall.
- `/auth/*` is not rate-limited by the service (no principal yet): the proxy must, per address.
- An `ebs status -f` / dashboard client of the event stream (P2-06) is the first consumer.
