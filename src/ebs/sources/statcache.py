"""Persistent stat cache: skip rehashing unchanged files, never decide a rerun (invariant I6).

An entry maps a file's stat key `(device, inode, size, mtime_ns, ctime_ns, path)` to the digest
of its content. A lookup hits only when every field is equal and the entry is not *racy*: if
the file's mtime or ctime lies within `racy_window_s` of the moment the entry was recorded (or
after it, for clock skew), a later edit in the same timestamp tick could be invisible, so the
entry is treated as a miss and the file rehashed, as git does for its index.

The same SQLite database holds the `git blob id -> (sha256, size)` map used by the git guard.

SQLite runs in WAL mode with a busy timeout so several `ebs` processes on one host can share it
(R9). WAL needs shared memory: keep the database on a local filesystem, not on NFS.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from types import TracebackType
from typing import Any, Final

from ebs.core.clock import Clock, SystemClock
from ebs.core.digest import Digest
from ebs.core.errors import DigestError, SourceError
from ebs.core.log import get_logger

_log = get_logger(__name__)

SCHEMA_VERSION: Final = 1
DEFAULT_RACY_WINDOW_S: Final = 3.0
_NS: Final = 1_000_000_000

_SCHEMA: Final = (
    """CREATE TABLE IF NOT EXISTS stat_entries (
        path TEXT PRIMARY KEY,
        dev INTEGER NOT NULL,
        ino INTEGER NOT NULL,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        ctime_ns INTEGER NOT NULL,
        digest TEXT NOT NULL,
        recorded_at_ns INTEGER NOT NULL
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS git_blobs (
        blob TEXT PRIMARY KEY,
        digest TEXT NOT NULL,
        size INTEGER NOT NULL
    ) WITHOUT ROWID""",
)


def default_statcache_path(env: Mapping[str, str]) -> Path:
    """`$XDG_CACHE_HOME/ebs/statcache.sqlite`, else `~/.cache/ebs/statcache.sqlite`."""
    home = env.get("HOME") or os.path.expanduser("~")
    cache_home = env.get("XDG_CACHE_HOME") or os.path.join(home, ".cache")
    return Path(cache_home) / "ebs" / "statcache.sqlite"


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def _parse(text: str) -> Digest | None:
    try:
        return Digest.parse(text)
    except DigestError:
        return None  # a damaged row is a miss, never an error


class StatCache:
    """The stat cache database at `db_path` (created on first use)."""

    def __init__(
        self,
        db_path: Path,
        *,
        racy_window_s: float = DEFAULT_RACY_WINDOW_S,
        busy_timeout_s: float = 30.0,
        clock: Clock | None = None,
    ) -> None:
        if racy_window_s < 0:
            raise SourceError(f"stat cache racy_window_s must be >= 0, got {racy_window_s}")
        self._racy_ns = int(racy_window_s * _NS)
        self._busy_ms = int(busy_timeout_s * 1000)
        self._path = db_path
        clock = clock or SystemClock()
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(db_path, timeout=busy_timeout_s, isolation_level=None)
        except (sqlite3.DatabaseError, OSError) as exc:
            raise self._open_error(exc) from exc
        # Switching a new database to WAL returns SQLITE_BUSY at once, ignoring the busy
        # timeout, when another process is doing the same: retry until the timeout.
        deadline = clock.monotonic() + busy_timeout_s
        delay = 0.01
        while True:
            try:
                self._init_schema()
                return
            except sqlite3.OperationalError as exc:
                if not _is_busy(exc) or clock.monotonic() >= deadline:
                    self._db.close()
                    raise self._open_error(exc) from exc
            except sqlite3.DatabaseError as exc:
                self._db.close()
                raise self._open_error(exc) from exc
            clock.sleep(delay)
            delay = min(delay * 2, 0.5)

    def _open_error(self, exc: Exception) -> SourceError:
        if isinstance(exc, sqlite3.OperationalError) and _is_busy(exc):
            return SourceError(
                f"the stat cache {self._path} stayed locked by another ebs process ({exc}); "
                "retry, or point the stat cache elsewhere"
            )
        return SourceError(
            f"cannot open the stat cache {self._path}: {exc}; it only speeds up hashing, so "
            "delete it (it is rebuilt automatically) or point the stat cache elsewhere"
        )

    def _init_schema(self) -> None:
        self._db.execute(f"PRAGMA busy_timeout = {self._busy_ms}")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute("PRAGMA synchronous = NORMAL")
        self._db.execute("BEGIN IMMEDIATE")
        try:
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                # A cache: an unknown layout is dropped, never migrated.
                for table in ("stat_entries", "git_blobs"):
                    self._db.execute(f"DROP TABLE IF EXISTS {table}")
                self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            for ddl in _SCHEMA:
                self._db.execute(ddl)
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def _query(self, sql: str, args: tuple[object, ...]) -> tuple[Any, ...] | None:
        """One row, or None; a failing read is a miss (the cache never breaks a plan)."""
        try:
            row: tuple[Any, ...] | None = self._db.execute(sql, args).fetchone()
        except sqlite3.Error as exc:
            _log.warning("sources.statcache_read_failed", db=str(self._path), error=str(exc))
            return None
        return row

    def _write(self, sql: str, args: tuple[object, ...]) -> None:
        """A failing write only costs a rehash next time, so it is logged, not raised."""
        try:
            self._db.execute(sql, args)
        except sqlite3.Error as exc:
            _log.warning("sources.statcache_write_failed", db=str(self._path), error=str(exc))

    # -- stat entries -------------------------------------------------------------------------

    def lookup(self, st: os.stat_result, path: Path) -> Digest | None:
        """The recorded digest if the stat key is unchanged and the entry is not racy."""
        row = self._query(
            "SELECT dev, ino, size, mtime_ns, ctime_ns, digest, recorded_at_ns "
            "FROM stat_entries WHERE path = ?",
            (os.fspath(path),),
        )
        if row is None:
            return None
        dev, ino, size, mtime_ns, ctime_ns, digest, recorded_at_ns = row
        if (dev, ino, size, mtime_ns, ctime_ns) != (
            st.st_dev,
            st.st_ino,
            st.st_size,
            st.st_mtime_ns,
            st.st_ctime_ns,
        ):
            return None
        if max(mtime_ns, ctime_ns) > recorded_at_ns - self._racy_ns:
            return None  # racy: an edit within the same timestamp tick could be invisible
        return _parse(digest)

    def store(self, st: os.stat_result, path: Path, d: Digest, recorded_at: float) -> None:
        """Record `d` for this stat key; `recorded_at` is when reading the file began (s)."""
        self._write(
            "INSERT OR REPLACE INTO stat_entries "
            "(path, dev, ino, size, mtime_ns, ctime_ns, digest, recorded_at_ns) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                os.fspath(path),
                st.st_dev,
                st.st_ino,
                st.st_size,
                st.st_mtime_ns,
                st.st_ctime_ns,
                str(d),
                int(recorded_at * _NS),
            ),
        )

    # -- git blob map -------------------------------------------------------------------------

    def lookup_git_blob(self, blob: str) -> tuple[Digest, int] | None:
        """The (digest, size) of the content whose git blob id is `blob`, if known."""
        row = self._query("SELECT digest, size FROM git_blobs WHERE blob = ?", (blob,))
        if row is None:
            return None
        digest = _parse(row[0])
        return None if digest is None else (digest, row[1])

    def store_git_blob(self, blob: str, d: Digest, size: int) -> None:
        self._write(
            "INSERT OR REPLACE INTO git_blobs (blob, digest, size) VALUES (?, ?, ?)",
            (blob, str(d), size),
        )

    def forget_git_blob(self, blob: str) -> None:
        self._write("DELETE FROM git_blobs WHERE blob = ?", (blob,))

    # -- lifecycle ----------------------------------------------------------------------------

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> StatCache:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
