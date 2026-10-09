# P0-08 — Planner, action keys, plan.json, plan diff

Status: done · Phase: 0 · Depends on: P0-05, P0-06, P0-07, P0-12 · Parallel-safe with: P0-14 · Size: L

## Goal
The heart of the system: turn a loaded flow into a static DAG of fully explicit actions with
action keys, serialize it as `plan.json`, and explain why any action will rerun.

## Read first
- docs/architecture.md § "Hashing, action keys and caching" (whole section) and § "Flow description format"
- docs/design/interfaces.md §4 (normative), §7 (RulePlugin.expand)
- docs/design/invariants.md I3, I4, I14

## Scope (files)
- create `src/ebs/plan/graph.py` (DAG, topo sort, cycle detection), `keys.py`, `planner.py`,
  `planfile.py` (plan.json encode/decode), `diff.py`
- create `tests/unit/plan/test_graph.py`, `test_keys.py`, `test_key_sensitivity.py`, `test_golden_keys.py`,
  `test_nondeterministic.py`, `test_planner.py`, `test_planfile.py`, `test_diff.py`,
  `tests/fixtures/golden_keys/*.json`, `tests/golden/plans/` (syrupy snapshots of example plans),
  `scripts/regen_golden_keys.py` (refuses to run unless `KEY_SCHEMA_VERSION` differs from the fixtures' version)

## Out of scope
Cache lookups (driver, P0-15). Imports (`${imports.*}` → P2-03: raise `PlanError("imports require
flow.lock support")` for now). Toolchain registry (use `ToolchainResolver` from P0-07).

## Contract
interfaces.md §4 exactly: `ActionSpec`, `InputRef`, `KEY_SCHEMA_VERSION`, `action_key_document`,
`compute_key`, `nondeterministic_output_id`, `Planner.plan`, `Planner.refine`, `diff_plans`, plan.json v1.

## Requirements
- R1 Expansion: each step × matrix row becomes one ActionSpec via the step's RulePlugin; `${steps.S.outputs.O}`
  creates an edge to the single instance of S (error if S has a matrix and no selector), `[*]` to all
  instances (fan-in; logical paths `<O>/<instance_id>/…` for tree inputs), `[k=v]` to the selected instance.
- R2 Cycles are reported with the full cycle path; references to unknown steps/outputs have "did you mean".
- R3 `targets` restricts the plan to the listed steps plus their transitive dependencies.
- R4 Logical paths: all argv paths and input/output paths are relative POSIX paths inside the scratch
  root; absolute paths into user homes or project NFS are rejected (architecture "Sandboxing phases" v1).
- R5 Key document contains exactly the fields of interfaces.md §4; I3 holds (Hypothesis: mutate each
  included field ⇒ key changes; mutate each excluded field ⇒ key unchanged).
- R6 Input ids: source inputs use snapshot digests; outputs of producers with `deterministic: false` use
  `nondeterministic_output_id`; deterministic outputs of producers not yet run leave `id=None` and the
  consumer's `key=None` (I14).
- R7 `refine(plan, produced)` fills ids/keys that become computable and is idempotent; after refining with
  all producers, every key is set.
- R8 plan.json is canonical JSON, round-trips losslessly (`decode(encode(p)) == p`), and its digest is
  stable across Python versions and runs (golden snapshot of the architecture example flow with fake tables).
- R9 Golden keys: `tests/fixtures/golden_keys/` holds ≥ 5 action specs and their expected keys; the test
  fails with instructions to bump `KEY_SCHEMA_VERSION` if a key changes (I4).
- R10 `diff_plans` reports per action: added / removed / unchanged / changed with the list of changed key
  fields (e.g. `inputs["src/alu.sv"]`, `params.seed`, `toolchain`); unknown keys are reported as
  "depends on <producer action_id>".
- R11 Planning 1 compile step × 50 libs + 1 elab + 1 sim step × 2,000 rows takes < 2 s and < 300 MB
  (excluding source hashing; use a fake snapshotter).

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `test_planner.py::test_matrix_expansion`, `::test_fan_in_star`, `::test_selector`, `::test_matrix_ref_without_selector_error` | R1 | unit |
| `test_graph.py::test_cycle_reported`, `test_planner.py::test_unknown_ref_suggestion` | R2 | unit |
| `test_planner.py::test_targets_closure` | R3 | unit |
| `test_planner.py::test_absolute_path_rejected[...]` | R4 | unit |
| `test_key_sensitivity.py::test_included_fields_change_key`, `::test_excluded_fields_do_not` | R5 (I3) | property |
| `test_nondeterministic.py::test_nd_id_stable_across_producer_runs`, `::test_deterministic_pending` | R6 (I14) | unit |
| `test_planner.py::test_refine_fills_keys`, `::test_refine_idempotent` | R7 | unit/property |
| `test_planfile.py::test_roundtrip`, golden snapshot `tests/golden/plans/architecture_example.json` | R8 | unit/golden |
| `test_golden_keys.py::test_keys_stable` | R9 (I4) | unit |
| `test_diff.py::test_changed_fields[...]`, `::test_pending_reason` | R10 | unit |
| `test_planner.py::test_large_plan_perf` (mark `slow`) | R11 | unit |

## Done when
- [x] `make check` passes; `ebs.plan` ≥ 95 % line coverage
- [x] invariants I3/I4/I14 reference the real test names

## Notes
Keep planning pure: all I/O goes through injected `CAS`, `SourceSnapshotter`, `ToolchainResolver`,
`RuleRegistry`. Use `FakeSnapshotter` (returns digests from a dict) in unit tests.

Implementation notes (P0-08):
- CONTRACT CHANGES (interfaces.md § 4, § 12): `Planner.plan(flow, *, base, info=None, targets=(),
  rehash=False)` (the flow directory is needed and `Flow` carries no path); `ActionSpec.runtime_env`
  (non-key); `ActionSpec.params` keeps bool/tuple values; `Snapshotter` protocol with the new public
  `SourceSnapshotter.rehash` property; supporting types live in `ebs.plan.types`.
- Layering: `plan` now sits above `sources : rules` in `.importlinter` (they never import plan).
- Source inputs are one file `InputRef` per matched file (fine-grained diffs such as
  `inputs["src/alu.sv"]`); output references are staged at `<output>/<instance id>[/<file name>]`.
- Golden plan snapshot uses a small golden-file helper (`EBS_UPDATE_GOLDEN=1`) instead of syrupy,
  which is allowed but not in uv.lock; adding it needs network access.

Follow-ups / open questions:
- Executable bit of source files: per-file `InputRef`s carry no mode, so a chmod-only change does
  not change the key and the runner (P0-13) cannot restore `+x` on staged scripts. Needs an
  `executable` flag in `InputRef` and the key document (KEY_SCHEMA_VERSION bump) — decide before P0-13.
- `OutputSpec.optional` is not in the key (per contract): flipping an output from optional to
  required can hit an old cache entry that lacks it. Either add it to the key (bump) or have the
  driver (P0-15) reject cached manifests missing a required output.
- Symlinks matched by a source glob are a PlanError for now (no symlink InputRef kind).
- Sandbox v1 checks argv, input patterns, outputs, config file paths and debug patterns; absolute
  paths inside `shell` scripts (config file content) and env values are not checked (P0-13/P3-01).
- `Plan.action()` / `ActionSpec.input()` are linear scans; the driver should build its own index.
