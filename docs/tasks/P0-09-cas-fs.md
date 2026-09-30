# P0-09 — CAS filesystem backend

Status: review · Phase: 0 · Depends on: P0-03 · Parallel-safe with: P0-12 · Size: M

## Goal
A content-addressed store on NFS that is safe under concurrent writers, never exposes partial objects,
and can materialize inputs cheaply on local scratch.

## Read first
- docs/architecture.md § "Artifact storage and garbage collection" (Layout, Write path, Read path)
- docs/design/interfaces.md §5; invariants I8, I9

## Scope (files)
- create `src/ebs/cas/api.py` (Protocol), `src/ebs/cas/fs.py`, `src/ebs/cas/materialize.py`
- create `tests/unit/cas/test_fs.py`, `test_fs_atomic.py`, `test_fs_tree_order.py`, `test_materialize.py`,
  `tests/integration/test_cas_concurrency.py`, `tests/contract/test_cas.py` (parametrized over backends)

## Out of scope
GC deletion policy (P1-09 decides what to delete; this task only implements `delete`/`iter_digests`).
S3 (P3-07). Chunking (P4).

## Contract
interfaces.md §5.

## Requirements
- R1 Layout exactly as interfaces.md §5; the domain is a constructor argument and every path is built
  from `root / domain`; a digest can never produce a path outside that root.
- R2 Write: stream to `tmp/<random>` while hashing, `fsync` file, verify against `expected` if given,
  `chmod 0444`, create shard dirs, `os.rename` into place (atomic on the same filesystem), `fsync` the
  shard dir. If the target exists, discard tmp and return success (first writer wins).
- R3 I8: 8 processes writing the same 50 MB blob concurrently all succeed and leave one object whose
  content matches its digest; no files remain in `tmp/` afterwards.
- R4 I9: `put_tree` uploads all blobs, then child manifests bottom-up, then the root manifest; fault
  injection (exception after N writes) never leaves a visible manifest referencing a missing object.
- R5 `put_file(expected=…)` with wrong bytes raises `CasError` and leaves nothing behind.
- R6 `verify` rehashes and returns False on corruption; `get_tree` validates the manifest (P0-03 rules).
- R7 `materialize(mode="auto")`: files ≤ `copy_threshold` are copied, larger are symlinked read-only from
  the CAS; trees are materialized as directories whose files are hard links when on the same filesystem,
  else copies/symlinks by the same threshold; executable bits restored; result is writable-dir/readonly-file
  unless `writable=True` requested (then copies).
- R8 Stale `tmp/` files older than 24 h are removed by `cleanup_tmp()` (used by GC).
- R9 Operations are safe on NFS semantics: no reliance on `O_EXCL` lock files or `flock`.

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `test_fs.py::test_layout`, `::test_domain_isolation_paths` | R1 | unit |
| `test_fs_atomic.py::test_write_sequence` (spy on os calls), `::test_existing_target_first_writer_wins` | R2 | unit |
| `tests/integration/test_cas_concurrency.py::test_same_blob_parallel` | R3 (I8) | integration |
| `test_fs_tree_order.py::test_fault_injection[...]` | R4 (I9) | unit/property |
| `test_fs.py::test_expected_mismatch` | R5 | unit |
| `test_fs.py::test_verify_detects_corruption`, `::test_get_tree_validates` | R6 | unit |
| `test_materialize.py::test_auto_modes[...]`, `::test_exec_bits`, `::test_writable_copy` | R7 | unit |
| `test_fs.py::test_cleanup_tmp` (FakeClock) | R8 | unit |
| `tests/contract/test_cas.py` (all of the above that are backend-agnostic) | all | contract |

## Done when
- [x] `make check` and `make check-all` pass; `ebs.cas` ≥ 95 % line coverage

## Notes
- From P0-03: tree manifests read back from the CAS are not re-checked against a filesystem, and a
  link can stay inside lexically while a chain of links escapes. Materialization must create
  symlinks last and never write through or follow a symlink (open with `O_NOFOLLOW`, or check
  each parent with `lstat`).
- Done: every tree symlink is resolved logically (`_resolve`: follows only the tree's own links,
  with the kernel's shared budget of 40 follows) and rejected if it escapes, before any link is
  created; symlinks are created after every file/dir; copies use `O_EXCL|O_NOFOLLOW`. The resolver
  is property-tested against the kernel, and the 40-link boundary is pinned. On failure `dest` is
  removed only if this call created it.
- Follow-up (low risk): a chain of more than 40 acyclic links counts as "inside", which is right
  for the kernel (ELOOP), but userspace resolvers without a hop limit could follow it out. We could
  reject acyclic over-budget chains while still allowing cycles.
- CONTRACT CHANGE (approved): `CAS.materialize` gained `*, writable: bool = False` (R7 needs it);
  interfaces.md §5 also documents what `has`/`open`/`verify`/`delete` address.
- Executable files are always copied (0555, or 0755 when writable), in every mode: a hard link or
  symlink would share the blob's 0444 inode, and chmod-ing it would change the shared object.
- Hard links share the blob inode: a tool running as the blob's owner could `chmod u+w` and
  modify the CAS object through it. The runner's input verification (I10) and `verify` catch it
  afterwards; the bubblewrap sandbox (P3-01) should mount the CAS read-only. Follow-up for P1-04.
- `auto` stops trying hard links for the rest of a call after EXDEV/ENOTSUP. EMLINK (the link
  limit on one popular blob) and EPERM (`fs.protected_hardlinks` refusing another user's blob)
  fall back for that file only.
- Extra test files beyond the scope list: `tests/helpers/cas.py` (sample tree + I9 checker, shared
  by unit/contract tests), `tests/helpers/cas_workers.py` (spawn-importable worker for R3), and the
  `cas` fixture in `tests/conftest.py` (named by docs/design/testing.md).
- Follow-up (deployment/P2-01): interfaces.md says CAS dirs are 2750, which gives the group no
  write permission, so a second user could not add objects to a shard dir created by the first.
  The code leaves dir modes to umask + the setgid bit inherited from the admin-created root; the
  deployment should use 2770 (or a umask of 007) for shared writers.
- `cleanup_tmp` is FsCAS-only (not in the Protocol); P1-09 calls it on the fs backend.
