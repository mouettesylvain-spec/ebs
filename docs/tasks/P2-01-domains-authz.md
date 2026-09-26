# P2-01 — Domains and authorization

Status: todo · Phase: 2 · Depends on: P1-06 · Size: L

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
