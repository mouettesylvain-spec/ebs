# Data model (PostgreSQL 17)

All tables live in schema `ebs`. Migrations are Alembic revisions under `src/ebs/meta/migrations/`;
each task that changes the schema adds exactly one revision and a test that upgrades from the
previous head and downgrades back (`tests/integration/meta/test_migrations.py`). The
`alembic_version` table lives in `public`, so `downgrade base` drops schema `ebs` entirely.
`ebs.meta.models` is the SQLAlchemy mirror of the schema; `test_no_model_drift` checks that the
migrations and the models agree (columns, types, defaults, indexes, keys, CHECK names). Constraint
names follow the convention `pk_<table>`, `fk_<table>_<cols>`, `uq_<table>_<cols>`,
`ix_<table>_<cols>`, `ck_<table>_<name>`.

Digests are stored as `text` in `algo:hex` form with a CHECK constraint
(`~ '^(sha256|blake3):[0-9a-f]{64}$'`). Timestamps are `timestamptz`. JSON payloads are `jsonb`
and always hold canonical models validated on write.

## Phase 0

```
domains(name PK, unix_group null, cas_root null, ttl_days int default 30, quota_bytes bigint null,
        high_water numeric default .85, low_water numeric default .75)
        -- rows are created implicitly by create_build/cache_put in P0; P2-01 fills group/root

builds(id bigserial PK, uuid uuid unique, domain FK, project text, plan_digest text,
       flow_repo text, flow_commit text, flow_dirty bool, user_name text, ci_job text null,
       cache_mode text check in ('off','read','write'),
       status text check in ('running','passed','failed','infra_failed','cancelled'),
       created_at, finished_at (null iff running), pinned bool default false)

actions(build_id FK, action_id text, step text, key text null, state text, attempts int,
        slurm_job_id text null, pending_reason text null, infra_reason text null,
        queued_at not null, started_at, finished_at, cached bool, result_key text null,
        result jsonb null,                                 -- last ResultManifest (0003, P0-15)
        rule text null, params jsonb object null,          -- rule kind, plan params and
        toolchain text null check digest,                  -- toolchain id (0010, P2-04)
        PRIMARY KEY(build_id, action_id))
        check state in (… , 'skipped')                     -- 'skipped' added by 0003 (P0-15)
        check result is null or (object with v = 1)        -- ck_actions_result_manifest
        -- 0003 downgrade: skipped -> cancelled, result dropped
        index (build_id, state), index (key)

action_cache(domain FK, key text, result jsonb, created_at, last_access, hits bigint default 0,
             PRIMARY KEY(domain, key))

blobs(domain FK, digest text, size bigint, kind text check in ('file','tree'), created_at,
      last_access, PRIMARY KEY(domain, digest))           -- GC bookkeeping; CAS is the source of truth for bytes

provenance_edges(domain FK, action_key text, direction text check in ('in','out'),
                 logical_path text, object_id text, content_digest text null,
                 PRIMARY KEY(domain, action_key, direction, logical_path))
                 index (domain, object_id), index (domain, content_digest)
                 -- written from P0 so P2 queries work retroactively; `out` rows hold the
                 -- passed-down id in object_id and the content digest in content_digest

toolchains(id text PK, name text, module text, version text, fingerprint text,
           env jsonb, install_roots jsonb, registered_at, registered_by, superseded_by text null,
           last_used_at null)                                  -- last_used_at: P1-07, migration 0004
toolchain_manifests(toolchain_id text PK FK toolchains ON DELETE CASCADE,
                    entries bytea)                             -- P1-07: zlib fingerprint lines

events(id bigint default nextval('event_id_seq'), build_id FK, ts, type text
       check ~ '^[a-z][a-z0-9_]*$', action_id text null, data jsonb object,
       stored_at)                                           -- stored_at: store clock (0008)
       PK (id, stored_at), index (build_id, id)             -- dashboard / status stream
       PARTITION BY RANGE (stored_at): events_yYYYYmMM per UTC month -- P2-05, migration 0008;
       ensure_events_partition(ts) creates one, retention drops whole partitions
```

## Phase 1

```
leases(id bigserial PK, domain FK, build_id FK builds ON DELETE CASCADE, expires_at, created_at)
       index (domain, expires_at)                          -- P1-09, revision 0006
lease_objects(lease_id FK leases ON DELETE CASCADE, digest text check digest,
              PRIMARY KEY(lease_id, digest))
pins(domain FK, build_id FK builds ON DELETE CASCADE, pinned_by, reason default '', created_at,
     PRIMARY KEY(domain, build_id))                        -- mirrored by builds.pinned
gc_runs(id bigserial PK, domain FK, started_at, finished_at, marked bigint, swept_rows bigint,
        swept_bytes bigint, dry_run bool, report jsonb object)   -- report = GcReport JSON
        index (domain, started_at)
tombstones(domain FK, digest check digest, deleted_rows_at, blob_delete_after,
           PRIMARY KEY(domain, digest))                    -- implements the grace period (I15)
api_tokens(id bigserial PK, principal text, kind text check in ('user','ci','service'),
           team text null, hash text UNIQUE check digest,   -- sha256 of the token, never the token
           created_at, expires_at null, revoked_at null)
           index (principal)                               -- P1-06, revision 0005
```

## Phase 2

```
releases(id bigserial PK, domain FK, name text check ~ RELEASE_NAME_PATTERN,
         version text check ~ VERSION_PATTERN (D4), build_id FK builds (no cascade),
         outputs jsonb object,                             -- {"<action>.<output>": {action_id, output, digest, id, type, size}}
         notes text default '', allow_failed text null,    -- the reason, when failed actions were released
         attestation text null, signature text null,       -- P3-03
         exported_from bigint FK releases null,            -- set on the copy made by an export
         created_at, created_by, yanked_at null, yanked_by null, yank_reason null,
         check yank fields all null or all set, UNIQUE(domain, name, version))
         -- 0007, P2-02; trigger releases_immutable: no DELETE, no UPDATE but setting the
         -- yank fields once (I19)
channel_history(id bigserial PK, domain FK, name, channel check ~ CHANNEL_PATTERN,
                release_id FK, moved_at, moved_by, gate_flow null, gate_build_id FK builds null,
                check gate fields both null or both set)
                index (domain, name, channel, id)          -- append-only (trigger)
channels(domain FK, name text, channel text, release_id FK, move_id FK channel_history,
         updated_at, updated_by, PRIMARY KEY(domain, name, channel))   -- the last move
exports(id bigserial PK, release_id FK, exported_release_id FK releases, to_domain FK,
        exported_at, exported_by) index (release_id)        -- audited cross-domain copy, append-only
release_imports(build_id FK builds, alias text check ~ IMPORT_ALIAS_PATTERN,
                release_id FK releases, PRIMARY KEY(build_id, alias))
                index (release_id)   -- 0009, P2-03: used-by queries; append-only (trigger)
```

## Phase 3

```
revalidations(id, release_id FK, started_at, finished_at, toolchain_overrides jsonb,
              verdict text, report_digest text)
archives(id, release_id FK, bundle_digest text, location text, created_at)
```

## Query patterns that must stay indexed

- `cache_get`: one `UPDATE … RETURNING result` by PK on `action_cache(domain, key)` that counts
  the hit and rewrites `last_access` only if it is over an hour old (write-amplification guard).
- `add_actions`: `COPY` into `actions` (20,000 rows well under 5 s).
- `ebs status`: `actions WHERE build_id = ? GROUP BY state`.
- `ebs why <digest>`: walk `provenance_edges` by `object_id` → `action_key` recursively
  (recursive CTE; depth limit 200). P2-04: one CTE per walk returns the walked keys with their
  edges (PK on `(domain, action_key, …)` and the `(domain, object_id)` index); a 20-level,
  50k-edge walk returns in under a second (`tests/integration/test_provenance_perf.py`).
- `ebs tests <release>`: forward walk from release output ids to actions whose step rule kind is
  `*.sim` (`actions.rule`, 0010). Releases holding a walked id are found by scanning the
  domain's `releases.outputs` (`jsonb_each`); fine while releases number in the thousands.
