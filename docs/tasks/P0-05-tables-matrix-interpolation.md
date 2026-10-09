# P0-05 — Parameter tables, matrix expansion, interpolation

Status: done · Phase: 0 · Depends on: P0-04 · Parallel-safe with: P0-03, P0-07, P0-10 · Size: M

## Goal
Turn a step with a `matrix` into one concrete instance per table row, with every `${row.*}`,
`${params.*}` and `${env.*}` reference resolved, so the planner receives explicit values only.

## Read first
- docs/architecture.md § "Flow description format" → Concepts (Matrix), Explicitness rules 1–2
- docs/design/interfaces.md §3 "Interpolation grammar"

## Scope (files)
- create `src/ebs/flow/tables.py`, `src/ebs/flow/matrix.py`, `src/ebs/flow/interp.py`
- create `tests/unit/flow/test_tables.py`, `test_matrix.py`, `test_interp.py`, fixtures under
  `tests/fixtures/tables/`

## Out of scope
Resolving `${imports.*}` and `${steps.*}` (the planner does that in P0-08; this task provides the
parser and an extension point `Resolver` protocol for them). Seed generation (P1-11).

## Contract
```python
@dataclass(frozen=True) class Table: columns: tuple[str, ...]; rows: tuple[Mapping[str, str], ...]; source: Path; digest: Digest
def load_table(path: Path) -> Table                      # .csv (RFC 4180, UTF-8, header row) or .yaml (list of maps)
def expand_matrix(step: StepDef, tables: Mapping[str, Table]) -> list[StepInstance]
class Resolver(Protocol):
    def resolve(self, ref: Ref, *, where: SourceLocation) -> str | list[str]: ...
def parse_template(s: str) -> Template                   # list of literal/ref parts
def render(t: Template, resolver: Resolver) -> str | list[str]
```
`StepInstance` has `step`, `row: Mapping[str,str]`, `instance_id` (e.g. `sim[seed=17,test=smoke]`,
keys sorted, values escaped), and resolved params/env/resources.

## Requirements
- R1 CSV: header required, duplicate column names rejected, ragged rows rejected with row number,
  values kept as strings (no type guessing), `#` comment lines and blank lines ignored, BOM stripped.
- R2 YAML tables: list of flat maps with scalar values; all rows must have the same keys.
- R3 Matrix modes: `table:` (one instance per row), `cross: [a.csv, b.csv]` (cartesian product;
  column-name collisions are errors), `zip: [a.csv, b.csv]` (same length required), optional
  `filter: {col: value | [values]}`.
- R4 `instance_id` is stable and unique within a step; if the chosen key columns do not make rows
  unique, the error lists the duplicate rows. Key columns default to all columns; `matrix.id: [test, seed]`
  narrows them.
- R5 `${row.X}` for a missing column is an error listing available columns; `${env.X}` only resolves
  declared env of the same step; `$${` produces a literal `${`.
- R6 A template that is exactly one reference to a list-valued ref renders a list; mixing a list-valued
  ref with literal text is an error.
- R7 Rendering never evaluates code and never touches the environment of the calling process.
- R8 Expansion is deterministic: same inputs ⇒ same instance order (table order) and ids.
- R9 Table digest = content digest of the file bytes (used later in plan provenance).

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `test_tables.py::test_csv_rules[...]`, `::test_bom_and_comments` | R1 | unit |
| `test_tables.py::test_yaml_table_rules[...]` | R2 | unit |
| `test_matrix.py::test_table`, `::test_cross`, `::test_zip_length_mismatch`, `::test_filter` | R3 | unit |
| `test_matrix.py::test_instance_id_stable_unique`, `::test_duplicate_rows_error` | R4 | unit/property |
| `test_interp.py::test_missing_column_message`, `::test_env_scope`, `::test_escape` | R5 | unit |
| `test_interp.py::test_list_ref_rules` | R6 | unit |
| `test_interp.py::test_no_env_leak` (set a sentinel env var; ensure `${HOME}`-like strings stay literal errors) | R7 | unit |
| `test_matrix.py::test_deterministic_order` (Hypothesis on tables) | R8 | property |
| `test_tables.py::test_digest_is_file_bytes` | R9 | unit |

## Done when
- [x] `make check` passes (except a pre-existing P0-09 failure, see Notes); parser has a Hypothesis round-trip test (`parse_template` → str → parse)

## Notes
- Depends-on check: P0-04 is merged into main (3d12362) but README still said `review`; started anyway.
- Contract additions (documented in interfaces.md §3 "Tables, matrix expansion, rendering"):
  `expand_matrix` takes keyword `name` (a `StepDef` does not know its own name; the instance id
  needs it) and an optional outer `resolver` for `${imports.*}`/`${steps.*}`; `parse_template` and
  `render` take an optional `where: SourceLocation`; `Table` has `lines` (source line per row);
  `StepInstance` also carries `name`, `row_origin` and `render()` for the step's other templates.
- Design choices: expansion that yields no rows is an error; table values keep their source text
  (YAML `007` stays `"007"`; nulls and empty rows are rejected); whitespace-only CSV lines count as
  blank (quote a value made only of spaces); ids percent-encode keys and values (`urllib.parse.quote`);
  resolved `Resources` keep the step's `model_fields_set` so toolchain defaults can still apply.
- Matrix table paths must be literal and relative, without `..` (checked in `load_matrix_tables`).
- Follow-ups:
  - P0-09: `tests/unit/cas/test_materialize.py::test_resolve_matches_kernel` fails on main with a
    Hypothesis counterexample (symlinks `l0 -> l1/..`, `l1 -> d/../l0/..`); not touched here.
  - P0-08: `instance_id` can exceed NAME_MAX for wide rows; hash or shorten it before using it as a
    path component (`<O>/<instance_id>/…`).
  - YAML alias rows (`- *r`) report the anchor's line in error messages.
