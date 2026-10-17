"""Cross-domain export of a release (P2-02 R4).

`ebs release export NAME VERSION --to DOMAIN` copies every released blob and tree manifest from
the source domain's CAS into the target domain's CAS (byte copies through the target's atomic
write path, never links, so the target's group permissions apply to its own inodes), then
creates the release in the target domain with `exported_from` set and an audit row
(`record_export`). Objects already in the target are kept (content addressing). The caller
must belong to both domains (`DomainAccess`); by default that is the Unix group owning each
domain's CAS directory (mode 2750, group `<domain>`), the same check the OS makes on the files.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Protocol

from ebs.cas.fs import FsCAS
from ebs.core.digest import Digest
from ebs.core.errors import CasError, ReleaseError
from ebs.meta.api import BlobRef, MetadataStore, ReleaseStore, ReleaseView

__all__ = ["DomainAccess", "UnixGroupAccess", "copy_objects", "export_release"]


class DomainAccess(Protocol):
    def is_member(self, domain: str) -> bool: ...


class UnixGroupAccess:
    """Membership = the caller owns `<cas root>/<domain>` or is in its group."""

    def __init__(
        self,
        cas_root: Path,
        *,
        uid: Callable[[], int] = os.getuid,
        groups: Callable[[], Iterable[int]] = lambda: {os.getgid(), *os.getgroups()},
    ) -> None:
        self._root = cas_root
        self._uid = uid
        self._groups = groups

    def is_member(self, domain: str) -> bool:
        try:
            st = (self._root / domain).stat()
        except OSError:
            return False
        return st.st_uid == self._uid() or st.st_gid in set(self._groups())


class _Exporter(MetadataStore, ReleaseStore, Protocol):
    pass


def copy_objects(src: FsCAS, dst: FsCAS, digest: Digest, kind: str, size: int) -> list[BlobRef]:
    """Copy one file or tree (manifests after their children, I9) from `src` to `dst`; returns
    every object copied or already there, for `register_blobs` (a tree's size is the total of
    the file bytes below it, as the runner registers it)."""
    if kind == "file":
        if not dst.has(digest):
            dst.put_file(src.blob_path(digest), expected=digest)
        return [BlobRef(digest=digest, size=size, kind="file")]
    manifest = src.get_tree(digest)
    refs: list[BlobRef] = []
    for entry in manifest.entries:
        if entry.digest is None:
            continue  # a symlink
        child = "tree" if entry.type == "dir" else "file"
        refs += copy_objects(src, dst, entry.digest, child, entry.size)
    dst.put_manifest(manifest)
    refs.append(BlobRef(digest=digest, size=size, kind="tree"))
    return refs


def export_release(
    store: _Exporter,
    release: ReleaseView,
    *,
    src: FsCAS,
    dst: FsCAS,
    access: DomainAccess,
    by: str,
) -> ReleaseView:
    """Copy `release`'s objects into `dst.domain` and record the exported release there."""
    target = dst.domain
    if src.domain != release.domain:
        raise ReleaseError(
            f"export: the source CAS is domain {src.domain!r}, the release is in {release.domain!r}"
        )
    if target == release.domain:
        raise ReleaseError(f"export: release {release.name} is already in domain {target!r}")
    if release.yanked_at is not None:
        raise ReleaseError(
            f"release {release.name} {release.version} is yanked ({release.yank_reason}); a "
            "yanked release is not exported"
        )
    done = store.get_release(target, release.name, release.version)
    if done is not None and done.exported_from == release.id:
        return done  # exported before (e.g. a retry after a lost response)
    missing = [d for d in (release.domain, target) if not access.is_member(d)]
    if missing:
        raise ReleaseError(
            f"export of {release.name} {release.version} to {target!r} needs membership in "
            f"both domains; you are not a member of {', '.join(map(repr, missing))} (ask the "
            "domain admins for the Unix group)"
        )
    refs: list[BlobRef] = []
    for name, out in sorted(release.outputs.items()):
        try:
            refs += copy_objects(src, dst, out.digest, out.type, out.size)
        except CasError as exc:
            raise CasError(
                f"export of {release.name} {release.version}: cannot copy output {name} "
                f"({out.digest}): {exc}"
            ) from exc
    store.register_blobs(target, refs)
    return store.record_export(release.id, target, by=by)
