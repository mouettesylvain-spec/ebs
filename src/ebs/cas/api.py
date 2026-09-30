"""The content-addressed store protocol (docs/design/interfaces.md § 5).

A CAS holds immutable blobs and tree manifests of one confidentiality domain, addressed by digest.
Objects become visible only when complete (invariant I8), and a tree manifest only after every
blob and child manifest it references (I9).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO, Final, Literal, Protocol

from ebs.core.digest import Digest
from ebs.core.tree import TreeManifest

ObjectKind = Literal["file", "tree"]
MaterializeMode = Literal["copy", "hardlink", "symlink", "auto"]

DEFAULT_COPY_THRESHOLD: Final = 256 << 20  # bytes; larger files are linked, not copied


class CAS(Protocol):
    """One domain's store. Every method is scoped to `domain`; nothing crosses domains."""

    domain: str

    def has(self, d: Digest) -> bool:
        """True if a blob or tree manifest with this digest is stored."""
        ...

    def put_bytes(self, data: bytes) -> Digest: ...

    def put_file(self, path: Path, *, expected: Digest | None = None) -> Digest:
        """Store a file's bytes; with `expected`, a different digest raises CasError."""
        ...

    def put_tree(self, root: Path) -> Digest:
        """Store a directory: all blobs first, then manifests bottom-up, the root last."""
        ...

    def put_manifest(self, manifest: TreeManifest) -> Digest:
        """Store one tree manifest whose blobs and child manifests are already stored.

        For trees assembled from already-stored blobs (source snapshots). A missing child
        raises CasError, so a visible manifest is always complete (I9).
        """
        ...

    def get_tree(self, d: Digest) -> TreeManifest:
        """Read and validate a tree manifest; CasError if missing, corrupt or invalid."""
        ...

    def open(self, d: Digest) -> BinaryIO:
        """Open a blob for reading; CasError if missing."""
        ...

    def local_path(self, d: Digest) -> Path | None:
        """Path of a stored blob for read-only bind/symlink; None if absent or not local."""
        ...

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
        """Place a blob (at `dest`) or a tree (as directory `dest`) on local disk.

        `auto`: a file ≤ `copy_threshold` is copied, a larger one symlinked from the CAS; tree
        files are hard-linked when on the same filesystem, else chosen by the same threshold.
        Files are read-only and directories writable; `writable=True` copies every file instead.
        Executable files are always copied (the shared blob's mode must not change).
        """
        ...

    def verify(self, d: Digest) -> bool:
        """Rehash the stored object; False if it is missing or its bytes do not match."""
        ...

    def delete(self, d: Digest) -> None:
        """Remove the blob and/or manifest with this digest (GC only); absent is a no-op."""
        ...

    def iter_digests(self) -> Iterator[tuple[Digest, int]]:
        """Every stored blob and manifest with its size in bytes (GC only)."""
        ...
