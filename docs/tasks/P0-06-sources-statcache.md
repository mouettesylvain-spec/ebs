# P0-06 — Source snapshot and stat cache

Status: review · Phase: 0 · Depends on: P0-03, P0-09 · Parallel-safe with: P0-13 · Size: L

## Goal
Resolve declared source globs to files, give each a content id quickly (git blob ids, stat cache)
without ever trusting a stale stat, and snapshot the bytes into the CAS at plan time so builds are
immune to edits made while they run.

## Read first
- docs/architecture.md § "Hashing, action keys and caching" → "Input ids" and
  "Why a stale stat cannot cause a wrong build" (all five guards)
- docs/design/invariants.md I6, I7

## Scope (files)
- create `src/ebs/sources/globs.py`, `gitids.py`, `statcache.py`, `snapshot.py`
- create `tests/unit/sources/test_globs.py`, `test_gitids.py`, `test_statcache.py`, `test_snapshot.py`,
  `tests/integration/test_snapshot_isolation.py`

## Out of scope
Import resolution (P2-03). Toolchain trees (P0-07).

## Contract
```python
class SourceSnapshotter:
    def __init__(self, cas: CAS, statcache: StatCache, *, clock: Clock, rehash: bool = False,
                 audit_fraction: float = 0.01, untrusted_mounts: Sequence[Path] = ()) -> None
    def snapshot(self, base: Path, pattern: str) -> SnapshotResult   # glob -> tree digest (logical layout)
    def snapshot_file(self, path: Path) -> Digest
    audit_mismatches: list[Path]
class StatCache:  # sqlite at ~/.cache/ebs/statcache.sqlite (path injectable)
    def lookup(self, st: os.stat_result, path: Path) -> Digest | None
    def store(self, st: os.stat_result, path: Path, d: Digest, recorded_at: float) -> None
```
Globs: gitignore-style `**`, `*`, `?`, `[…]`; results sorted; symlinks inside the base are kept as
symlinks; the matched set is snapshotted as one tree preserving relative paths.

## Requirements
- R1 Glob semantics: `**` matches zero or more directories; hidden files match only if the pattern
  segment starts with `.`; no match is an error unless the input is marked `optional`.
- R2 Paths outside `base` (via `..`, absolute patterns, or symlinks resolving outside it) are rejected at
  plan time with `SourceEscapeError` naming the pattern and the resolved path (sandbox v1 rule).
- R3 Git guard: for files tracked and clean in a git work tree, the id comes from
  `git ls-files -s` + `git status --porcelain=v2` (one call each per repository, not per file), mapped
  from git blob id to SHA-256 through a persistent `git_blob -> sha256` map; dirty/untracked files fall
  through to the stat cache.
- R4 Stat key = (device, inode, size, mtime_ns, ctime_ns, path). Racy-clean rule: an entry whose
  mtime_ns is within `racy_window_s` of its `recorded_at` is treated as a miss.
- R5 `rehash=True` or a path on an untrusted mount bypasses both the git map and the stat cache.
- R6 Sampled audit: `audit_fraction` of stat-cache hits are rehashed; mismatches are recorded,
  the correct digest is used and the stale entry is replaced (never silently kept).
- R7 Snapshot: every snapshotted file is `put_file` into the CAS (skipped if `has()`), and the
  resulting tree reflects the bytes read during hashing (hash while copying; never hash then copy
  separately). I7 integration test: modify a source during a build → the build uses the old bytes.
- R8 Performance: re-snapshotting 5,000 unchanged files (generated in tmp_path) completes with zero
  file reads (assert via a counting hasher) and in < 3 s on CI.
- R9 The stat cache DB is safe for concurrent use by several `ebs` processes (WAL mode, busy timeout).

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `test_globs.py::test_semantics[...]` (table-driven), `::test_no_match_error` | R1 | unit |
| `test_globs.py::test_escape_rejected[...]` | R2 | unit |
| `test_gitids.py::test_clean_files_use_git`, `::test_dirty_falls_through`, `::test_single_git_call` | R3 | unit (real `git` in tmp repo) |
| `test_statcache.py::test_racy_clean`, `::test_same_mtime_different_content`, `::test_key_fields[...]` | R4 | unit |
| `test_statcache.py::test_rehash_bypass`, `::test_untrusted_mount_bypass` | R5 | unit |
| `test_statcache.py::test_audit_detects_and_repairs` (FakeClock, seeded RNG) | R6 | unit |
| `test_snapshot.py::test_hash_while_copy`, `tests/integration/test_snapshot_isolation.py` | R7 | unit/integration |
| `test_snapshot.py::test_no_reads_when_unchanged` (mark `slow`) | R8 | unit |
| `test_statcache.py::test_concurrent_writers` (multiprocessing) | R9 | integration |

## Done when
- [x] `make check` passes; I6/I7 tests exist with the names in invariants.md

## Notes
- CONTRACT CHANGE: `CAS.put_manifest(manifest) -> Digest` (interfaces.md §5). The protocol had no
  way to store a tree built from already-stored blobs; `put_tree` would re-read every file,
  breaking R7 (hash while copying) and R8 (zero reads). The backend refuses a manifest whose
  children are missing (I9). P3-07 (S3) must implement it.
- CONTRACT CHANGE: `SourceError` → `SourceEscapeError` in `ebs.core.errors`; new interfaces.md §12
  documents `ebs.sources`. Additive keyword args beyond the task contract: `snapshot(...,
  optional=)`, `SourceSnapshotter(git=, rng=)`, `StatCache(racy_window_s=, busy_timeout_s=, clock=)`.
- Stricter than R4: racy if `max(mtime_ns, ctime_ns) > recorded_at - window` (future timestamps
  are racy too). `recorded_at` is taken before the read.
- A read counts only if the file's stat (dev, ino, size, mtime, ctime, mode) is identical before
  and after; otherwise it is repeated (3 attempts, then `SourceError`).
- Git guard hardening: the `git_blob -> sha256` map is written only after the CAS bytes re-hash to
  that git blob id, and a map hit needs the file size to match (LFS/eol filters). Entries tagged
  assume-unchanged / skip-worktree (`ls-files -v`) are never trusted; git runs with
  `core.fsmonitor=false` and without repo-redirecting `GIT_*` env vars. Audits cover git hits too.
- `test_concurrent_writers` lives in `tests/integration/test_statcache_concurrency.py` (the table
  marks it integration; tests/unit is auto-marked unit). Extra helper files:
  `tests/helpers/sources.py`, `tests/helpers/statcache_workers.py`, `git_repo` fixture in
  tests/conftest.py (named by testing.md). Sources unit tests no-op the CAS fsync
  (tests/unit/sources/conftest.py): it dominated cold snapshots (~50 s for R8's 5,000 files).
- Follow-up (P0-16 R7): WAL SQLite is unsafe on NFS. Decided by the human (2026-10-05): the CLI
  defaults the stat cache to local disk (`/var/tmp/ebs-<uid>/`), overridable by `[stat_cache] path`.
- Follow-up (P0-08/P0-16): wire `[stat_cache]` config keys and report `audit_mismatches` as the
  `stat_audit_mismatch` event.
- Follow-up (P0-09 tests): `FsCAS._put_manifests` dedupe branch (fs.py `if d in written: return`)
  is covered only when Hypothesis generates identical subtrees, so cas line coverage flips
  between 99.8 % and 100 %; add a deterministic test with two identical sub-directories.
- Size-preserving clean/smudge filters on a checkout could still make a verified map entry wrong
  for that checkout; the sampled audit and `--rehash` (default in CI) are the remaining guards.
