# P1-11 — `ebs seeds gen`

Status: todo · Phase: 1 · Depends on: P0-05 · Size: S

## Goal
Seeds are explicit table values (explicitness rule 1); a command generates them into the table so the
choice is committed and reviewable.

## Read first
- docs/architecture.md § "Flow description format" → Explicitness rules

## Scope (files)
- create `src/ebs/cli/seeds.py`, `src/ebs/flow/seeds.py`; tests `tests/unit/flow/test_seeds.py`

## Requirements
- R1 `ebs seeds gen TABLE --per-test N [--column seed] [--base-seed S]` appends N rows per distinct test
  (copying other columns) with seeds from a documented deterministic PRNG (`random.Random(S)`; 31-bit
  positive ints) — same arguments ⇒ same output.
- R2 Never modifies existing rows; `--replace` regenerates only rows whose seed cell is empty.
- R3 Preserves CSV formatting (column order, quoting style, comments) and YAML tables' comments (ruamel).
- R4 Refuses to write duplicate (test, seed) pairs.

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `test_seeds.py::test_deterministic`, `::test_range` | R1 | unit |
| `test_seeds.py::test_existing_rows_untouched`, `::test_replace_empty_only` | R2 | unit |
| `test_seeds.py::test_format_preserved[csv,yaml]` | R3 | unit |
| `test_seeds.py::test_no_duplicates` | R4 | unit |

## Done when
- [ ] `make check` passes
