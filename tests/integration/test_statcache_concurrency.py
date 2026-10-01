"""R9: several `ebs` processes share one stat cache database."""

from __future__ import annotations

import multiprocessing as mp
from pathlib import Path

from ebs.core.digest import hash_bytes
from ebs.sources.statcache import StatCache
from tests.helpers.sources import fake_stat
from tests.helpers.statcache_workers import ENTRIES, hammer_statcache

WORKERS = 6


# R9
def test_concurrent_writers(tmp_path: Path) -> None:
    db = tmp_path / "statcache.sqlite"
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(WORKERS)
    results = ctx.Queue()
    procs = [
        ctx.Process(target=hammer_statcache, args=(str(db), w, barrier, results))
        for w in range(WORKERS)
    ]
    for p in procs:
        p.start()
    outcomes = [results.get(timeout=120) for _ in procs]
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0
    assert outcomes == ["ok"] * WORKERS

    with StatCache(db) as cache:
        for w in range(WORKERS):
            for i in range(ENTRIES):
                path = Path(f"/w{w}/f{i}.sv")
                assert cache.lookup(fake_stat(ino=i, size=w), path) == hash_bytes(
                    f"{w}/{i}".encode()
                )
