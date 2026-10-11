# P1-11 — `ebs seeds gen`

Status: done · Phase: 1 · Depends on: P0-05 · Size: S

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
- [x] `make check` passes

## Notes
- Decisions (not fixed by the requirements): one `random.Random(base_seed)` stream, default
  `--base-seed 0`; `--replace` fills empty cells (row order) before `--per-test` appends (first
  appearance order of tests); a drawn seed that clashes with an existing or just-generated
  `(test, seed)` pair is redrawn; a table that already has a duplicate pair is refused; ASCII
  digit-only seeds compare numerically (`017` = `17`), anything else as text (` 17` ≠ `17`);
  `--test-column` (default `test`) names the test column; a missing seed column is an error (it
  is not added, which would change every existing row); new rows go at the end of the file.
- Files outside the listed scope: `src/ebs/cli/main.py` (registers `seeds`),
  `tests/unit/cli/test_seeds.py`, `docs/design/interfaces.md` (`--json` contract).
- YAML limits: ruamel may normalise spacing inside rows (flow-map spacing, a trailing comma, the
  column of an end-of-line comment on a row that grew, mixed per-row indentation). Comments,
  quotes, BOM, line ends and `---`/`...` markers are kept.
- Follow-up: the CLI could print the new rows (or a diff) so users can review before committing.
