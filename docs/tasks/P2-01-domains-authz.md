# P2-01 — Domains and authorization

Status: review · Phase: 2 · Depends on: P1-06 · Size: L

## Goal
NDA boundaries enforced end to end: each domain has its own CAS root, debug area and cache namespace;
users only see domains whose Unix group they belong to, through every interface.

## Read first
- docs/architecture.md § "Security and confidentiality" (all bullets); invariants I13, I20; open decision D7

## Scope (files)
- create `src/ebs/meta/service/authz.py` (group lookup via NSS/SSSD with TTL cache, injectable), `src/ebs/domains.py`
  (config `[[domains]]`: name, unix_group, cas_root, debug_root, ttl, quota), `src/ebs/cli/domain.py`
  (`ebs domain list|check`), `deploy/admin/create_domain.sh` (dirs, group, mode 2750)
- `ebs release export --to DOMAIN` is in P2-02 (uses this task's checks)
- tests `tests/unit/meta/service/test_authz.py`, `tests/integration/test_authz_matrix.py`

## Requirements
- R1 Every service route resolves the target domain and calls `authorize(principal, domain, action)`; a
  route-coverage test enumerates the OpenAPI routes and fails if any route lacks an authz dependency.
- R2 I20 matrix test: for users in {A}, {B}, {A,B}, none, and CI tokens scoped to A, every read/write route
  returns 403/404 (not 200) outside membership; 404 vs 403 policy: cache lookups return "miss" rather than
  revealing existence.
- R3 Flows declare `domain:`; the CLI refuses to plan a flow whose domain the user is not in, before any hashing.
- R4 `create_domain.sh` is idempotent and sets group ownership + setgid; `ebs domain check` verifies modes,
  groups and free space for each configured domain.
- R5 Service accounts (GC, metadata) have explicitly listed domains; the metadata DB never stores file
  contents (assert by schema review test: no `bytea`/large text columns except logs digests).

## Tests
| Test | Covers | Kind |
| --- | --- | --- |
| `test_authz.py::test_every_route_authorized` | R1 | unit |
| `tests/integration/test_authz_matrix.py` | R2 (I20) | integration |
| `tests/unit/cli/test_domain_guard.py` | R3 | unit |
| `tests/unit/deploy/test_create_domain.py` (runs in a user namespace or with a fake chgrp) | R4 | unit |
| `tests/unit/meta/test_schema_no_content.py` | R5 | unit |

## Notes
- From P2-02: the `release_write` / `release_export` ops of the `authorize` hook only check the
  release's build owner today. `release_export` must also check membership in both the source
  domain and `to_domain` on the server (the CLI's `UnixGroupAccess` check is client side, and the
  route only records the export); consider also requiring every output digest to be registered
  in `to_domain`'s `blobs` before `record_export` succeeds.
- Done (P2-01): `[[domains]]` (keyed by name by the config loader) drive per-domain CAS and
  debug roots in the CLI, runner and SLURM executor. The service's `DomainPolicy` requires
  `[meta_service].domains`; bearer principals use `[meta_service].token_domains`, else NSS.
  Non-members get 403 (domain in the path or body), 404 for builds/releases by id (unknown
  ids are 404 too, CONTRACT CHANGE), and cache reads are misses. Exports check both domains.
  The CLI guard (R3) is advisory: user config can override `[[domains]]`; the filesystem modes
  and the service enforce membership.
- Follow-ups: `FsCAS` creates shard directories (`blobs/sha256/ab/cd`) with the user's umask
  (often 2755), so a second member cannot add shards: create them 2770 explicitly. The
  fail-closed middleware stops a response, not the writes of a route that forgot `check()`.
  `ResultManifest.summary.first_error` (a log line) is the one place log text reaches the
  database; decide whether NDA sites must redact it. Release reads by name (`list_releases`,
  channels) use the domain in the request; `ebs fetch`/`logs` rely on file permissions only.
