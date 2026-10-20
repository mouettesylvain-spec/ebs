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
L1  config, domains, flow, cas, toolchain(fingerprint only)
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
`syrupy` (golden files), `import-linter`, `mypy`, `ruff`, `respx` (httpx mocks), `xmlschema`
(JUnit XSD validation, P2-07).
The runner entry point must import only stdlib + `pydantic` + `httpx` + `core`/`cas`/`runner`.

Documentation site (`site/`, D-01; Node, never shipped with the package): `astro`,
`@astrojs/starlight`, `animejs`, `sharp`; dev: `@astrojs/check`, `typescript`,
`starlight-links-validator`. Exact versions are pinned in `site/package.json` and its lockfile.

Dashboard (`web/`, P2-06; Node 24, exact versions in `web/package.json`): runtime `react`,
`react-dom`, `react-router-dom`, `openapi-fetch`, `elkjs` (DAG layout); dev `vite`,
`@vitejs/plugin-react`, `typescript`, `vitest`, `jsdom`, `openapi-typescript`,
`@playwright/test`, `@axe-core/playwright`, `@types/*`. No Python dependency is added.

## Site configuration (`ebs.toml`)

Looked up in order: `$EBS_CONFIG`, `./.ebs/config.toml`, `~/.config/ebs/config.toml`,
`/etc/ebs/config.toml`; later files are overridden by earlier ones. Keys (P0 set, extended later):

```toml
[cas]
root = "/cas"                 # per-domain subdirs: /cas/<domain>
copy_threshold = "256MiB"     # inputs above this are bind/symlinked read-only

[debug]                       # P1-04; no debug area (nor live log) when root is unset
root = "/debug"               # <root>/<domain> created by an admin, group <domain>, 2750
max_size = "2GiB"             # default cap for steps without debug.max_size

[metadata]
url = "https://ebs-meta.example"   # P1-06: the metadata service root (routes under /api/v1);
                                   # "postgresql+psycopg://…" opens the database directly (dev)
# auth = "munge"                   # HTTP only: "munge" (default) or "token"
# token_file = "~/.config/ebs/meta-token"   # mode 600; or $EBS_META_TOKEN
# ca_file = "/etc/pki/tls/certs/site-ca.pem"
# timeout_s = 10                   # per request
# retries = 4

# audience = "ebs-meta.example"   # HTTP + MUNGE: credential binding; default host[:port] of url

[meta_service]                     # service host only (P1-06)
munge = true                       # accept X-EBS-Munge, verified with `unmunge`
unmunge = "unmunge"
audience = "ebs-meta.example"      # required with munge: the host[:port] clients use
bearer = true                      # accept bearer tokens (api_tokens)
toolchain_admins = []              # users who may register toolchains (service tokens may too)
domains = ["cpu-nda"]              # P2-01: required; the served domains (each in [[domains]])
token_domains = {}                 # P2-01: bearer principal => domains, e.g. {"ci-cpu" = ["cpu-nda"]}
group_ttl_s = 60                   # P2-01: NSS group lookups cached this long

[[domains]]                        # P2-01 (D7): one per NDA scope; none = unrestricted (dev)
name = "cpu-nda"
unix_group = "cpu-nda"             # members see the domain; owns <cas>/<name> and <debug>/<name>
# cas_root = "/cas"                # default [cas].root; objects under <cas_root>/<name>/
# debug_root = "/debug"            # default [debug].root
# ttl_days = 30                    # shown by `ebs domain list` (GC reads the domains table)
# quota = "50T"

[scratch]
dir = "${TMPDIR}/ebs"

[runner]                       # P0-13
passthrough_env = ["LM_LICENSE_FILE", "*_LICENSE_FILE", "SLURM_*"]   # caller vars the tool sees
max_log = "2GiB"               # tool log cap; the tail is kept
kill_grace_s = 30              # SIGTERM -> SIGKILL on timeout
heartbeat_s = 60               # build-lease renewal while the tool runs (P1-04)

[stat_cache]
path = "/var/tmp/ebs-<uid>/statcache.sqlite"   # default; must be local disk, never NFS (P0-16)
racy_window_s = 3
audit_fraction = 0.01
untrusted_mounts = []

[rules]                        # read into ebs.rules.api.RuleSettings (config wiring: P0-16)
license_error_patterns = []    # regexes on the log tail => INFRA("license"); replaces the defaults

[gc]                           # P1-09: `ebs gc`, as the GC service account
domain = ""                    # default for --domain (one domain per run)
domains = []                   # P2-01: the other domains this account may collect (R5)
grace_hours = 24               # cache rows go first, their bytes this much later (I15)
tmp_max_age_hours = 24         # abandoned uploads in <cas root>/<domain>/tmp/
# TTL and quota are per domain, in the `domains` table (ttl_days, quota_bytes, *_water)

[retry]                        # P1-05: infrastructure failures (I12); test failures never retry
max_retries = 2                # retries per action, whatever the reason
backoff_s = 10                 # wait backoff_s * backoff_factor^(n-1), at most backoff_max_s
backoff_factor = 2
backoff_max_s = 300
oom_mem_factor = 1.5           # oom: next attempt asks for mem x this (rounded up to a MiB)
# max_mem = "256G"             # oom: never ask for more; at the cap the OOM is final
license_backoff_s = 60         # license: wait this x attempt number
retry_on_timeout = false       # timeout: default for steps without resources.retry_on_timeout
input_verification_retries = 1 # runner exit 76: retries, each on another node; then it fails
# A node with 2 node_fail in one build is excluded (--exclude) for the rest of that build.

[slurm]                        # P1
default_partition = ""
max_array_size = 1000
array_throttle = 200
max_pending_jobs = 2000        # in-flight jobs, array tasks counted individually (D5)
max_submits_per_min = 60      # sbatch calls in any 60 s window (P1-02)
poll_min_s = 5                 # also how long a batch refused for max_pending_jobs waits
poll_max_s = 60
# bin_dir = "/opt/slurm/bin"   # where sbatch & co live; default: PATH
license_server = "flexlm"      # P1-03: remote licenses are `<feature>@<server>`
license_fuzzy = false          # true: request `<feature>` only (LicenseParameters=RemoteFuzzyMatch)
os_constraint = ""             # node feature of the OS image, e.g. "rhel9" (--constraint)

[slurm.account_map]            # P1-03: flow project => SLURM account; empty: user's default
# rv32x-cpu = "cpu-team"

[slurm.qos]                    # P1-03: per build kind; empty: SLURM default (ci/release: default)
default = ""
ci = ""                        # `ebs build --ci`
release = ""

[release.gates]                # P2-02: channel (or "name:channel") => gate flow project
# stable = "cpu-gate"          # read by the CLI on a direct database, else by the service

[release.signing]              # P3-03: `ebs release attest` / `verify-signature`
# key = "~/.ssh/team-release"  # team SSH key (or its .pub, key in ssh-agent); --key overrides
# allowed_signers = "/etc/ebs/allowed_signers"   # ssh-keygen ALLOWED SIGNERS; --allowed-signers
# ssh_keygen = "ssh-keygen"    # OpenSSH >= 8.2; signatures use namespace "ebs-release"

[ci]                           # P2-07: `ebs build --ci`
# build_url = "https://ebs.example/builds/{uuid}"   # link in ebs-summary.md ({build}, {uuid})

[driver]                       # P2-08: the SLURM job of `ebs build --detach` (1 CPU)
partition = ""                 # empty: [slurm].default_partition
qos = ""                       # empty: the build kind's QOS ([slurm.qos])
time = "72h"                   # walltime: the longest build a detached driver may drive
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
