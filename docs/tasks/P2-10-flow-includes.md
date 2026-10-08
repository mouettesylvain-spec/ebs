# P2-10 — Flow includes (split one flow across files)

Status: todo · Phase: 2 · Depends on: P0-08 · Parallel-safe with: P2-01, P2-02, P2-05 · Size: M

## Goal
One project's flow can be split into a root `flow.yaml` plus fragment files owned by different teams
(e.g. `blocks/lsu/flow.yaml`), still planned as one DAG and one build, so teams no longer edit the
same YAML file. Releases (P2-02/P2-03) stay the mechanism *between* teams with separate schedules.

## Read first
- docs/architecture.md § "Flow description format" → "Splitting a flow and reusing step definitions"
- docs/design/interfaces.md §3 (flow model, interpolation grammar)
- Existing code to follow: `src/ebs/flow/loader.py` (node positions, `FlowError`), `src/ebs/plan/planner.py`
  (`base`, `_static_refs`, `_snapshot`, `targets`)

## Scope (files)
- modify `src/ebs/flow/model.py` (`include:` field, `Fragment` model, dotted step ids in refs),
  `src/ebs/flow/loader.py` (load and merge fragments), `src/ebs/flow/interp.py` (ref grammar)
- modify `src/ebs/plan/planner.py` (per-step base directory), `src/ebs/plan/types.py` (`FlowInfo.files`),
  `src/ebs/plan/planfile.py`
- regenerate `schemas/flow.v1.schema.json`
- create `tests/unit/flow/test_includes.py`, `tests/unit/plan/test_includes_plan.py`,
  `tests/fixtures/flows/includes/**`

## Out of scope
- Reusable step templates (`use:` / `with:`) — P2-11.
- Includes fetched from another repo or a release — not planned; cross-team sharing is imports (P2-03).

## Contract
```yaml
# root flow.yaml (only the root has version/project/domain/imports/toolchains)
include:
  lsu:  blocks/lsu/flow.yaml       # alias -> path relative to the including file
  core: blocks/core/flow.yaml

# fragment file
version: 1
fragment: true                     # marks the file as not plannable on its own
include: { … }                     # optional, nested includes allowed
steps: { … }
```
- Step ids are the include path plus the step name: `lsu.compile`, `soc.lsu.compile`; root steps keep
  their plain name. Action ids follow (`lsu.compile[lib=x]`).
- Grammar (interfaces.md §3): `"steps." STEPID ".outputs." IDENT …` with `STEPID := NAME ("." NAME)*`.
  A dotted STEPID is absolute (from the root); an undotted one names a step of the same file.
  `outputs` is reserved as a step name and include alias.
- `FlowInfo(path, git, files: tuple[tuple[str, Digest], ...])`: every loaded flow file, root-relative,
  with its content digest.

## Requirements
- R1 `load_flow` follows `include:` recursively and returns one `Flow` whose `steps` use dotted ids, in a
  deterministic order (root steps, then includes in declaration order, depth-first).
- R2 A fragment may only contain `version`, `fragment`, `include` and `steps` (plus `templates`/`uses`
  once P2-11 lands). `project`, `domain`, `imports` or `toolchains` in a fragment is a `FlowError` that
  says to move them to the root. A fragment loaded directly with `ebs plan` fails with a message naming
  the root.
- R3 Errors point at the right file: `FlowError` carries the fragment's `file:line:col`. Include cycles,
  missing files, duplicate aliases, and an alias equal to a step name in the same file are errors.
- R4 Ref resolution: `${steps.compile…}` inside `blocks/lsu/flow.yaml` means `lsu.compile`;
  `${steps.core.compile…}` is absolute. Unknown ids get a "did you mean" over all dotted ids.
- R5 Paths in a fragment (input patterns, matrix tables, `workdir`) resolve against the fragment's
  directory; the default under open decision D8 applies to paths outside it.
- R6 `targets` accepts a step id or an include prefix: `lsu` selects every step under `lsu.`.
- R7 plan.json records `flow.files` (R1 order, root first); editing any fragment changes the plan digest.
- R8 No key schema change: a flow split into fragments, whose source paths and step ids are unchanged,
  produces the same action keys as before (golden keys untouched, `KEY_SCHEMA_VERSION` not bumped).
- R9 The JSON Schema accepts fragment files (`fragment: true`) for editor autocompletion.

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `test_includes.py::test_merge_order_and_ids` | R1 | unit |
| `test_includes.py::test_fragment_forbidden_keys[...]`, `::test_fragment_not_plannable` | R2 | unit |
| `test_includes.py::test_error_location_in_fragment`, `::test_cycle`, `::test_missing`, `::test_alias_clash` | R3 | unit |
| `test_includes.py::test_relative_and_absolute_refs`, `::test_unknown_ref_suggests` | R4 | unit |
| `test_includes_plan.py::test_paths_relative_to_fragment` | R5 | unit |
| `test_includes_plan.py::test_target_prefix` | R6 | unit |
| `test_includes_plan.py::test_flow_files_recorded` | R7 | unit |
| `test_includes_plan.py::test_split_flow_same_keys` | R8 | unit |
| `tests/unit/flow/test_schema.py::test_fragment_schema` | R9 | unit |

## Done when
- [ ] `make check` passes (paste the summary line)
- [ ] every R has a test; reviewer subagent reports no correctness gaps
- [ ] interfaces.md §3 updated (grammar, `FlowInfo.files`), commit body has "CONTRACT CHANGE: …"
- [ ] Status set to `review` here and in docs/tasks/README.md

## Notes
- Keys and step ids: the step name is not in the key document, but a consumer stages a producer's output
  at `<output>/<producer action id>` (`_staged_path` in the planner), so the producer's id is in the
  consumer's input logical paths. Renaming or moving a step therefore reruns its consumers once. This is
  expected; say so in the user docs rather than hiding it.
- Release selectors (P2-02 `--output`) and import refs keep working with dotted ids: the last segment is
  the output name (`--output lsu.pkg.rtl`).
- Recommend CODEOWNERS per fragment directory in the user docs; that is what gives each team its own
  review path.
