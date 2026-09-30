"""Place CAS objects on local scratch: copies, hard links or read-only symlinks (R7).

Generic over any `CAS`: backends without local paths (S3 later) always copy from `open()`.
Tree manifests read back from a CAS were never checked against a filesystem, so their symlinks
are untrusted: targets that would leave `dest` (directly or through a chain of the tree's own
links) are rejected before any link exists, symlinks are created last, nothing is ever written
through a symlink, and on any failure the partial result is removed.
"""

from __future__ import annotations

import errno
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, get_args

from ebs.cas.api import CAS, DEFAULT_COPY_THRESHOLD, MaterializeMode, ObjectKind
from ebs.core.digest import Digest
from ebs.core.errors import CasError
from ebs.core.tree import TreeManifest

_MODES: Final[frozenset[str]] = frozenset(get_args(MaterializeMode))
_KINDS: Final[frozenset[str]] = frozenset(get_args(ObjectKind))
_CHUNK: Final = 1 << 20
_MAX_HOPS: Final = 40  # like the kernel's MAXSYMLINKS
# os.link failures meaning "this filesystem pair cannot hard-link": stop trying for this call.
_NO_LINKS: Final = frozenset({errno.EXDEV, errno.ENOTSUP, errno.EOPNOTSUPP})
_PER_FILE: Final = frozenset({errno.EMLINK, errno.EPERM})
# (writable, executable) -> mode of a copied file
_COPY_MODES: Final[dict[tuple[bool, bool], int]] = {
    (False, False): 0o444,
    (False, True): 0o555,
    (True, False): 0o644,
    (True, True): 0o755,
}


@dataclass
class _Ctx:
    cas: CAS
    mode: MaterializeMode
    threshold: int
    writable: bool
    dest: Path
    created: bool = False  # dest itself was created by this call (so cleanup may remove it)
    can_link: bool = True
    manifests: dict[Digest, TreeManifest] = field(default_factory=dict)
    links: dict[tuple[str, ...], str] = field(default_factory=dict)  # tree path -> target


def materialize(
    cas: CAS,
    d: Digest,
    kind: ObjectKind,
    dest: Path,
    mode: MaterializeMode = "auto",
    copy_threshold: int = DEFAULT_COPY_THRESHOLD,
    *,
    writable: bool = False,
) -> None:
    """Materialize blob or tree `d` at `dest`, which must not exist yet (see `CAS.materialize`)."""
    if mode not in _MODES:
        raise CasError(f"invalid materialize mode {mode!r}; expected one of {sorted(_MODES)}")
    if kind not in _KINDS:
        raise CasError(f"invalid materialize kind {kind!r}; expected one of {sorted(_KINDS)}")
    if type(copy_threshold) is not int or copy_threshold < 0:
        raise CasError(f"copy_threshold must be a non-negative byte count, got {copy_threshold!r}")
    if os.path.lexists(dest):
        raise CasError(
            f"cannot materialize {d} at {dest}: it already exists; materialize into a fresh path"
        )
    ctx = _Ctx(cas, mode, copy_threshold, writable, dest)
    try:
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if kind == "file":
                _place_file(ctx, d, dest, size=None, executable=False, in_tree=False)
            else:
                _place_tree(ctx, d, dest)
        except OSError as exc:
            raise CasError(
                f"cannot materialize {d} at {dest}: {exc.strerror or exc} "
                f"({exc.filename or dest}); check free space and permissions on the scratch disk"
            ) from exc
    except BaseException:
        if ctx.created:  # never remove what someone else put at dest meanwhile
            _remove(dest)
        raise


def _place_tree(ctx: _Ctx, d: Digest, dest: Path) -> None:
    root = _manifest(ctx, d)  # fetched first, so a missing tree creates nothing
    os.mkdir(dest, 0o755)
    ctx.created = True
    _fill(ctx, root, dest, ())
    # Every link is checked before any is created. Resolution is logical (no disk access): only
    # the tree's own links are followed; files, including symlinks into the CAS, are terminal.
    for rel, target in ctx.links.items():
        if _resolve(rel[:-1], target, ctx.links) is None:
            raise CasError(
                f"tree {d}: symlink {'/'.join(rel)} -> {target!r} escapes the tree (directly or "
                "through other symlinks); the tree manifest cannot be materialized safely"
            )
    for rel, target in ctx.links.items():
        os.symlink(target, dest.joinpath(*rel))


def _manifest(ctx: _Ctx, d: Digest) -> TreeManifest:
    manifest = ctx.manifests.get(d)
    if manifest is None:
        manifest = ctx.manifests[d] = ctx.cas.get_tree(d)
    return manifest


def _fill(ctx: _Ctx, manifest: TreeManifest, path: Path, rel: tuple[str, ...]) -> None:
    for entry in manifest.entries:
        child = path / entry.name
        if entry.type == "dir":
            assert entry.digest is not None  # guaranteed by TreeEntry
            sub = _manifest(ctx, entry.digest)
            os.mkdir(child, 0o755)
            _fill(ctx, sub, child, (*rel, entry.name))
        elif entry.type == "file":
            assert entry.digest is not None  # guaranteed by TreeEntry
            _place_file(
                ctx, entry.digest, child, size=entry.size, executable=entry.executable, in_tree=True
            )
        else:
            assert entry.target is not None  # guaranteed by TreeEntry
            ctx.links[(*rel, entry.name)] = entry.target


class _TooManyLinks(Exception):
    pass


def _resolve(
    base: tuple[str, ...], target: str, links: dict[tuple[str, ...], str]
) -> tuple[str, ...] | None:
    """Where `target`, read in directory `base`, lands inside the tree; None if it leaves it.

    Follows the tree's own links as the kernel does ('..' after a link applies to the link's
    resolved location), with the kernel's budget of 40 link follows per lookup in total. A lookup
    over budget fails with ELOOP on disk, so it cannot escape. The shared budget also keeps a
    crafted manifest (links whose targets name other links many times) from taking exponential
    time.
    """
    try:
        return _walk(base, target, links, [_MAX_HOPS])
    except _TooManyLinks:
        return base


def _walk(
    base: tuple[str, ...], target: str, links: dict[tuple[str, ...], str], budget: list[int]
) -> tuple[str, ...] | None:
    cur = base
    for part in target.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if not cur:
                return None
            cur = cur[:-1]
            continue
        cur = (*cur, part)
        if cur in links:
            budget[0] -= 1
            if budget[0] < 0:
                raise _TooManyLinks
            resolved = _walk(cur[:-1], links[cur], links, budget)
            if resolved is None:
                return None
            cur = resolved
    return cur


def _place_file(
    ctx: _Ctx, d: Digest, dest: Path, *, size: int | None, executable: bool, in_tree: bool
) -> None:
    # Executables are copied: a link would share the blob's inode and mode (0444), and
    # making it executable would change the shared object for everyone.
    if ctx.writable or executable:
        _copy(ctx, d, dest, executable)
        return
    local = ctx.cas.local_path(d)
    if ctx.mode != "auto":
        if ctx.mode == "copy":
            _copy(ctx, d, dest, executable)
            return
        if local is None:
            raise CasError(
                f"cannot {ctx.mode} blob {d} to {dest}: it has no local path in CAS domain "
                f"{ctx.cas.domain!r} (missing, or the backend is not a local filesystem); "
                "use mode='copy' or 'auto'"
            )
        _link(ctx.mode == "hardlink", local, dest)
        return
    if local is None:
        _copy(ctx, d, dest, executable)
        return
    if in_tree and ctx.can_link:
        try:
            os.link(local, dest)
            return
        except OSError as exc:
            if exc.errno in _NO_LINKS:
                ctx.can_link = False
            # EMLINK: this blob is at its link limit. EPERM: protected_hardlinks refuses a blob
            # owned by another user. Both are per file: fall back for this one only.
            elif exc.errno not in _PER_FILE:
                raise
    if size is None:
        size = os.stat(local).st_size
    if size <= ctx.threshold:
        _copy(ctx, d, dest, executable)
    else:
        _link(False, local, dest)


def _link(hard: bool, local: Path, dest: Path) -> None:
    if hard:
        os.link(local, dest)
    else:
        os.symlink(local, dest)


def _copy(ctx: _Ctx, d: Digest, dest: Path, executable: bool) -> None:
    final = _COPY_MODES[ctx.writable, executable]
    with ctx.cas.open(d) as src:
        # O_EXCL | O_NOFOLLOW: never replace or write through anything already at dest.
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        if dest == ctx.dest:
            ctx.created = True
        with open(fd, "wb", closefd=True) as out:
            shutil.copyfileobj(src, out, _CHUNK)
            os.fchmod(fd, final)


def _remove(dest: Path) -> None:
    if os.path.isdir(dest) and not os.path.islink(dest):
        shutil.rmtree(dest, ignore_errors=True)
    elif os.path.lexists(dest):
        os.unlink(dest)
