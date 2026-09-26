# P2-04 — Provenance queries: `why`, `used-by`, `tests`

Status: todo · Phase: 2 · Depends on: P2-02 · Size: M

## Goal
Answer "where did this artifact come from", "who consumed this release" and "which tests ran on the RTL
behind this release" from the provenance edges recorded since phase 0.

## Read first
- docs/architecture.md § "Versioning…" → Provenance bullets; data-model "Query patterns"

## Scope (files)
- create `src/ebs/release/provenance.py` (recursive CTE queries + in-memory equivalent), `src/ebs/cli/why.py`,
  `used_by.py`, `tests_cmd.py`; service routes
- tests `tests/contract/test_provenance.py` (memory + pg), `tests/unit/cli/test_provenance_cli.py`

## Requirements
- R1 `ebs why <digest|release output|action>` prints the upstream chain: actions (step, params, toolchain),
  source commits, imported releases, down to sources; `--depth`, `--json`, `--dot` (Graphviz).
- R2 `ebs used-by <release>` lists downstream builds and releases (transitively, through release imports).
- R3 `ebs tests <release>` lists every action with a `*.sim` rule whose inputs derive from the release's outputs:
  test, seed, status, toolchain, build, date; `--latest` keeps the newest per (test, seed); JUnit export option.
- R4 Queries respect domain authz (exported releases link across domains only through the audited export row).
- R5 Performance: `why` over a 20-level, 50k-edge synthetic graph returns in < 1 s on PG.

## Tests
| Test | Covers | Kind |
| --- | --- | --- |
| `test_provenance.py::test_why_chain`, CLI golden output | R1 | contract/unit |
| `test_provenance.py::test_used_by_transitive` | R2 | contract |
| `test_provenance.py::test_tests_for_release`, `::test_latest_filter` | R3 | contract |
| `tests/integration/test_authz_matrix.py` (provenance routes added) | R4 | integration |
| `tests/integration/test_provenance_perf.py` (mark `slow`) | R5 | integration |
