# P0-10 — Metadata store: schema, PostgreSQL, in-memory fake, contract suite

Status: todo · Phase: 0 · Depends on: P0-02 · Parallel-safe with: P0-03, P0-05, P0-07 · Size: L

## Goal
Persistent shared state (builds, actions, action cache, provenance edges, events) behind one
`MetadataStore` protocol with a PostgreSQL implementation and an in-memory implementation that are
provably equivalent.

## Read first
- docs/design/data-model.md "Phase 0"; docs/design/interfaces.md §6
- docs/architecture.md § "Hashing, action keys and caching" → "Cache semantics"

## Scope (files)
- create `src/ebs/meta/api.py` (Protocol + Pydantic models: ResultManifest, BuildCreate, ActionRow, …),
  `src/ebs/meta/models.py` (SQLAlchemy 2 typed ORM/Core), `src/ebs/meta/pg.py`, `src/ebs/meta/memory.py`,
  `src/ebs/meta/migrations/` (Alembic env + revision `0001_initial`)
- create `tests/contract/test_metadata_store.py` (parametrized `memory` | `pg`),
  `tests/integration/meta/test_migrations.py`, `tests/unit/meta/test_models.py`

## Out of scope
HTTP service/client (P1-06), leases/GC tables (P1-09), releases (P2-02), authz (P2-01).

## Contract
interfaces.md §6 (P0 methods: cache_get, cache_put, create_build, add_actions, set_action_state,
record_result, finish_build, get_build, list_actions, touch, emit).

## Requirements
- R1 Alembic `upgrade head` from empty creates the P0 schema; `downgrade base` removes it; migration test
  compares the reflected schema with the models (no drift).
- R2 `cache_put` is insert-if-absent (`ON CONFLICT DO NOTHING`), returns whether it inserted; concurrent
  puts of the same key from 10 threads produce exactly one row and one `True`.
- R3 `cache_get` returns a validated `ResultManifest`, increments `hits`, and updates `last_access` at most
  once per hour per key (write-amplification guard; FakeClock).
- R4 I13: entries are scoped by domain.
- R5 `record_result` writes the action row fields and provenance edges (`in` for each input id, `out` for
  each output content digest and passed-down id) in one transaction.
- R6 `add_actions` handles 20,000 rows in < 5 s against PG (COPY or executemany batches).
- R7 State transitions are validated (`queued→pending→running→done|failed|infra_failed|cached|cancelled`,
  retries go back to `queued`); illegal transitions raise `MetadataError`.
- R8 All digests and JSON payloads are validated on write; DB CHECK constraints mirror the rules.
- R9 PG connection settings come from config (`[metadata].url`), use a pool, and every public method is a
  single short transaction (no transaction spans driver waits).

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `test_migrations.py::test_upgrade_downgrade`, `::test_no_model_drift` | R1 | integration |
| `test_metadata_store.py::test_cache_put_idempotent`, `::test_cache_put_concurrent` | R2 | contract |
| `test_metadata_store.py::test_cache_get_touch_throttled` | R3 | contract |
| `test_metadata_store.py::test_domain_scoping` | R4 (I13) | contract |
| `test_metadata_store.py::test_record_result_edges_atomic` | R5 | contract |
| `test_metadata_store.py::test_add_actions_bulk` (pg only, mark `slow`) | R6 | integration |
| `test_metadata_store.py::test_state_machine[...]` | R7 | contract |
| `test_models.py::test_validation_on_write[...]` | R8 | unit |
| `test_metadata_store.py::test_short_transactions` (assert no idle-in-transaction via pg_stat_activity) | R9 | integration |

## Done when
- [ ] `make check-all` passes with both implementations; contract suite is reusable for `HttpMetadataStore` (P1-06)
