# P2-04 — Provenance queries: `why`, `used-by`, `tests`

Status: review · Phase: 2 · Depends on: P2-02 · Size: M

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

## Notes
- Started while P2-01 and P2-02 were merged but still `review` (the user chose to proceed).
- Rule kind, params and toolchain are stored on `actions` (revision 0008, `ActionRow.rule/params/
  toolchain`, set by the driver): the service has no CAS access to read plans, and `ebs tests`
  must select `*.sim` rules from the database. Rows written before 0008 hold NULL, so they show
  no params and are not listed by `ebs tests`. Revision 0008 may collide with a parallel task's
  revision number (P2-03 adds `release_imports`): the second to merge renumbers and rebases its
  `down_revision`.
- `used-by` derives downstream builds from `in` provenance edges, which exist since P0 and count
  cache hits. When P2-03 lands `release_imports`, add builds that imported a release without
  consuming any of its outputs (union with the edge walk).
- Cross-domain links follow export audit rows only: upstream, an id with no producer that is an
  output of an exported copy continues in the source release's domain; downstream, the exports
  of the release and of every release found downstream. Over HTTP every walk is a
  domain-scoped route (`read`), and a release the caller cannot read is reported as hidden
  (for `why`, without even the source domain).
- Store walks are one recursive CTE (level = shortest distance, `UNION` dedups (key, level)).
  R5: ~0.2 s for the CTE on 50k edges; most of the rest is building 37k pydantic edge models.
  The perf test takes the best of 5 runs because the CI and dev hosts are shared, and ANALYZEs
  after its bulk load (deployed tables have autovacuum statistics; without them the first walk
  took 28 s).
- Follow-ups: releases holding walked ids are found by scanning the domain's
  `releases.outputs`; index the output ids (or a `release_outputs` table) if releases reach
  tens of thousands. A walk over HTTP starts from at most `PROVENANCE_START_MAX` (10,000) ids;
  graphs are not paginated (P2-05/P2-06 may want a summary form for the dashboard).
- Review (task-reviewer, test-critic): `ebs tests` now reports hidden exports and truncation
  (`SimRuns`); `--json`/`--dot` are exclusive; DOT node ids carry the full digest; the PG walk
  returns edges and actions in the graph's order (no Python re-sort). Mutation testing found
  the domain filters of the PG walk untested (fixture keys were unique per build): added a
  scenario where one action key exists in two domains, plus tests for exports of downstream
  releases, exports without consumers, content-digest matching only at the start, truncation
  (incl. a diamond), local producers winning over export links, service errors (5xx) never
  read as "hidden", `--latest` ties, JUnit skips and the HTTP start limit.
- Open: a key reachable by paths of different lengths is expanded once per distinct length in
  the CTE (bounded by the depth); fine for flows, but a skip-level DAG of depth 200 could be
  slow. R5 measured 0.73 s (best of 3; the test takes the best of 5) at load 6.5 on a 12-core dev host (CTE ~0.2 s).
