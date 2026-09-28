"""Tree manifests: Merkle content identity for directories (docs/design/interfaces.md § 2).

Each directory is a `TreeManifest` of sorted entries, encoded as canonical JSON and identified by
its digest; a sub-directory entry carries the digest of its own manifest. Only names, content,
the user-execute bit and symlink targets are recorded (invariant I5): never mtime, owner, other
permission bits or enumeration order.

The field set maps onto REAPI `Directory` (files/directories/symlinks, `is_executable`), but a
REAPI backend must re-derive two things: our dir `size` is the total of file bytes below it (REAPI
uses the size of the serialized child), and manifest digests are over canonical JSON, not protobuf.
"""

from __future__ import annotations

import itertools
import os
import stat
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from ebs.core.canon import digest_json
from ebs.core.digest import Algo, Digest
from ebs.core.errors import DigestError, TreeError
from ebs.core.types import JsonValue

EntryType = Literal["file", "dir", "symlink"]
Hasher = Callable[[Path], tuple[Digest, int]]

MANIFEST_VERSION: Final = 1
_ENTRY_TYPES: Final[frozenset[str]] = frozenset({"file", "dir", "symlink"})
_KEYS: Final[dict[str, frozenset[str]]] = {
    "file": frozenset({"name", "type", "digest", "size", "executable"}),
    "dir": frozenset({"name", "type", "digest", "size"}),
    "symlink": frozenset({"name", "type", "target"}),
}


def _check_text(value: str, what: str, fix: str) -> None:
    """Shared checks for names and symlink targets: UTF-8 encodable, NUL-free, NFC."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise TreeError(f"{what} {value!r} is not valid UTF-8; {fix} with a UTF-8 name") from None
    if "\x00" in value:
        raise TreeError(f"{what} {value!r} contains a NUL character")
    if not unicodedata.is_normalized("NFC", value):
        raise TreeError(
            f"{what} {value!r} is not NFC-normalized; {fix} with the NFC form "
            f"(unicodedata.normalize('NFC', …) gives {unicodedata.normalize('NFC', value)!r})"
        )


@dataclass(frozen=True, slots=True)
class TreeEntry:
    """One directory entry; invalid field combinations raise TreeError on construction."""

    name: str  # single path component, NFC, no "/" or NUL, not "." / ".."
    type: EntryType
    digest: Digest | None  # file: content digest; dir: child tree digest; symlink: None
    size: int  # file: bytes; dir: total bytes below; symlink: 0
    executable: bool  # files only; other mode bits are not recorded
    target: str | None  # symlink only; must be relative and stay inside the tree

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or self.name in {"", ".", ".."} or "/" in self.name:
            raise TreeError(
                f"invalid entry name {self.name!r}: expected one non-empty path component "
                "other than '.' and '..'"
            )
        _check_text(self.name, "entry name", "rename it")
        if self.type not in _ENTRY_TYPES:
            raise TreeError(
                f"entry {self.name!r} has unknown type {self.type!r}; "
                f"expected one of {sorted(_ENTRY_TYPES)}"
            )
        # type() rather than isinstance(): bool is an int subclass and would serialize as true.
        if type(self.size) is not int or self.size < 0:
            raise TreeError(f"entry {self.name!r} has invalid size {self.size!r}")
        if type(self.executable) is not bool:
            raise TreeError(f"entry {self.name!r} has non-boolean executable {self.executable!r}")
        if self.type == "symlink":
            self._check_symlink()
        else:
            if not isinstance(self.digest, Digest):
                raise TreeError(f"{self.type} entry {self.name!r} needs a digest")
            if self.target is not None:
                raise TreeError(f"{self.type} entry {self.name!r} must not have a target")
            if self.type == "dir" and self.executable:
                raise TreeError(f"dir entry {self.name!r} must not be marked executable")

    def _check_symlink(self) -> None:
        # Fields that to_json does not serialize must be fixed, or equal digests would hide
        # unequal entries.
        if self.digest is not None or self.size != 0 or self.executable:
            raise TreeError(
                f"symlink entry {self.name!r} must have no digest, size 0 and executable false"
            )
        if not isinstance(self.target, str) or not self.target:
            raise TreeError(f"symlink entry {self.name!r} needs a non-empty target")
        _check_text(self.target, f"symlink {self.name!r} target", "re-create the symlink")
        if self.target.startswith("/"):
            raise TreeError(
                f"symlink entry {self.name!r} has absolute target {self.target!r}; "
                "only relative targets that stay inside the tree are allowed"
            )

    def to_json(self) -> dict[str, JsonValue]:
        if self.type == "symlink":
            return {"name": self.name, "type": "symlink", "target": self.target}
        doc: dict[str, JsonValue] = {
            "name": self.name,
            "type": self.type,
            "digest": str(self.digest),
            "size": self.size,
        }
        if self.type == "file":
            doc["executable"] = self.executable
        return doc

    @classmethod
    def from_json(cls, doc: JsonValue) -> TreeEntry:
        """Parse one entry; field types and consistency are checked by the constructor."""
        if not isinstance(doc, dict):
            raise TreeError(f"tree entry must be a JSON object, got {type(doc).__name__}")
        kind = doc.get("type")
        if not isinstance(kind, str) or kind not in _KEYS:
            raise TreeError(f"tree entry {doc.get('name')!r} has unknown type {kind!r}")
        if set(doc) != _KEYS[kind]:
            raise TreeError(
                f"{kind} entry {doc.get('name')!r} has keys {sorted(doc)}, "
                f"expected exactly {sorted(_KEYS[kind])}"
            )
        name = doc["name"]
        if kind == "symlink":
            return cls(name, "symlink", None, 0, False, doc["target"])  # type: ignore[arg-type]
        digest_text = doc["digest"]
        if not isinstance(digest_text, str):
            raise TreeError(f"{kind} entry {name!r} digest must be a string, got {digest_text!r}")
        try:
            digest = Digest.parse(digest_text)
        except DigestError as exc:
            raise TreeError(f"{kind} entry {name!r}: {exc}") from None
        executable = doc["executable"] if kind == "file" else False
        return cls(
            name,  # type: ignore[arg-type]
            "file" if kind == "file" else "dir",
            digest,
            doc["size"],  # type: ignore[arg-type]
            executable,  # type: ignore[arg-type]
            None,
        )


@dataclass(frozen=True, slots=True)
class TreeManifest:
    """The sorted entries of one directory."""

    entries: tuple[TreeEntry, ...]  # sorted by name (bytewise UTF-8), unique

    def __post_init__(self) -> None:
        keys = [e.name.encode("utf-8") for e in self.entries]
        for prev, cur in itertools.pairwise(keys):
            if prev >= cur:
                raise TreeError(
                    "tree manifest entries must be sorted by UTF-8 bytes with no duplicate "
                    f"names: {prev.decode()!r} is followed by {cur.decode()!r}"
                )

    def to_json(self) -> JsonValue:
        return {"v": MANIFEST_VERSION, "entries": [e.to_json() for e in self.entries]}

    @classmethod
    def from_json(cls, doc: JsonValue) -> TreeManifest:
        if not isinstance(doc, dict) or set(doc) != {"v", "entries"}:
            raise TreeError("tree manifest must be a JSON object with exactly 'v' and 'entries'")
        # type() check: true and 1.0 compare equal to 1 but would re-encode to other bytes.
        if type(doc["v"]) is not int or doc["v"] != MANIFEST_VERSION:
            raise TreeError(
                f"unsupported tree manifest version {doc['v']!r}; this ebs reads version "
                f"{MANIFEST_VERSION}"
            )
        entries = doc["entries"]
        if not isinstance(entries, list):
            raise TreeError("tree manifest 'entries' must be a list")
        return cls(entries=tuple(TreeEntry.from_json(e) for e in entries))

    def digest(self, algo: Algo = "sha256") -> Digest:
        return digest_json(self.to_json(), algo)


def build_tree(root: Path, hasher: Hasher) -> tuple[Digest, dict[Digest, TreeManifest]]:
    """Hash the directory `root` into manifests; returns the root digest and all manifests.

    Regular files are hashed with `hasher` (injected so the stat cache can plug in). Symlinks are
    recorded, never followed, and must resolve inside `root` (also through chains of links).
    Special files are rejected. If `root` itself is a symlink to a directory, it is followed.
    """
    if not root.is_dir():
        raise TreeError(f"cannot build a tree from {root}: not a directory")
    manifests: dict[Digest, TreeManifest] = {}
    real_root = os.path.realpath(root)
    try:
        digest, _ = _build_dir(root, (), real_root, hasher, manifests)
    except RecursionError:
        raise TreeError(
            f"cannot build a tree from {root}: directories are nested too deeply"
        ) from None
    return digest, manifests


def _build_dir(
    path: Path,
    rel: tuple[str, ...],
    real_root: str,
    hasher: Hasher,
    manifests: dict[Digest, TreeManifest],
) -> tuple[Digest, int]:
    try:
        with os.scandir(path) as it:
            # Code-point order equals UTF-8 byte order; non-UTF-8 names are rejected below.
            children = sorted(it, key=lambda e: e.name)
    except OSError as exc:
        raise _io_error(path, exc) from exc
    entries: list[TreeEntry] = []
    total = 0
    for child in children:
        child_path = path / child.name
        try:
            st = child.stat(follow_symlinks=False)
            if stat.S_ISLNK(st.st_mode):
                target = os.readlink(child_path)
                _check_link_target(child_path, target, rel, real_root)
                entry = _entry(child_path, child.name, "symlink", None, 0, False, target)
            elif stat.S_ISDIR(st.st_mode):
                digest, size = _build_dir(
                    child_path, (*rel, child.name), real_root, hasher, manifests
                )
                entry = _entry(child_path, child.name, "dir", digest, size, False, None)
            elif stat.S_ISREG(st.st_mode):
                digest, size = hasher(child_path)
                executable = bool(st.st_mode & stat.S_IXUSR)
                entry = _entry(child_path, child.name, "file", digest, size, executable, None)
            else:
                raise TreeError(
                    f"{child_path} is a special file ({_kind(st.st_mode)}); tree inputs and "
                    "outputs may only contain regular files, directories and symlinks"
                )
        except OSError as exc:
            raise _io_error(child_path, exc) from exc
        entries.append(entry)
        total += entry.size
    manifest = TreeManifest(entries=tuple(entries))
    # Manifests are always sha256 (the contract has no algo here), whatever the file hasher uses.
    digest = manifest.digest()
    manifests[digest] = manifest
    return digest, total


def _io_error(path: Path, exc: OSError) -> TreeError:
    return TreeError(
        f"{path}: cannot read ({exc.strerror or exc}); check its permissions and that the tree "
        "is not modified while it is being hashed"
    )


def _entry(
    path: Path,
    name: str,
    kind: EntryType,
    digest: Digest | None,
    size: int,
    executable: bool,
    target: str | None,
) -> TreeEntry:
    try:
        return TreeEntry(name, kind, digest, size, executable, target)
    except TreeError as exc:
        raise TreeError(f"{path}: {exc}") from None


def _check_link_target(link: Path, target: str, rel: tuple[str, ...], real_root: str) -> None:
    """Reject targets that leave the tree: lexically, then after resolving chains of links."""
    depth = len(rel)
    for part in target.split("/"):
        if part in {"", "."}:
            continue
        depth = depth - 1 if part == ".." else depth + 1
        if depth < 0:
            raise TreeError(
                f"symlink {link} has target {target!r}, which escapes the tree root; "
                "make it point inside the tree"
            )
    # Each link may stay inside lexically while a chain escapes (sub/up -> .., x -> sub/up/..).
    # realpath follows the chain on disk (dangling tails are resolved lexically).
    resolved = os.path.realpath(link)
    if not target.startswith("/") and os.path.commonpath([real_root, resolved]) != real_root:
        raise TreeError(
            f"symlink {link} has target {target!r}, which resolves to {resolved} outside the "
            "tree root through other symlinks; make it point inside the tree"
        )


def _kind(mode: int) -> str:
    if stat.S_ISFIFO(mode):
        return "FIFO"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        return "device"
    return f"mode {stat.filemode(mode)}"
