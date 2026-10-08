# P2-11 — Reusable step templates (`use:` / `with:`)

Status: todo · Phase: 2 · Depends on: P2-10, P2-03 · Size: M

## Goal
A step recipe (e.g. "compile one Questa library") is written once as a template with typed arguments
and reused for many libraries, whether their sources are local files or imported releases, from any
fragment of a flow and from other teams' flows (template libraries shared through releases).

## Read first
- docs/architecture.md § "Flow description format" → "Splitting a flow and reusing step definitions"
- docs/tasks/P2-10-flow-includes.md (fragments, path bases, dotted ids)
- docs/tasks/P2-03-imports-flow-lock.md (lock entries, materializing imported trees)
- Existing code to follow: `src/ebs/flow/loader.py`, `src/ebs/flow/interp.py`

## Scope (files)
- modify `src/ebs/flow/model.py` (`TemplateDef`, `ArgDef`, `UseDef`, `StepDef.use` / `with_`),
  `src/ebs/flow/loader.py` (load template libraries, expand uses), `src/ebs/flow/interp.py` (`args.` refs)
- modify `src/ebs/plan/planfile.py` (record template origin per step)
- regenerate `schemas/flow.v1.schema.json`
- create `tests/unit/flow/test_templates.py`, `tests/unit/plan/test_templates_plan.py`,
  `tests/fixtures/flows/templates/**`

## Out of scope
- Python rule plugins (new `kind`s): P0-12 already covers them; templates are the YAML-level tool.
- Template inheritance (a template using another template) — follow-up if asked for.

## Contract
```yaml
# flowlib/questa.yaml — a template library (any flow or fragment file may also hold `templates:`)
version: 1
templates:
  questa_lib:
    args:
      lib:       { type: string }
      srcs:      { type: list }                 # source patterns or ${imports…} globs
      deps:      { type: list, default: [] }    # whole output refs of other libraries
      vlog_opts: { type: string, default: "-sv -timescale 1ns/1ps" }
    step:
      kind: questa.compile
      toolchain: questa
      inputs: { srcs: "${args.srcs}", deps: "${args.deps}" }
      params: { lib: "${args.lib}", vlog_opts: "${args.vlog_opts}" }
      outputs: { worklib: { dir: "work/${args.lib}", deterministic: false } }
      resources: { cpus: 2, mem: 8G, time: 30m }

# a flow or fragment using it
uses:
  q:   { from: "flowlib/questa.yaml" }                  # same repo, relative to this file
  std: { from: "${imports.flowlib}/questa.yaml" }       # another team's release, pinned in flow.lock
steps:
  lsu:
    use: q.questa_lib
    with: { lib: lsu, srcs: ["rtl/lsu/**/*.sv"], deps: ["${steps.common.outputs.worklib}"] }
  ip:                                                    # one action per row, sources from an import
    use: std.questa_lib
    matrix: { table: libs.csv }
    with: { lib: "${row.lib}", srcs: ["${imports.rtl}/${row.dir}/**/*.sv"] }
    resources: { mem: 16G }
```
- `${args.X}` is valid only inside a template's `step`; it is substituted at load time and never
  reaches the planner. A `list` arg may only be used as a whole value of a list-accepting field.
- Beside `use:` a step may set only `with`, `matrix`, `resources`, `licenses` and `debug`;
  `resources` and `licenses` merge key by key over the template's.

## Requirements
- R1 Expansion: a step with `use:` becomes an ordinary `StepDef` at load time; the expanded flow
  validates exactly like a hand-written one (same errors, same per-kind checks).
- R2 Arguments: unknown `with` keys, missing required args, wrong types (string vs list), and `${args.X}`
  naming an undeclared arg are `FlowError`s at the location of the offending node, with "did you mean".
- R3 `${row.*}`, `${imports.*}`, `${steps.*}` and `${params.*}` inside `with` values are kept verbatim
  and resolved later in the *calling* step's scope (matrix rows, dotted ids and path base of the
  calling file, per P2-10).
- R4 Template libraries load from a path relative to the declaring file, or from inside an imported
  release (`${imports.alias}/…`): the latter reads the file from the CAS at the release digest pinned
  in `flow.lock`, so changing a shared template needs `ebs lock update` like any other import.
- R5 Only `with`, `matrix`, `resources`, `licenses`, `debug` beside `use:`; anything else is an error
  that says to add an arg to the template.
- R6 Keys: a templated step and its hand-written equivalent produce identical action keys (templates
  add nothing to the key document; no `KEY_SCHEMA_VERSION` bump). Changing a template so that the
  expanded command changes changes the key.
- R7 plan.json records, per step, the template origin (`uses` alias, template name, library file
  digest or release digest) for `ebs plan --diff` and `ebs why`; it is outside the action key.
- R8 Template names and `uses` aliases follow the step name rule; duplicate names in one library and
  `uses` cycles (a library that `uses` itself transitively) are errors.

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `test_templates.py::test_expands_to_stepdef`, `::test_expanded_step_validated` | R1 | unit |
| `test_templates.py::test_arg_errors[...]` | R2 | unit |
| `test_templates.py::test_with_refs_resolved_in_caller_scope` | R3 | unit |
| `test_templates_plan.py::test_library_from_import_uses_lock`, `::test_library_relative_path` | R4 | unit |
| `test_templates.py::test_only_allowed_overrides[...]`, `::test_resources_merge` | R5 | unit |
| `test_templates_plan.py::test_template_same_keys_as_inline`, `::test_template_change_changes_key` | R6 | unit |
| `test_templates_plan.py::test_template_origin_recorded` | R7 | unit |
| `test_templates.py::test_names_and_cycles[...]` | R8 | unit |

## Done when
- [ ] `make check` passes (paste the summary line)
- [ ] every R has a test; reviewer subagent reports no correctness gaps
- [ ] interfaces.md §3 updated (models, `args.` refs, plan.json template origin), commit body has
      "CONTRACT CHANGE: …"
- [ ] Status set to `review` here and in docs/tasks/README.md

## Notes
- Templates vs rule plugins: a template is a YAML macro over an existing `kind`; a plugin is a new
  `kind` in Python with its own rule version in the key. Use a plugin when the recipe needs logic
  (parsing reports, classifying failures), a template when it only fills in fields.
- Literal relative paths inside a template body resolve in the caller's base, like `with` values;
  templates should take sources as args rather than hard-code them.
- R4 is the only part that needs P2-03. If P2-03 is late, land R1–R3, R5–R8 first and keep R4 as a
  follow-up task rather than reading channels directly.
