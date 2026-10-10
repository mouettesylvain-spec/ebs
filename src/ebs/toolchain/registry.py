"""Registered toolchains: immutable snapshots in the metadata store (P1-07).

`register` fingerprints a snapshot's install roots and stores it under its toolchain id. Ids are
immutable: registering identical contents again is a no-op, and different contents (a patched
install, a changed modulefile) give a new id that supersedes the module's previous one, with a
drift report of the changed files from the stored fingerprint manifests. `RegistryToolchainResolver`
gives the planner the module's current id, and the plan records it, so old plans and builds keep
pointing at the snapshot they used. `check` re-fingerprints recently used toolchains and reports
drift without changing anything.

A manifest is the zlib-compressed fingerprint lines (`FingerprintEntry.line()`), one per entry.
"""

from __future__ import annotations

import json
import os
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, Literal

from ebs.core.digest import Digest
from ebs.core.errors import DigestError, ToolchainError
from ebs.core.log import get_logger
from ebs.meta.api import MetadataStore, ToolchainCreate, ToolchainRecord
from ebs.toolchain.fingerprint import (
    ContentHashPredicate,
    FingerprintEntry,
    default_content_hash,
    fingerprint_entries,
    scan_roots,
)
from ebs.toolchain.model import Toolchain
from ebs.toolchain.modules import check_module_name

__all__ = [
    "DRIFT_LIST_LIMIT",
    "CheckResult",
    "FileChange",
    "Registration",
    "RegistryToolchainResolver",
    "check",
    "decode_manifest",
    "diff_manifests",
    "encode_manifest",
    "format_drift",
    "register",
    "to_toolchain",
]

DRIFT_LIST_LIMIT: Final = 20
"""Changed files kept and shown per drift report; the total is always counted."""

_log = get_logger(__name__)

ManifestKey = tuple[str, str]  # (root, path)


@dataclass(frozen=True, slots=True)
class FileChange:
    path: str  # absolute: root + "/" + relative path
    change: Literal["added", "removed", "changed"]


@dataclass(frozen=True, slots=True)
class Registration:
    """What `register` did. `previous` is the module's current id before the call."""

    toolchain: Toolchain
    status: Literal["registered", "unchanged", "reinstated"]
    previous: Digest | None
    superseded: tuple[Digest, ...]
    changes: tuple[FileChange, ...] = ()  # vs `previous`, first DRIFT_LIST_LIMIT by path
    changes_total: int = 0
    env_changes: tuple[str, ...] = ()  # variable names that differ from `previous`
    manifest_missing: bool = False  # `previous` has no stored manifest: files unknown

    @property
    def drifted(self) -> bool:
        return self.previous is not None and self.previous != self.toolchain.id


@dataclass(frozen=True, slots=True)
class CheckResult:
    record: ToolchainRecord
    status: Literal["ok", "drift", "error"]
    fingerprint: Digest | None = None  # the fingerprint now (None on error)
    changes: tuple[FileChange, ...] = ()
    changes_total: int = 0
    error: str | None = None
    manifest_missing: bool = False  # drift, but no usable stored manifest: files unknown


def encode_manifest(entries: Sequence[FingerprintEntry]) -> bytes:
    return zlib.compress(b"".join(e.line() for e in entries))


def decode_manifest(data: bytes) -> dict[ManifestKey, FingerprintEntry]:
    """Entries of a stored manifest by (root, path); ToolchainError if it is corrupt."""
    try:
        lines = zlib.decompress(data).splitlines()
        out: dict[ManifestKey, FingerprintEntry] = {}
        for line in lines:
            root, path, kind, size, mtime_ns, executable, content, target = json.loads(line)
            out[(root, path)] = FingerprintEntry(
                root,
                path,
                kind,
                size=size,
                mtime_ns=mtime_ns,
                executable=executable,
                content=None if content is None else Digest.parse(content),
                target=target,
            )
        return out
    except (zlib.error, ValueError, TypeError, DigestError) as exc:
        raise ToolchainError(
            f"stored toolchain manifest is corrupt ({exc}); the drift file list is unavailable"
        ) from None


def diff_manifests(
    old: Mapping[ManifestKey, FingerprintEntry], new: Mapping[ManifestKey, FingerprintEntry]
) -> tuple[tuple[FileChange, ...], int]:
    """(first DRIFT_LIST_LIMIT changes sorted by path, total number of changes)."""
    changes: list[FileChange] = []
    for key in old.keys() | new.keys():
        path = f"{key[0].rstrip('/')}/{key[1]}"
        if key not in old:
            changes.append(FileChange(path, "added"))
        elif key not in new:
            changes.append(FileChange(path, "removed"))
        elif old[key] != new[key]:
            changes.append(FileChange(path, "changed"))
    changes.sort(key=lambda c: c.path)
    return tuple(changes[:DRIFT_LIST_LIMIT]), len(changes)


def register(
    store: MetadataStore,
    *,
    module: str,
    version: str,
    env: Mapping[str, str],
    install_roots: Sequence[Path],
    user: str,
    content_hash: ContentHashPredicate = default_content_hash,
) -> Registration:
    """Fingerprint the roots and store the snapshot as `module`'s current toolchain (R1, R2)."""
    check_module_name(module)
    roots = tuple(sorted({Path(os.path.normpath(r)) for r in install_roots}))
    entries = scan_roots(roots, content_hash=content_hash)
    tc = Toolchain.from_parts(
        name=module.rpartition("/")[0],
        module=module,
        version=version,
        install_roots=roots,
        env=env,
        fingerprint=fingerprint_entries(entries),
    )
    outcome = store.register_toolchain(
        ToolchainCreate(
            id=tc.id,
            name=tc.name,
            module=module,
            version=version,
            fingerprint=tc.fingerprint,
            env=dict(tc.env),
            install_roots=[str(r) for r in roots],
            registered_by=user,
        ),
        encode_manifest(entries),
    )
    status: Literal["registered", "unchanged", "reinstated"] = (
        "registered" if outcome.inserted else "reinstated" if outcome.superseded else "unchanged"
    )
    # The predecessor comes from the store's own transaction, so concurrent registrations of
    # the module cannot make the drift report compare against the wrong one.
    previous = None
    if outcome.superseded:
        previous = store.get_toolchain(outcome.superseded[-1])
    reg = Registration(
        tc,
        status,
        tc.id if status == "unchanged" else None if previous is None else previous.id,
        tuple(outcome.superseded),
    )
    if previous is not None:
        reg = _with_drift(store, reg, previous, entries)
        _log.warning(
            "toolchain drift",
            module=module,
            old=str(previous.id),
            new=str(tc.id),
            files=reg.changes_total,
            env=list(reg.env_changes),
        )
    return reg


def _with_drift(
    store: MetadataStore,
    reg: Registration,
    previous: ToolchainRecord,
    entries: Sequence[FingerprintEntry],
) -> Registration:
    env = reg.toolchain.env
    env_changes = tuple(
        sorted(k for k in previous.env.keys() | env.keys() if previous.env.get(k) != env.get(k))
    )
    old = _stored_manifest(store, previous.id)
    if old is None:
        return _replace(reg, env_changes=env_changes, manifest_missing=True)
    changes, total = diff_manifests(old, {(e.root, e.path): e for e in entries})
    return _replace(reg, changes=changes, changes_total=total, env_changes=env_changes)


def _stored_manifest(
    store: MetadataStore, toolchain_id: Digest
) -> dict[ManifestKey, FingerprintEntry] | None:
    """The decoded manifest, or None if absent or corrupt (a drift list is best effort)."""
    data = store.toolchain_manifest(toolchain_id)
    if data is None:
        return None
    try:
        return decode_manifest(data)
    except ToolchainError as exc:
        _log.warning("unusable toolchain manifest", toolchain=str(toolchain_id), error=str(exc))
        return None


def _replace(reg: Registration, **fields: object) -> Registration:
    values: dict[str, object] = {name: getattr(reg, name) for name in reg.__slots__}
    values.update(fields)
    return Registration(**values)  # type: ignore[arg-type]


def format_drift(reg: Registration) -> str | None:
    """The drift warning for a registration that changed the module's id, else None (R2)."""
    if not reg.drifted:
        return None
    lines = [
        f"toolchain {reg.toolchain.module} changed since it was last registered: "
        f"new id {reg.toolchain.id} supersedes {reg.previous}"
    ]
    lines += _change_lines(reg.changes, reg.changes_total, missing=reg.manifest_missing)
    if reg.env_changes:
        lines.append(f"  environment changed: {', '.join(reg.env_changes)}")
    lines.append(
        "  New plans use the new id; existing plans and builds keep the old one. If the "
        "install was not meant to change, restore it and register again."
    )
    return "\n".join(lines)


def _change_lines(changes: Sequence[FileChange], total: int, *, missing: bool) -> list[str]:
    if missing:
        return ["  changed files unknown: the previous registration has no stored manifest"]
    if not total:
        return []
    lines = [f"  {total} file(s) changed under the install roots:"]
    lines += [f"    {c.change}: {c.path}" for c in changes]
    if total > len(changes):
        lines.append(f"    ... and {total - len(changes)} more")
    return lines


def format_check(result: CheckResult) -> list[str]:
    """Human-readable lines for one `check` result."""
    rec = result.record
    if result.status == "ok":
        return [f"ok     {rec.module}  {rec.id}"]
    if result.status == "error":
        return [f"error  {rec.module}  {rec.id}: {result.error}"]
    lines = [
        f"drift  {rec.module}  {rec.id}: install tree fingerprint is now {result.fingerprint} "
        f"(registered {rec.fingerprint}); run `ebs toolchain register {rec.module}` to snapshot it"
    ]
    return lines + _change_lines(
        result.changes, result.changes_total, missing=result.manifest_missing
    )


def check(
    store: MetadataStore,
    *,
    days: int,
    now: datetime,
    content_hash: ContentHashPredicate = default_content_hash,
) -> list[CheckResult]:
    """Re-fingerprint the current toolchains used (or registered) in the last `days` days (R4).

    Read-only: it never registers, supersedes or touches anything, so ids never change.
    """
    if days < 0:
        raise ToolchainError(f"--days must be zero or more, got {days}")
    cutoff = now - timedelta(days=days)
    results: list[CheckResult] = []
    for record in store.list_toolchains(current_only=True):
        if (record.last_used_at or record.registered_at) < cutoff:
            continue
        try:
            entries = scan_roots([Path(r) for r in record.install_roots], content_hash=content_hash)
        except ToolchainError as exc:
            results.append(CheckResult(record, "error", error=str(exc)))
            continue
        fingerprint = fingerprint_entries(entries)
        if fingerprint == record.fingerprint:
            results.append(CheckResult(record, "ok", fingerprint))
            continue
        old = _stored_manifest(store, record.id)
        if old is None:
            results.append(CheckResult(record, "drift", fingerprint, manifest_missing=True))
            continue
        changes, total = diff_manifests(old, {(e.root, e.path): e for e in entries})
        results.append(CheckResult(record, "drift", fingerprint, changes, total))
    return results


def to_toolchain(record: ToolchainRecord, name: str) -> Toolchain:
    """The `Toolchain` of a stored row; ToolchainError if the row no longer matches its id."""
    tc = Toolchain.from_parts(
        name=name,
        module=record.module,
        version=record.version,
        install_roots=[Path(r) for r in record.install_roots],
        env=record.env,
        fingerprint=record.fingerprint,
    )
    if tc.id != record.id:
        raise ToolchainError(
            f"registered toolchain {record.id} ({record.module}) does not match its stored "
            f"contents (they hash to {tc.id}); the registry row was modified. Register the "
            "module again"
        )
    return tc


class RegistryToolchainResolver:
    """`ToolchainResolver` over the registry: a module's current id, fixed per resolver (R3)."""

    def __init__(self, store: MetadataStore) -> None:
        self._store = store
        self._current: dict[str, ToolchainRecord] = {}

    def resolve(self, name: str, module: str) -> Toolchain:
        record = self._current.get(module)
        if record is None:
            record = self._store.current_toolchain(module)
            if record is None:
                raise ToolchainError(
                    f"toolchain {name!r}: module {module!r} is not registered; run "
                    f"`ebs toolchain register {module}` on a host with the module system"
                )
            self._store.touch_toolchain(record.id)
            self._current[module] = record
        return to_toolchain(record, name)

    def get(self, toolchain_id: Digest, *, name: str) -> Toolchain:
        """The exact snapshot a plan recorded, superseded or not."""
        record = self._store.get_toolchain(toolchain_id)
        if record is None:
            raise ToolchainError(
                f"toolchain {toolchain_id} is not registered in this metadata store; was the "
                "plan made against another site?"
            )
        return to_toolchain(record, name)
