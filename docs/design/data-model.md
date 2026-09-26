# Data model (PostgreSQL 17)

All tables live in schema `ebs`. Migrations are Alembic revisions under `src/ebs/meta/migrations/`;
each task that changes the schema adds exactly one revision and a test that upgrades from the
previous head and downgrades back (`tests/integration/meta/test_migrations.py`).

Digests are stored as `text` in `algo:hex` form with a CHECK constraint
(`~ '^(sha256|blake3):[0-9a-f]{64}$'`). Timestamps are `timestamptz`. JSON payloads are `jsonb`
and always hold canonical models validated on write.

## Phase 0

```
domains(name PK, unix_group, cas_root, ttl_days int default 30, quota_bytes bigint null,
        high_water numeric default .85, low_water numeric default .75)

builds(id bigserial PK, uuid uuid unique, domain FK, project text, plan_digest text,
       flow_repo text, flow_commit text, flow_dirty bool, user_name text, ci_job text null,
       cache_mode text check in ('off','read','write'), status text, created_at, finished_at,
       pinned bool default false)

actions(build_id FK, action_id text, step text, key text null, state text, attempts int,
        slurm_job_id text null, pending_reason text null, infra_reason text null,
        queued_at, started_at, finished_at, cached bool, result_key text null,
        PRIMARY KEY(build_id, action_id))
        index (build_id, state), index (key)

action_cache(domain FK, key text, result jsonb, created_at, last_access, hits bigint default 0,
             PRIMARY KEY(domain, key))

blobs(domain FK, digest text, size bigint, kind text check in ('file','tree'), created_at,
      last_access, PRIMARY KEY(domain, digest))           -- GC bookkeeping; CAS is the source of truth for bytes

provenance_edges(domain FK, action_key text, direction text check in ('in','out'),
                 logical_path text, object_id text, content_digest text null,
                 PRIMARY KEY(domain, action_key, direction, logical_path))
                 index (domain, object_id)                 -- written from P0 so P2 queries work retroactively

toolchains(id text PK, name text, module text, version text, fingerprint text,
           env jsonb, install_roots jsonb, registered_at, registered_by, superseded_by text null)

events(id bigserial PK, build_id FK, ts, type text, action_id text null, data jsonb)
       index (build_id, id)                                -- dashboard / status stream; partition by month in P2
```

## Phase 1

```
leases(id bigserial PK, domain FK, build_id FK, expires_at, created_at)
lease_objects(lease_id FK, digest text, PRIMARY KEY(lease_id, digest))
pins(domain FK, build_id FK, pinned_by, reason, created_at, PRIMARY KEY(domain, build_id))
gc_runs(id, domain, started_at, finished_at, marked bigint, swept_rows bigint,
        swept_bytes bigint, dry_run bool, report jsonb)
tombstones(domain, digest, deleted_rows_at, blob_delete_after)   -- implements the grace period (I15)
```

## Phase 2

```
releases(id bigserial PK, domain FK, name text, version text, build_id FK,
         outputs jsonb,                                    -- {name: {digest, id, type, size}}
         notes text, attestation text null, signature text null,
         created_at, created_by, yanked_at null, yanked_by null, yank_reason null,
         UNIQUE(domain, name, version))
channels(domain FK, name text, channel text, release_id FK, updated_at, updated_by,
         PRIMARY KEY(domain, name, channel))
channel_history(id, domain, name, channel, release_id, moved_at, moved_by, gate_build_id null)
release_imports(consumer_build_id FK, release_id FK, alias text)   -- used-by queries
exports(id, release_id FK, to_domain FK, exported_at, exported_by)  -- audited cross-domain copy
api_tokens(id, principal text, kind text check in ('user','ci','service'), hash text,
           created_at, expires_at, revoked_at)
```

## Phase 3

```
revalidations(id, release_id FK, started_at, finished_at, toolchain_overrides jsonb,
              verdict text, report_digest text)
archives(id, release_id FK, bundle_digest text, location text, created_at)
```

## Query patterns that must stay indexed

- `cache_get`: PK lookup on `action_cache(domain, key)` + `UPDATE last_access` batched
  (touch at most once per key per hour to limit write amplification).
- `ebs status`: `actions WHERE build_id = ? GROUP BY state`.
- `ebs why <digest>`: walk `provenance_edges` by `object_id` → `action_key` recursively
  (recursive CTE; depth limit 200).
- `ebs tests <release>`: forward walk from release output ids to actions whose step rule kind is
  `*.sim`.
