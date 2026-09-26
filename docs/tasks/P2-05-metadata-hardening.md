# P2-05 — Metadata service hardening

Status: todo · Phase: 2 · Depends on: P1-06 · Size: M

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
