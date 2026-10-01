"""Process entry points for stat cache concurrency tests (importable by spawned children)."""

from __future__ import annotations

from multiprocessing.queues import Queue
from multiprocessing.synchronize import Barrier
from pathlib import Path

from ebs.core.digest import hash_bytes
from ebs.sources.statcache import StatCache
from tests.helpers.sources import fake_stat

ENTRIES = 300
_RECORDED_AT = 2_000_000_000.0  # far after fake_stat's mtime, so entries are not racy


def hammer_statcache(db: str, worker: int, barrier: Barrier, results: Queue[str]) -> None:
    """Interleave writes and reads of this worker's entries with everyone else's."""
    try:
        barrier.wait()  # every worker opens (and creates) the database at the same moment
        with StatCache(Path(db)) as cache:
            for i in range(ENTRIES):
                path = Path(f"/w{worker}/f{i}.sv")
                st = fake_stat(ino=i, size=worker)
                cache.store(st, path, hash_bytes(f"{worker}/{i}".encode()), _RECORDED_AT)
                cache.store_git_blob(f"{i:040x}", hash_bytes(f"{i}".encode()), i)
                if cache.lookup(st, path) != hash_bytes(f"{worker}/{i}".encode()):
                    results.put(f"error: lost write {path}")
                    return
        results.put("ok")
    except Exception as exc:  # reported to the parent instead of a silent exit code
        results.put(f"error: {exc!r}")
