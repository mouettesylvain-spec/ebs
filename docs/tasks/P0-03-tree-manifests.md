# P0-03 — Tree manifests

Status: review · Phase: 0 · Depends on: P0-02 · Parallel-safe with: P0-05, P0-07, P0-10 · Size: S

## Goal
Directories get a content identity (Merkle tree of canonical JSON manifests) so directory inputs and
outputs (Questa work libraries, report dirs) can be hashed, stored and compared.

## Read first
- docs/architecture.md § "Hashing, action keys and caching" → "Input ids"
- docs/design/interfaces.md §2; invariants I5

## Scope (files)
- create `src/ebs/core/tree.py`
- create `tests/unit/core/test_tree.py`

## Out of scope
Uploading to CAS (P0-09). Hard-link farms (P0-09 materialize).

## Contract
interfaces.md §2.

## Requirements
- R1 `build_tree` walks a directory and returns the root digest plus every manifest (root and nested)
  keyed by digest; entries sorted by UTF-8 bytes of the name.
- R2 I5: digest independent of mtime, atime, owner, group, permission bits other than user-execute,
  and of `os.scandir` order (test by monkeypatching scandir to return reversed order).
- R3 Symlinks: recorded with their target, never followed; absolute targets or targets escaping the
  tree root raise `TreeError` with the offending path.
- R4 Rejected: special files (FIFO, socket, device), names that are not valid UTF-8, non-NFC names
  (error message suggests renaming); hard links are treated as ordinary files.
- R5 Empty directories are represented (an empty manifest) and differ from absent directories.
- R6 `TreeManifest.from_json(m.to_json()) == m`, and `from_json` validates sorting/uniqueness/field
  consistency (e.g. symlink with a digest is invalid).
- R7 Directory `size` equals the sum of file sizes below it.
- R8 The hasher is injected (`build_tree(root, hasher=…)`) so the stat cache (P0-06) can plug in.

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `test_tree.py::test_nested_manifests_returned` | R1 | unit |
| `::test_metadata_independence`, `::test_scandir_order_independence` | R2 | unit/property |
| `::test_symlink_recorded_not_followed`, `::test_symlink_escape_rejected[abs,dotdot]` | R3 | unit |
| `::test_special_files_rejected`, `::test_non_nfc_name_rejected` | R4 | unit |
| `::test_empty_dir_distinct` | R5 | unit |
| `::test_json_roundtrip` (Hypothesis over generated trees), `::test_from_json_rejects[...]` | R6 | property/unit |
| `::test_dir_size_sum` | R7 | unit |
| `::test_hasher_injected_is_used` | R8 | unit |

## Done when
- [x] `make check` passes; `ebs.core` ≥ 95 % coverage (tree.py 100 % line and branch)

## Notes
- Entry JSON holds only the fields of its type, so an inconsistent entry (e.g. a symlink with a
  digest) cannot even be written; `from_json` requires the exact key set.
- Manifests are always hashed with sha256 (the contract's `build_tree` has no `algo`), even when
  the injected hasher returns blake3 file digests. Revisit if blake3 becomes a default.
- Symlink escapes are checked twice: lexically (also rejects `../<root-name>/x`, which only
  resolves inside because of the root's own name) and with `realpath` (catches chains such as
  `sub/up -> ..` plus `x -> sub/up/..`). Manifests from the CAS can't be checked this way; P0-09
  got a note that materialization must not follow symlinks.
- Owner/group independence (I5) is not varied by the test: that needs root. The code never reads
  st_uid/st_gid.
- `TreeError` added to `ebs.core.errors` and interfaces.md §11.
