# P0-04 — Flow model, YAML loader, JSON Schema

Status: review · Phase: 0 · Depends on: P0-01 · Parallel-safe with: P0-02, P0-11 · Size: M

## Goal
`flow.yaml` files load into validated, immutable Pydantic models with precise `file:line:col` errors,
and a published JSON Schema gives editors autocompletion.

## Read first
- docs/architecture.md § "Flow description format" (the whole section, including the YAML example)
- docs/design/interfaces.md §3 (model names and extra P0 fields)

## Scope (files)
- create `src/ebs/flow/model.py`, `src/ebs/flow/loader.py`, `src/ebs/flow/schema.py`
- create `schemas/flow.v1.schema.json` (generated, committed) and `scripts/gen_schema.py`
- create `tests/unit/flow/test_model.py`, `test_loader.py`, `test_schema.py`,
  `tests/fixtures/flows/valid/*.yaml`, `tests/fixtures/flows/invalid/*.yaml` (each with an
  `# expect: <error substring> @ line:col` header comment)

## Out of scope
Table loading, matrix expansion and `${…}` resolution (P0-05): here `${…}` strings are kept verbatim,
but their syntax is checked. Per-kind validation of steps (P0-12).

## Contract
```python
def load_flow(path: Path, *, lock: Path | None = None) -> Flow
def flow_json_schema() -> JsonValue
```
Models per interfaces.md §3; `extra="forbid"`, `frozen=True`.

## Requirements
- R1 The architecture.md example flow loads without error (copy it to `valid/architecture_example.yaml`).
- R2 Unknown keys, wrong types, missing required fields produce `FlowError` with the YAML location of
  the offending node (ruamel round-trip loader positions), the field path (`steps.sim.resources.mem`) and,
  for unknown keys, a "did you mean" suggestion (difflib, cutoff 0.75).
- R3 `version: 1` is required; other versions fail with a message naming the supported versions.
- R4 Step names and output names match `^[a-z][a-z0-9_]{0,62}$`; duplicate keys in YAML are an error
  (ruamel allows them by default — disable).
- R5 Resource strings parse to typed values: memory (`8G`, `512M`, `1T`, binary units), time (`30m`,
  `2h`, `1-00:00:00`, `${row.timeout}` deferred), cpus (positive int). Invalid literals error at load time.
- R6 `outputs` accept the short form (`result: result.json` ⇒ file, deterministic) and the long form
  (`{file|dir: …, deterministic: bool, optional: bool}`); exactly one of `file`/`dir`.
- R7 `${…}` strings are syntax-checked against the grammar in interfaces.md §3 (not resolved); a
  malformed reference is a `FlowError` at its location.
- R8 YAML anchors/aliases and merge keys are supported; tags other than standard ones are rejected
  (no arbitrary object construction).
- R9 `flow_json_schema()` output equals the committed `schemas/flow.v1.schema.json`; every valid fixture
  validates against it and every invalid-by-schema fixture does not (use `jsonschema` in tests only —
  add it to dev deps).

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `test_loader.py::test_valid_fixtures_load[...]` | R1 | unit |
| `test_loader.py::test_invalid_fixtures[...]` (parses the `# expect:` header) | R2, R3, R4, R7 | unit |
| `test_model.py::test_memory_parse[...]`, `::test_time_parse[...]` | R5 | unit/property |
| `test_model.py::test_output_short_and_long_form` | R6 | unit |
| `test_loader.py::test_anchors_and_merge`, `::test_unsafe_tag_rejected` | R8 | unit |
| `test_schema.py::test_committed_schema_up_to_date`, `::test_fixtures_vs_schema` | R9 | unit |

## Done when
- [x] `make check` passes; ≥ 12 invalid fixtures covering distinct error kinds

## Notes
- Dependencies: `pydantic>=2.11` (`serialize_by_alias`, `json_schema_input_type`) and `ruamel.yaml`
  (runtime, both on the allowed list); `jsonschema` (dev, mandated by R9, added to overview.md).
  jsonschema ships no type stubs, so pyproject has a mypy `ignore_missing_imports` override for it.
- The loader only *composes* YAML and builds plain data from the node graph itself: tag allowlist
  (core schema + merge), own duplicate-key check (R4), merge semantics, alias-expansion cap
  (`MAX_NODES`), `%YAML` directives other than 1.2 rejected (1.1 would turn `yes` into a boolean).
- Choices made here (no open decision involved): `domain` is required; bare numbers for `mem`/`time`
  are rejected as ambiguous (SLURM reads them as MiB/minutes); floats and unquoted timestamps are
  rejected with a "quote it" hint; a step's `toolchain` must be declared; `script`/`command` are
  mutually exclusive; `cross`/`zip` need ≥ 2 tables; `imports` need exactly one of
  `channel`/`version`; output and config-file paths must be relative without `..`.
- `model_dump()` re-emits the YAML dialect (`8G`, `30m`, aliases `from`/`zip`) and re-validates.
- Follow-ups for P0-05: re-check `RelPath` values (outputs, config files) after interpolation
  (`${row.x}/..` passes today); decide whether never-interpolated strings (`toolchains.*.module`,
  `steps.*.toolchain`, import `channel`/`version`, matrix `filter` keys/`id`) should reject `${`.
  `check_ref_syntax` scans left to right, with `$${` as the escape; `interp.parse_template` must
  follow the same rule.
- Follow-up for P2-03: `load_flow(..., lock=)` is accepted but not read yet.
