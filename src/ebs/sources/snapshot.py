"""Snapshot declared sources into the CAS at plan time (invariant I7).

`snapshot(base, pattern)` resolves the glob, gives every matched file a content id and returns
the digest of one tree holding the matched paths relative to `base` (symlinks kept as symlinks).
Actions later run on those CAS bytes, never on the live paths, so an edit made during a build
cannot leak in.

A file's id comes from, in order (R3-R6, docs/architecture.md "Why a stale stat cannot cause a
wrong build"):
1. git: a clean tracked file's blob id, mapped to SHA-256 through the persistent map (the size
   must match too);
2. the stat cache (full stat key, racy-clean rule);
3. reading the file: `CAS.put_file` hashes the bytes while copying them, and the digest it
   returns is the id (never a separate hash-then-copy). The read counts only if the file's stat
   is identical before and after it; otherwise it is repeated.
`rehash=True` or a path on an untrusted mount skips 1 and 2. A sampled fraction of 1/2 hits is
re-read (audit); a mismatch is recorded, the read digest is used and the stale entry replaced.
"""

from __future__ import annotations

import os
import random
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, TypeAlias

from ebs.cas.api import CAS
from ebs.core.clock import Clock
from ebs.core.digest import Digest
from ebs.core.errors import SourceError, SourceEscapeError, TreeError
from ebs.core.log import get_logger
from ebs.core.tree import TreeEntry, TreeManifest
from ebs.sources.gitids import GitIds, blob_algo, git_blob_hex
from ebs.sources.globs import resolve_glob
from ebs.sources.statcache import StatCache

_log = get_logger(__name__)

MAX_READ_ATTEMPTS: Final = 3
_CHUNK: Final = 1 << 20

_Node: TypeAlias = "dict[str, TreeEntry | _Node]"


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    digest: Digest  # tree manifest of the matched paths, relative to the base
    files: tuple[str, ...]  # matched relative paths (files and symlinks), sorted
    size: int  # total bytes of the regular files


def _stat_key(st: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_mode)


class SourceSnapshotter:
    """Resolves source globs to CAS trees; one instance per plan (git state is cached)."""

    def __init__(
        self,
        cas: CAS,
        statcache: StatCache,
        *,
        clock: Clock,
        rehash: bool = False,
        audit_fraction: float = 0.01,
        untrusted_mounts: Sequence[Path] = (),
        git: GitIds | Literal["auto"] | None = "auto",
        rng: random.Random | None = None,
    ) -> None:
        if not 0.0 <= audit_fraction <= 1.0:
            raise SourceError(
                f"stat cache audit_fraction must be between 0 and 1, got {audit_fraction}"
            )
        self._cas = cas
        self._cache = statcache
        self._clock = clock
        self._rehash = rehash
        self._audit_fraction = audit_fraction
        mounts = {os.path.abspath(m) for m in untrusted_mounts}
        self._untrusted = tuple(sorted(mounts | {os.path.realpath(m) for m in mounts}))
        self._git = GitIds() if git == "auto" else git
        self._rng = rng if rng is not None else random.Random()
        self.audit_mismatches: list[Path] = []

    # -- public -----------------------------------------------------------------------------

    def snapshot(self, base: Path, pattern: str, *, optional: bool = False) -> SnapshotResult:
        """Glob -> tree digest (logical layout relative to `base`), every blob in the CAS."""
        files = resolve_glob(base, pattern, optional=optional)
        root: _Node = {}
        for rel in files:
            parts = rel.split("/")
            node = root
            for part in parts[:-1]:
                child = node.setdefault(part, {})
                if not isinstance(child, dict):
                    raise SourceError(
                        f"source pattern {pattern!r} matches {part!r} both as a file and as a "
                        "directory; narrow the pattern"
                    )
                node = child
            node[parts[-1]] = self._entry(Path(base, *parts), parts, pattern)
        digest, size = self._store_dir(root)
        return SnapshotResult(digest, tuple(files), size)

    def snapshot_file(self, path: Path) -> Digest:
        """The content id of one file (following symlinks), stored in the CAS."""
        st = self._stat(path, follow=True)
        if not stat.S_ISREG(st.st_mode):
            raise SourceError(f"source {path} is not a regular file; declare a file")
        return self._file_id(path, st)[0]

    # -- tree -------------------------------------------------------------------------------

    def _entry(self, path: Path, parts: list[str], pattern: str) -> TreeEntry:
        st = self._stat(path, follow=False)
        try:
            if stat.S_ISLNK(st.st_mode):
                target = os.readlink(path)
                self._check_link_target(path, target, len(parts) - 1, pattern)
                return TreeEntry(parts[-1], "symlink", None, 0, False, target)
            if stat.S_ISREG(st.st_mode):
                # size and mode come from the stat that bracketed the accepted read
                digest, st = self._file_id(path, st)
                return TreeEntry(
                    parts[-1], "file", digest, st.st_size, bool(st.st_mode & stat.S_IXUSR), None
                )
        except TreeError as exc:
            raise SourceError(f"source {path}: {exc}") from None
        except OSError as exc:
            raise SourceError(f"cannot read source {path}: {exc.strerror or exc}") from exc
        raise SourceError(
            f"source {path} (pattern {pattern!r}) is not a regular file, directory or symlink; "
            "exclude it from the pattern"
        )

    @staticmethod
    def _check_link_target(path: Path, target: str, depth: int, pattern: str) -> None:
        if target.startswith("/"):
            raise SourceError(
                f"source symlink {path} has absolute target {target!r}; snapshots keep symlinks, "
                "so make it relative (pointing inside the base)"
            )
        for part in target.split("/"):
            if part in {"", "."}:
                continue
            depth = depth - 1 if part == ".." else depth + 1
            if depth < 0:
                raise SourceEscapeError(
                    f"source pattern {pattern!r} matches symlink {path}, whose target {target!r} "
                    f"climbs out of the snapshot root to {os.path.normpath(path.parent / target)}"
                    "; make it point inside the base without leaving it"
                )

    def _store_dir(self, node: _Node) -> tuple[Digest, int]:
        """Children first, so every manifest is stored after what it references (I9)."""
        entries: list[TreeEntry] = []
        total = 0
        for name in sorted(node):
            value = node[name]
            if isinstance(value, dict):
                digest, size = self._store_dir(value)
                value = TreeEntry(name, "dir", digest, size, False, None)
            entries.append(value)
            total += value.size
        return self._cas.put_manifest(TreeManifest(tuple(entries))), total

    # -- file ids ---------------------------------------------------------------------------

    def _file_id(self, path: Path, st: os.stat_result) -> tuple[Digest, os.stat_result]:
        """The id of the file and the stat it describes (a re-read may have changed it)."""
        if self._bypass(path):
            return self._read(path, st, None)
        blob = self._git.blob_id(path) if self._git is not None else None
        cached: Digest | None = None
        from_git = False
        if blob is not None:
            hit = self._cache.lookup_git_blob(blob)
            if hit is not None and hit[1] == st.st_size:
                cached, from_git = hit[0], True
        if cached is None:
            cached = self._cache.lookup(st, path)
        if cached is None:
            return self._read(path, st, blob)
        audit = self._audit_fraction > 0 and self._rng.random() < self._audit_fraction
        if not audit and self._cas.has(cached):
            return cached, st
        # Audited, or the blob is missing from this CAS: read (and upload) the bytes.
        digest, st = self._read(path, st, blob)
        if digest != cached:
            self._mismatch(path, cached, digest, blob if from_git else None)
        return digest, st

    def _read(
        self, path: Path, st: os.stat_result, blob: str | None
    ) -> tuple[Digest, os.stat_result]:
        """Hash while copying into the CAS; accept only a read bracketed by identical stats."""
        for _ in range(MAX_READ_ATTEMPTS):
            recorded_at = self._clock.now().timestamp()  # before reading: see the racy rule
            digest = self._cas.put_file(path)
            after = self._stat(path, follow=True)
            if _stat_key(after) == _stat_key(st):
                self._cache.store(st, path, digest, recorded_at)
                if blob is not None:
                    self._learn_git_blob(blob, digest, st.st_size)
                return digest, st
            _log.info("sources.changed_during_read", path=str(path))
            if not stat.S_ISREG(after.st_mode):
                raise SourceError(f"source {path} stopped being a regular file while being read")
            st = after
        raise SourceError(
            f"source {path} kept changing while it was being snapshotted "
            f"({MAX_READ_ATTEMPTS} attempts); stop writing to it and plan again"
        )

    def _learn_git_blob(self, blob: str, digest: Digest, size: int) -> None:
        """Map blob -> digest only if the stored bytes really are that git blob.

        Work-tree bytes can differ from the blob (LFS, eol or clean/smudge filters); checking
        the CAS copy keeps the map a pure content fact, valid for every checkout.
        """
        if self._cache.lookup_git_blob(blob) == (digest, size):
            return
        with self._cas.open(digest) as f:
            actual = git_blob_hex(iter(lambda: f.read(_CHUNK), b""), size, blob_algo(blob))
        if actual == blob:
            self._cache.store_git_blob(blob, digest, size)

    def _mismatch(self, path: Path, cached: Digest, actual: Digest, blob: str | None) -> None:
        self.audit_mismatches.append(path)
        _log.warning(
            "sources.stale_cache_entry",
            path=str(path),
            cached=str(cached),
            actual=str(actual),
            source="git" if blob is not None else "stat",
        )
        if blob is not None:
            hit = self._cache.lookup_git_blob(blob)
            if hit is not None and hit[0] == cached:
                self._cache.forget_git_blob(blob)

    def _bypass(self, path: Path) -> bool:
        if self._rehash:
            return True
        if not self._untrusted:
            return False
        candidates = {os.path.abspath(path), os.path.realpath(path)}
        return any(
            os.path.commonpath([mount, c]) == mount for mount in self._untrusted for c in candidates
        )

    @staticmethod
    def _stat(path: Path, *, follow: bool) -> os.stat_result:
        try:
            return os.stat(path, follow_symlinks=follow)
        except OSError as exc:
            raise SourceError(f"cannot read source {path}: {exc.strerror or exc}") from exc
