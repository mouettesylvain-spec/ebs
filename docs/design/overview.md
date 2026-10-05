# Design overview

This folder turns docs/architecture.md into buildable contracts. Read this file first, then only
the design file(s) your task points to.

| File | Contents |
| --- | --- |
| overview.md | Package map, layering rules, dependencies, site config, glossary |
| interfaces.md | Cross-package Python contracts (types, protocols, file formats) |
| invariants.md | Correctness rules; each one has a named test that enforces it |
| data-model.md | PostgreSQL schema, by phase |
| testing.md | Test layers, fakes, fixtures, markers, coverage gates |

## Package map

```
src/ebs/
  core/        digest, canonical JSON, tree manifests, errors, logging, clock, ids   (P0)
  config.py    site + user config loading (ebs.toml)                                 (P0)
  flow/        Pydantic flow model, YAML loader, JSON Schema, tables, interpolation   (P0)
  sources/     git blob ids, stat cache, glob resolution, source snapshot to CAS      (P0)
  toolchain/   install-tree fingerprint, env capture, modulefile loading, registry    (P0/P1)
  plan/        DAG build, action keys, plan.json, plan diff                           (P0)
  cas/         CAS protocol, fs backend, materialization; s3 backend later            (P0/P3)
  meta/        SQLAlchemy models, Alembic, MetadataStore protocol + PG + in-memory,
               FastAPI service, HTTP client                                           (P0/P1/P2)
  rules/       RulePlugin protocol, registry (entry points), shell, make, tcl,
               questa/ (compile, opt, sim), later vcs/, spyglass/ …                  (P0/P1/P4)
  exec/        Executor protocol, local executor, slurm/ (cli adapter, executor,
               arrays, licenses, fake)                                                (P0/P1)
  runner/      ebs-runner entry point: stage, env, run, hash+upload, debug, cleanup   (P0/P1)
  driver/      scheduler loop, cache policy, retries, events, build records           (P0/P1)
  gc/          access tracking, roots, leases, mark & sweep, quota                    (P1)
  release/     releases, channels, flow.lock, provenance queries, attestations,
               sign-off bundles, revalidation                                         (P2/P3)
  sandbox/     staging-only (v1), bubblewrap, apptainer fallback, strace discover     (P1/P3)
  cli/         Typer app; one module per command group                                (P0+)
web/           React + TypeScript dashboard                                           (P2)
deploy/        compose files, Ansible role, SLURM epilog, cron units                  (P0+)
tests/         unit/ integration/ grid/ vendor/ fakes/ fixtures/
examples/      demo flows on open-source tools (Verilator, small RISC-V core)
```

## Layering rules (enforced by `tests/unit/test_layering.py` with import-linter)

Lower layers never import higher ones.

```
L0  core
L1  config, flow, cas, toolchain(fingerprint only)
L2  sources, plan, rules, meta(store/models)
L3  exec, runner, sandbox, gc, release, toolchain(registry)
L4  driver, meta(service)
L5  cli
```

- Within L2, `plan` sits above `sources` and `rules` (the planner drives both); they never
  import `plan`.
- `runner` must not import `driver`, `plan.planner` or `meta.service` (it runs on compute nodes
  with a minimal dependency set: it reads an ActionSpec and talks to CAS + metadata client).
- `rules` plugins may import `core`, `flow`, `rules.api` only.
- Nothing imports `cli`.

## Dependencies (allowed list)

Runtime: `pydantic>=2.11`, `ruamel.yaml`, `typer`, `rich`, `sqlalchemy>=2`, `alembic`,
`psycopg[binary]>=3`, `fastapi`, `uvicorn`, `httpx`, `structlog`, `tomli-w`.
Optional extras: `blake3` (`[blake3]`), `boto3` (`[s3]`, P3).
Dev: `pytest`, `pytest-cov`, `hypothesis`, `jsonschema` (schema tests, P0-04), `pytest-timeout`, `pytest-xdist`, `testcontainers[postgres]`,
`syrupy` (golden files), `import-linter`, `mypy`, `ruff`, `respx` (httpx mocks).
The runner entry point must import only stdlib + `pydantic` + `httpx` + `core`/`cas`/`runner`.

## Site configuration (`ebs.toml`)

Looked up in order: `$EBS_CONFIG`, `./.ebs/config.toml`, `~/.config/ebs/config.toml`,
`/etc/ebs/config.toml`; later files are overridden by earlier ones. Keys (P0 set, extended later):

```toml
[cas]
root = "/cas"                 # per-domain subdirs: /cas/<domain>
copy_threshold = "256MiB"     # inputs above this are bind/symlinked read-only

[debug]
root = "/debug"

[metadata]
url = "postgresql+psycopg://…"     # P0 direct mode; P1+: "https://ebs-meta.example/api"

[scratch]
dir = "${TMPDIR}/ebs"

[stat_cache]
racy_window_s = 3
audit_fraction = 0.01
untrusted_mounts = []

[rules]                        # read into ebs.rules.api.RuleSettings (config wiring: P0-16)
license_error_patterns = []    # regexes on the log tail => INFRA("license"); replaces the defaults

[slurm]                        # P1
default_partition = ""
max_array_size = 1000
array_throttle = 200
poll_min_s = 5
poll_max_s = 60
```

## Glossary

- **Flow**: `flow.yaml` + tables + `flow.lock` in a team repo.
- **Step**: a named entry in `steps:`; with a matrix it expands to many actions.
- **Action**: one executable unit (step × matrix row). Identified by an **action id**
  (human-readable, stable: `sim[test=smoke,seed=17]`) and an **action key** (digest).
- **Input id**: the digest that represents one input in an action key (content digest, or a
  derived id for nondeterministic outputs — see interfaces.md).
- **Domain**: NDA scope. Owns a CAS root, a debug root, a cache namespace and a Unix group.
- **Toolchain id**: digest of module name + version + install-tree fingerprint + captured env.
- **Build**: one immutable run of a plan. **Release**: named, versioned selection of build
  outputs. **Channel**: movable pointer to a release.
