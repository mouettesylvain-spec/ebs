"""Action logs for the dashboard (P2-06 R3): `GET /builds/{build}/actions/{action}/log`.

A finished action's log is the CAS blob its ResultManifest names; until then (or for a run that
posted no result), the runner's live log `<debug root>/<domain>/<build>/<action>/live.log`
(decision D2) is served. The route reads a byte range (`offset`, at most `LOG_CHUNK_MAX`), so a
client tails a live log by asking again from the size it has, and a large log is fetched in
pieces. The service account reads these roots as a member of each served domain's group (P2-01
R5); the route authorizes as a read of the build first, so the files are never consulted for a
build the caller may not see.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ebs.cas.fs import FsCAS
from ebs.core.digest import Digest
from ebs.core.errors import CasError
from ebs.runner.collect import debug_action_dir

__all__ = ["LIVE_LOG_NAME", "LogChunk", "LogSource"]

LIVE_LOG_NAME = "live.log"  # ebs.runner.main: the debug dir's live log


@dataclass(frozen=True)
class LogChunk:
    """`data` is the log's bytes from `offset`; `size` is the whole log's size now. A live log
    is `complete=False`: it may still grow (or be replaced by the stored log)."""

    source: Literal["cas", "live"]
    data: bytes
    offset: int
    size: int
    complete: bool


class LogSource:
    """Reads logs under the CAS and debug roots of each domain (`[[domains]]`, `[cas]`,
    `[debug]`), as the runner wrote them."""

    def __init__(
        self,
        cas_root_for: Callable[[str], Path],
        debug_root_for: Callable[[str], Path | None],
    ) -> None:
        self._cas_root_for = cas_root_for
        self._debug_root_for = debug_root_for

    def read(
        self,
        domain: str,
        build: int,
        action_id: str,
        log: Digest | None,
        *,
        offset: int,
        limit: int,
    ) -> LogChunk | None:
        """The chunk of the stored log `log`, else of the live log; None if there is neither."""
        if log is not None:
            cas_root = self._cas_root_for(domain)
            path = FsCAS(cas_root, domain).blob_path(log)
            chunk = _read(Path(os.path.abspath(cas_root)) / domain, path, offset, limit)
            if chunk is not None:
                return LogChunk("cas", chunk[0], offset, chunk[1], complete=True)
        root = self._debug_root_for(domain)
        if root is None:
            return None
        live = debug_action_dir(root, domain, str(build), action_id) / LIVE_LOG_NAME
        chunk = _read(root / domain, live, offset, limit)
        if chunk is None:
            return None
        return LogChunk("live", chunk[0], offset, chunk[1], complete=False)


def _open_under(base: Path, path: Path) -> int | None:
    """An fd of the regular file `path` below `base` (the domain's own dir, made by an admin),
    opened without following any symlink below `base`: a user who owns a debug or shard dir
    must not point it at another domain's files, which the service account can read (I20).
    None if the file is missing, not a regular file (a FIFO would block), or a link."""
    try:
        parts = path.relative_to(base).parts
    except ValueError:
        return None
    if not parts or any(p in ("", ".", "..") for p in parts):
        return None
    flags_dir = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        for part in parts[:-1]:
            nxt = os.open(part, flags_dir, dir_fd=fd)
            os.close(fd)
            fd = nxt
        leaf = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd
        )
    except OSError:
        return None
    finally:
        os.close(fd)
    if not stat.S_ISREG(os.fstat(leaf).st_mode):
        os.close(leaf)
        return None
    return leaf


def _read(base: Path, path: Path, offset: int, limit: int) -> tuple[bytes, int] | None:
    """(bytes from `offset`, file size) of `path` below `base`; None if it cannot be opened."""
    fd = _open_under(base, path)
    if fd is None:
        return None
    try:
        size = os.fstat(fd).st_size
        if offset >= size:
            return b"", size
        parts: list[bytes] = []
        at, left = offset, min(limit, size - offset)
        while left > 0:
            part = os.pread(fd, left, at)
            if not part:
                break
            parts.append(part)
            at += len(part)
            left -= len(part)
        return b"".join(parts), size
    except OSError as exc:
        raise CasError(f"cannot read the log {path}: {exc}") from None
    finally:
        os.close(fd)
