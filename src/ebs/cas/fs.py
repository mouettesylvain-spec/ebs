"""Filesystem CAS backend: one directory per domain, on NFS or local disk (interfaces.md § 5).

Layout under `<root>/<domain>/`:

    blobs/<algo>/<ab>/<cd>/<hex>        immutable file contents, mode 0444
    trees/<algo>/<ab>/<cd>/<hex>.json   canonical-JSON tree manifests, mode 0444
    tmp/                                upload staging on the same filesystem

Writes stream into `tmp/<random>` while hashing, fsync, chmod 0444, then `rename` into place and
fsync the shard directory. `rename` is atomic on one filesystem (also on NFS), so a reader sees
either nothing or the complete object (I8); if the object already exists the upload is discarded
(first writer wins; concurrent writers of one digest carry identical bytes anyway). Nothing relies
on `O_EXCL` lock files or `flock`, whose NFS semantics are unreliable (R9): temporary names carry
128 random bits instead. Directory modes and the domain group are left to the deployment.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import shutil
import stat
from collections.abc import Iterable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import BinaryIO, Final, Literal

from ebs.cas.api import DEFAULT_COPY_THRESHOLD, MaterializeMode, ObjectKind
from ebs.cas.materialize import materialize
from ebs.core.canon import canonical_json
from ebs.core.clock import Clock, SystemClock
from ebs.core.digest import ALGOS, Algo, Digest, StreamingHasher, hash_bytes
from ebs.core.errors import CasError, TreeError
from ebs.core.log import get_logger
from ebs.core.tree import TreeManifest, build_tree

_log = get_logger(__name__)

_DOMAIN_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_HEX_RE: Final = re.compile(r"[0-9a-f]{64}")
_TMP_MAX_AGE: Final = timedelta(hours=24)
_OBJECT_MODE: Final = 0o444
_Area = Literal["blobs", "trees"]


class FsCAS:
    """CAS on a POSIX filesystem, scoped to one domain under `root`."""

    def __init__(
        self,
        root: Path,
        domain: str,
        *,
        clock: Clock | None = None,
        algo: Algo = "sha256",
        chunk_size: int = 1 << 20,
    ) -> None:
        if not isinstance(domain, str) or _DOMAIN_RE.fullmatch(domain) is None:
            raise CasError(
                f"invalid CAS domain {domain!r}: use letters, digits, '.', '_' or '-', starting "
                "with a letter or digit (it becomes a directory name under the CAS root)"
            )
        if chunk_size <= 0:
            raise CasError(f"CAS chunk_size must be a positive number of bytes, got {chunk_size}")
        self.domain = domain
        self._base = Path(os.path.abspath(root)) / domain
        self._tmp = self._base / "tmp"
        self._clock: Clock = clock or SystemClock()
        self._algo: Algo = algo
        self._chunk = chunk_size

    # -- paths ------------------------------------------------------------------------------

    def blob_path(self, d: Digest) -> Path:
        return self._path("blobs", d)

    def tree_path(self, d: Digest) -> Path:
        return self._path("trees", d)

    def _path(self, area: _Area, d: Digest) -> Path:
        # Digest validates itself, but a frozen dataclass can still be forced; a path component
        # built from an unchecked hex could leave the domain root, so re-check here.
        if (
            not isinstance(d, Digest)
            or d.algo not in ALGOS
            or not isinstance(d.hex, str)
            or _HEX_RE.fullmatch(d.hex) is None
        ):
            raise CasError(
                f"invalid digest {d!r} for CAS domain {self.domain!r}: expected a Digest with "
                "64 lowercase hex characters"
            )
        ab, cd, hex_ = d.shard()
        name = f"{hex_}.json" if area == "trees" else hex_
        return self._base / area / d.algo / ab / cd / name

    # -- writes -----------------------------------------------------------------------------

    def put_bytes(self, data: bytes) -> Digest:
        view = memoryview(data)
        chunks = (view[i : i + self._chunk].tobytes() for i in range(0, len(view), self._chunk))
        return self._store("blobs", chunks, None, f"<{len(data)} bytes>")[0]

    def put_file(self, path: Path, *, expected: Digest | None = None) -> Digest:
        return self._put_file(path, expected)[0]

    def _put_file(self, path: Path, expected: Digest | None = None) -> tuple[Digest, int]:
        if expected is not None:
            self._path("blobs", expected)  # validate before touching the disk
        try:
            with open(path, "rb", buffering=0) as src:
                return self._store(
                    "blobs", iter(lambda: src.read(self._chunk), b""), expected, path
                )
        except OSError as exc:
            raise CasError(
                f"cannot store {path} in CAS domain {self.domain!r}: {exc.strerror or exc}"
            ) from exc

    def put_tree(self, root: Path) -> Digest:
        # build_tree hashes every file through the uploader, so blobs land before any manifest.
        root_digest, manifests = build_tree(root, self._put_file)
        written: set[Digest] = set()
        self._put_manifests(root_digest, manifests, written)
        return root_digest

    def _put_manifests(
        self, d: Digest, manifests: dict[Digest, TreeManifest], written: set[Digest]
    ) -> None:
        """Children before parents, so a visible manifest never references a missing one (I9)."""
        if d in written:
            return
        manifest = manifests[d]
        for entry in manifest.entries:
            if entry.type == "dir" and entry.digest is not None:
                self._put_manifests(entry.digest, manifests, written)
        self._store("trees", [canonical_json(manifest.to_json())], d, f"tree manifest {d}")
        written.add(d)

    def _store(
        self, area: _Area, chunks: Iterable[bytes], expected: Digest | None, source: object
    ) -> tuple[Digest, int]:
        algo: Algo = expected.algo if expected is not None else self._algo
        tmp = self._tmp / f"{secrets.token_hex(16)}.tmp"
        try:
            os.makedirs(self._tmp, exist_ok=True)
            # No O_EXCL: the random name already makes collisions impossible (R9).
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with open(fd, "wb", closefd=True) as out:
                hasher = StreamingHasher(algo)
                for chunk in chunks:
                    hasher.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(fd)
                digest, size = hasher.finish()
                if expected is not None and digest != expected:
                    raise CasError(
                        f"digest mismatch storing {source} in CAS domain {self.domain!r}: "
                        f"expected {expected}, got {digest}; the input changed or the declared "
                        "digest is wrong"
                    )
                os.fchmod(fd, _OBJECT_MODE)
            target = self._path(area, digest)
            os.makedirs(target.parent, exist_ok=True)
            if os.path.lexists(target):
                os.unlink(tmp)  # first writer wins; the object is immutable
                return digest, size
            os.rename(tmp, target)
            _fsync_dir(target.parent)
            return digest, size
        except OSError as exc:
            raise CasError(
                f"cannot write {source} to CAS domain {self.domain!r} under {self._base}: "
                f"{exc.strerror or exc}; check free space and permissions on the CAS filesystem"
            ) from exc
        finally:
            _unlink_quietly(tmp)

    # -- reads ------------------------------------------------------------------------------

    def has(self, d: Digest) -> bool:
        return os.path.lexists(self.blob_path(d)) or os.path.lexists(self.tree_path(d))

    def open(self, d: Digest) -> BinaryIO:
        path = self.blob_path(d)
        try:
            return open(path, "rb")
        except FileNotFoundError:
            raise CasError(
                f"blob {d} not found in CAS domain {self.domain!r} ({path}); it may have been "
                "garbage-collected or never uploaded: rebuild the producing action"
            ) from None

    def local_path(self, d: Digest) -> Path | None:
        path = self.blob_path(d)
        return path if os.path.lexists(path) else None

    def get_tree(self, d: Digest) -> TreeManifest:
        path = self.tree_path(d)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            raise CasError(
                f"tree {d} not found in CAS domain {self.domain!r} ({path}); it may have been "
                "garbage-collected or never uploaded: rebuild the producing action"
            ) from None
        except OSError as exc:
            raise CasError(f"cannot read tree manifest {path}: {exc.strerror or exc}") from exc
        if hash_bytes(data, d.algo) != d:
            raise CasError(f"tree manifest {path} is corrupt: its content does not hash to {d}")
        try:
            manifest = TreeManifest.from_json(json.loads(data))
        except ValueError as exc:
            raise CasError(f"tree manifest {path} is not valid JSON: {exc}") from None
        except TreeError as exc:
            raise CasError(f"tree manifest {path} is invalid: {exc}") from None
        if manifest.digest(d.algo) != d:
            raise CasError(
                f"tree manifest {path} is not in canonical form, so it does not re-encode to {d}"
            )
        return manifest

    def verify(self, d: Digest) -> bool:
        found = False
        for path in (self.blob_path(d), self.tree_path(d)):
            try:
                actual = self._hash_path(path, d.algo)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise CasError(f"cannot verify CAS object {path}: {exc.strerror or exc}") from exc
            if actual != d:
                return False
            found = True
        return found

    def _hash_path(self, path: Path, algo: Algo) -> Digest:
        hasher = StreamingHasher(algo)
        with open(path, "rb", buffering=0) as f:
            while block := f.read(self._chunk):
                hasher.update(block)
        return hasher.finish()[0]

    def materialize(
        self,
        d: Digest,
        kind: ObjectKind,
        dest: Path,
        mode: MaterializeMode = "auto",
        copy_threshold: int = DEFAULT_COPY_THRESHOLD,
        *,
        writable: bool = False,
    ) -> None:
        materialize(self, d, kind, dest, mode, copy_threshold, writable=writable)

    # -- GC support -------------------------------------------------------------------------

    def delete(self, d: Digest) -> None:
        for path in (self.blob_path(d), self.tree_path(d)):
            _unlink_quietly(path)

    def iter_digests(self) -> Iterator[tuple[Digest, int]]:
        for area in ("blobs", "trees"):
            suffix = ".json" if area == "trees" else ""
            for algo in _subdirs(self._base / area):
                if algo not in ALGOS:
                    _log.warning("cas.foreign_entry", path=str(self._base / area / algo))
                    continue
                for ab in _subdirs(self._base / area / algo):
                    for cd in _subdirs(self._base / area / algo / ab):
                        yield from self._iter_shard(self._base / area / algo / ab / cd, suffix)

    def _iter_shard(self, shard: Path, suffix: str) -> Iterator[tuple[Digest, int]]:
        algo, ab, cd = shard.parts[-3:]
        for entry in _scandir(shard):
            hex_ = entry.name.removesuffix(suffix) if entry.name.endswith(suffix) else ""
            st = entry.stat(follow_symlinks=False)
            if (
                _HEX_RE.fullmatch(hex_) is None
                or (hex_[0:2], hex_[2:4]) != (ab, cd)
                or not stat.S_ISREG(st.st_mode)
            ):
                _log.warning("cas.foreign_entry", path=str(shard / entry.name))
                continue
            yield Digest(algo, hex_), st.st_size  # type: ignore[arg-type]  # checked above

    def cleanup_tmp(self, max_age: timedelta = _TMP_MAX_AGE) -> int:
        """Remove staging files older than `max_age` (abandoned uploads); returns the count."""
        cutoff = self._clock.now().timestamp() - max_age.total_seconds()
        removed = 0
        for entry in _scandir(self._tmp):
            path = self._tmp / entry.name
            try:
                st = entry.stat(follow_symlinks=False)
                if st.st_mtime >= cutoff:
                    continue
                if stat.S_ISDIR(st.st_mode):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.unlink(path)
            except FileNotFoundError:
                continue  # a writer renamed it into place (or another cleaner won) meanwhile
            except OSError as exc:
                raise CasError(
                    f"cannot clean stale CAS staging file {path}: {exc.strerror or exc}; check "
                    f"that the GC account can write {self._tmp}"
                ) from exc
            removed += 1
        if removed:
            _log.info("cas.tmp_cleaned", domain=self.domain, removed=removed)
        return removed


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _unlink_quietly(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)


def _scandir(path: Path) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(path) as it:
            return list(it)
    except FileNotFoundError:
        return []


def _subdirs(path: Path) -> list[str]:
    names: list[str] = []
    for entry in _scandir(path):
        if entry.is_dir(follow_symlinks=False):
            names.append(entry.name)
        else:
            _log.warning("cas.foreign_entry", path=str(path / entry.name))
    return sorted(names)
