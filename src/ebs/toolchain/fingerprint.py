"""Toolchain install-tree fingerprints (layer L1, docs/design/interfaces.md § 13).

A fingerprint identifies the exact bits of a tool installation without hashing gigabytes of it:
every entry under each install root contributes its relative path and type; regular files add
size, mtime_ns and the executable bit, plus a full content hash when `content_hash(path)` says so
(by default executables, ELF objects and script/library suffixes). Symlinks are recorded by their
target and never followed. atime, ownership and the other permission bits are not recorded.

The digest is SHA-256 over a header line and one JSON line per entry, sorted by (root, path), so
it does not depend on root order or directory listing order. Lines are `json.dumps` with
`ensure_ascii=True` rather than canonical JSON: install trees can hold non-UTF-8 or non-NFC file
names, which canonical JSON rejects, and the escaped form keeps them byte-exact and stable.
Changing the line format changes every toolchain id, so bump `FINGERPRINT_VERSION` when it does.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from ebs.core.digest import Digest, StreamingHasher, hash_file
from ebs.core.errors import ToolchainError
from ebs.core.log import get_logger

FINGERPRINT_VERSION: Final = 1
CONTENT_HASH_SUFFIXES: Final = frozenset({".so", ".sh", ".tcl", ".py"})
ELF_MAGIC: Final = b"\x7fELF"

EntryKind = Literal["file", "dir", "symlink", "other"]
ContentHashPredicate = Callable[[Path], bool]

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class FingerprintEntry:
    """One recorded entry; `path` is relative to `root`, with `/` separators."""

    root: str
    path: str
    kind: EntryKind
    size: int = 0  # files only
    mtime_ns: int = 0  # files only
    executable: bool = False  # files only: any of the u/g/o x bits
    content: Digest | None = None  # files selected by the content-hash predicate
    target: str | None = None  # symlinks only, as stored (not resolved)

    def line(self) -> bytes:
        """The entry's contribution to the fingerprint (one JSON line)."""
        record = [
            self.root,
            self.path,
            self.kind,
            self.size,
            self.mtime_ns,
            self.executable,
            None if self.content is None else str(self.content),
            self.target,
        ]
        return json.dumps(record, ensure_ascii=True, separators=(",", ":")).encode() + b"\n"


def default_content_hash(path: Path) -> bool:
    """Hash contents of executables, ELF objects and `.so`/`.sh`/`.tcl`/`.py` files."""
    if path.suffix in CONTENT_HASH_SUFFIXES:
        return True
    try:
        if os.stat(path).st_mode & 0o111:
            return True
        with open(path, "rb") as f:
            return f.read(len(ELF_MAGIC)) == ELF_MAGIC
    except OSError as exc:
        raise ToolchainError(
            f"cannot read toolchain file {path}: {exc.strerror}; "
            "install trees must be readable by the user registering the toolchain"
        ) from exc


def scan_roots(
    roots: Sequence[Path], *, content_hash: ContentHashPredicate = default_content_hash
) -> list[FingerprintEntry]:
    """Every entry under the install roots, sorted by (root, path); duplicate roots count once."""
    if not roots:
        raise ToolchainError(
            "a toolchain fingerprint needs at least one install root; "
            "list the tool's installation directories in install_roots"
        )
    entries: list[FingerprintEntry] = []
    for root in sorted({_check_root(r) for r in roots}):
        entries.extend(_scan_root(root, content_hash))
    entries.sort(key=lambda e: (e.root, e.path))
    return entries


def fingerprint_roots(
    roots: Sequence[Path], *, content_hash: ContentHashPredicate = default_content_hash
) -> Digest:
    """Digest of `scan_roots(roots)`; changes when any recorded attribute changes."""
    hasher = StreamingHasher()
    header = json.dumps(["ebs-toolchain-fingerprint", FINGERPRINT_VERSION])
    hasher.update(header.encode() + b"\n")
    entries = scan_roots(roots, content_hash=content_hash)
    for entry in entries:
        hasher.update(entry.line())
    digest = hasher.finish()[0]
    _log.debug("toolchain fingerprint", roots=[str(r) for r in roots], entries=len(entries))
    return digest


def _check_root(root: Path) -> str:
    if not root.is_absolute():
        raise ToolchainError(
            f"toolchain install root {str(root)!r} must be an absolute path; "
            "resolve it against the toolchain config file first"
        )
    if not root.exists():
        raise ToolchainError(
            f"toolchain install root {root} does not exist; "
            "check the toolchain's install_roots (is the tool installed on this host?)"
        )
    if not root.is_dir():
        raise ToolchainError(f"toolchain install root {root} is not a directory")
    return os.path.normpath(root)


def _scan_root(root: str, content_hash: ContentHashPredicate) -> list[FingerprintEntry]:
    entries: list[FingerprintEntry] = []
    pending: list[tuple[str, str]] = [(root, "")]  # (absolute dir, relative prefix)
    while pending:
        directory, prefix = pending.pop()
        try:
            with os.scandir(directory) as it:
                names = [e.name for e in it]
        except OSError as exc:
            raise _unreadable(directory, exc) from exc
        for name in names:
            abs_path = os.path.join(directory, name)
            rel = prefix + name
            try:
                st = os.lstat(abs_path)
                mode = st.st_mode
                if stat.S_ISLNK(mode):
                    entries.append(
                        FingerprintEntry(root, rel, "symlink", target=os.readlink(abs_path))
                    )
                elif stat.S_ISDIR(mode):
                    entries.append(FingerprintEntry(root, rel, "dir"))
                    pending.append((abs_path, rel + "/"))
                elif stat.S_ISREG(mode):
                    content = None
                    if content_hash(Path(abs_path)):
                        content = hash_file(Path(abs_path))[0]
                    entries.append(
                        FingerprintEntry(
                            root,
                            rel,
                            "file",
                            size=st.st_size,
                            mtime_ns=st.st_mtime_ns,
                            executable=bool(mode & 0o111),
                            content=content,
                        )
                    )
                else:  # FIFO, socket, device: recorded by type, never opened
                    entries.append(FingerprintEntry(root, rel, "other"))
            except OSError as exc:
                raise _unreadable(abs_path, exc) from exc
    return entries


def _unreadable(path: str, exc: OSError) -> ToolchainError:
    return ToolchainError(
        f"cannot fingerprint toolchain path {path}: {exc.strerror}; "
        "install trees must be readable by the user registering the toolchain"
    )
