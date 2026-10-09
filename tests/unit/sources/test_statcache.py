"""Stat cache: key fields and racy-clean rule (R4, I6), bypasses (R5), sampled audit (R6)."""

from __future__ import annotations

import os
import random
import sqlite3
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from ebs.core.clock import FakeClock, SystemClock
from ebs.core.digest import hash_bytes
from ebs.core.errors import SourceError
from ebs.sources.gitids import GitIds
from ebs.sources.snapshot import SourceSnapshotter
from ebs.sources.statcache import StatCache, default_statcache_path
from tests.helpers.sources import CountingCAS, CountingGit, fake_stat, git, write

D1 = hash_bytes(b"one")
D2 = hash_bytes(b"two")
NS = 10**9
T = 1_700_000_000  # recorded_at used by the key tests, far from their mtimes


def _later(seconds: float = 60) -> FakeClock:
    clock = FakeClock(SystemClock().now())
    clock.advance(seconds)
    return clock


@pytest.fixture
def cache(tmp_path: Path) -> Iterator[StatCache]:
    with StatCache(tmp_path / "statcache.sqlite") as c:
        yield c


BASE = {"dev": 5, "ino": 77, "size": 123, "mtime_ns": (T - 100) * NS, "ctime_ns": (T - 90) * NS}
KEY_FIELDS = [
    ("dev", {"dev": 6}),
    ("ino", {"ino": 78}),
    ("size", {"size": 124}),
    ("mtime_ns", {"mtime_ns": (T - 100) * NS + 1}),
    ("ctime_ns", {"ctime_ns": (T - 90) * NS + 1}),
]


# R4
@pytest.mark.parametrize(("field", "change"), KEY_FIELDS, ids=[f for f, _ in KEY_FIELDS])
def test_key_fields(cache: StatCache, field: str, change: dict[str, int]) -> None:
    path = Path("/src/a.sv")
    cache.store(fake_stat(**BASE), path, D1, recorded_at=T)
    assert cache.lookup(fake_stat(**BASE), path) == D1
    assert cache.lookup(fake_stat(**{**BASE, **change}), path) is None, field


# R4
def test_key_field_path(cache: StatCache) -> None:
    cache.store(fake_stat(**BASE), Path("/src/a.sv"), D1, recorded_at=T)
    assert cache.lookup(fake_stat(**BASE), Path("/src/b.sv")) is None
    assert cache.lookup(fake_stat(**BASE), Path("/src/a.sv")) == D1


# R4
def test_store_replaces_entry(cache: StatCache) -> None:
    path = Path("/src/a.sv")
    cache.store(fake_stat(**BASE), path, D1, recorded_at=T)
    newer = fake_stat(**{**BASE, "mtime_ns": (T - 50) * NS})
    cache.store(newer, path, D2, recorded_at=T)
    assert cache.lookup(newer, path) == D2
    assert cache.lookup(fake_stat(**BASE), path) is None


# R4 (I6)
@pytest.mark.parametrize(
    ("mtime_off", "ctime_off", "hit"),
    [
        (-10.0, -10.0, True),  # well before recording: clean
        (-3.5, -3.5, True),  # just outside the 3 s window
        (-2.9, -2.9, False),  # mtime within the window: racy
        (-10.0, -1.0, False),  # ctime within the window: racy too
        (-1.0, -100.0, False),  # mtime alone within the window (utime to a skewed time)
        (0.0, 0.0, False),  # same instant
        (5.0, 5.0, False),  # in the future (clock skew): racy
    ],
)
def test_racy_clean(tmp_path: Path, mtime_off: float, ctime_off: float, hit: bool) -> None:
    with StatCache(tmp_path / "c.sqlite", racy_window_s=3) as cache:
        st = fake_stat(mtime_ns=int((T + mtime_off) * NS), ctime_ns=int((T + ctime_off) * NS))
        cache.store(st, Path("/a"), D1, recorded_at=T)
        assert (cache.lookup(st, Path("/a")) == D1) is hit


# R4 (I6): a racy entry is re-recorded after a rehash and becomes clean later
def test_racy_entry_rehashed_then_trusted(tmp_path: Path) -> None:
    f = write(tmp_path / "src" / "a.sv", "v1")
    clock = FakeClock(SystemClock().now())  # recording right when the file was written
    with StatCache(tmp_path / "c.sqlite") as cache:
        cas = CountingCAS(tmp_path / "cas")
        snap = SourceSnapshotter(cas, cache, clock=clock, git=None, audit_fraction=0)
        snap.snapshot_file(f)
        snap.snapshot_file(f)
        assert len(cas.puts) == 2  # racy: not trusted
        clock.advance(60)
        snap.snapshot_file(f)  # rehashed and recorded 60 s after the mtime
        snap.snapshot_file(f)
        assert len(cas.puts) == 3


# R4 (I6)
def test_same_mtime_different_content(tmp_path: Path) -> None:
    f = write(tmp_path / "src" / "a.sv", "module a; endmodule\n")
    with StatCache(tmp_path / "c.sqlite") as cache:
        cas = CountingCAS(tmp_path / "cas")
        # The real clock: the edit below may land in the same timestamp tick as the first
        # write (same ctime too), and only the racy-clean rule can tell the entries apart.
        snap = SourceSnapshotter(cas, cache, clock=SystemClock(), git=None, audit_fraction=0)
        first = snap.snapshot_file(f)
        st = f.stat()
        # recorded right when the file was written, so the entry is racy: never trusted
        assert cache.lookup(st, f) is None
        f.write_bytes(b"module b; endmodule\n")  # same size
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))  # same mtime
        assert f.stat().st_mtime_ns == st.st_mtime_ns
        assert f.stat().st_size == st.st_size
        second = snap.snapshot_file(f)
    assert first == hash_bytes(b"module a; endmodule\n")
    assert second == hash_bytes(b"module b; endmodule\n")


def _poisoned(tmp_path: Path, cache: StatCache, cas: CountingCAS, rel: str = "a.sv") -> Path:
    """A file whose non-racy stat entry names a wrong digest that is present in the CAS."""
    f = write(tmp_path / "src" / rel, "real content")
    wrong = cas.put_bytes(b"stale content")
    cache.store(f.stat(), f, wrong, recorded_at=_later().now().timestamp())
    cas.puts.clear()
    return f


# R5
def test_rehash_bypass(tmp_path: Path) -> None:
    with StatCache(tmp_path / "c.sqlite") as cache:
        cas = CountingCAS(tmp_path / "cas")
        f = _poisoned(tmp_path, cache, cas)
        trusting = SourceSnapshotter(cas, cache, clock=_later(), git=None, audit_fraction=0)
        assert trusting.snapshot_file(f) == hash_bytes(b"stale content")  # the cache decides
        strict = SourceSnapshotter(cas, cache, clock=_later(), git=None, rehash=True)
        assert strict.snapshot_file(f) == hash_bytes(b"real content")
        assert cas.puts == [f]
        # the fresh digest replaced the stale entry
        assert cache.lookup(f.stat(), f) == hash_bytes(b"real content")


# R5: rehash also bypasses the git blob map
def test_rehash_bypasses_git(git_repo: Path, tmp_path: Path) -> None:
    blob = git(git_repo, "hash-object", "a.sv").strip()
    size = (git_repo / "a.sv").stat().st_size
    with StatCache(tmp_path / "c.sqlite") as cache:
        cas = CountingCAS(tmp_path / "cas")
        wrong = cas.put_bytes(b"x" * size)
        cache.store_git_blob(blob, wrong, size)
        trusting = SourceSnapshotter(cas, cache, clock=_later(), audit_fraction=0)
        assert trusting.snapshot_file(git_repo / "a.sv") == wrong
        strict = SourceSnapshotter(cas, cache, clock=_later(), rehash=True)
        assert strict.snapshot_file(git_repo / "a.sv") == hash_bytes(
            (git_repo / "a.sv").read_bytes()
        )


# R5
def test_untrusted_mount_bypass(tmp_path: Path) -> None:
    with StatCache(tmp_path / "c.sqlite") as cache:
        cas = CountingCAS(tmp_path / "cas")
        inside = _poisoned(tmp_path, cache, cas, "nfs/a.sv")
        outside = _poisoned(tmp_path, cache, cas, "local/a.sv")
        snap = SourceSnapshotter(
            cas,
            cache,
            clock=_later(),
            git=None,
            audit_fraction=0,
            untrusted_mounts=[tmp_path / "src" / "nfs"],
        )
        assert snap.snapshot_file(inside) == hash_bytes(b"real content")
        assert snap.snapshot_file(outside) == hash_bytes(b"stale content")
        assert snap.snapshot(tmp_path / "src", "nfs/*.sv").files == ("nfs/a.sv",)
        assert cas.puts == [inside, inside]


# R5: the mount is compared after resolving symlinks, so an alias does not dodge it
def test_untrusted_mount_through_alias(tmp_path: Path) -> None:
    with StatCache(tmp_path / "c.sqlite") as cache:
        cas = CountingCAS(tmp_path / "cas")
        f = _poisoned(tmp_path, cache, cas, "nfs/a.sv")
        alias = tmp_path / "alias"
        alias.symlink_to(tmp_path / "src" / "nfs")
        later = _later().now().timestamp()
        cache.store((alias / "a.sv").stat(), alias / "a.sv", hash_bytes(b"stale content"), later)
        snap = SourceSnapshotter(
            cas, cache, clock=_later(), git=None, audit_fraction=0, untrusted_mounts=[f.parent]
        )
        assert snap.snapshot_file(alias / "a.sv") == hash_bytes(b"real content")


# R6
def test_audit_detects_and_repairs(tmp_path: Path) -> None:
    with StatCache(tmp_path / "c.sqlite") as cache:
        cas = CountingCAS(tmp_path / "cas")
        f = _poisoned(tmp_path, cache, cas)
        snap = SourceSnapshotter(
            cas, cache, clock=_later(), git=None, audit_fraction=1.0, rng=random.Random(7)
        )
        result = snap.snapshot(tmp_path / "src", "a.sv")
        assert snap.audit_mismatches == [f]
        entry = cas.get_tree(result.digest).entries[0]
        assert entry.digest == hash_bytes(b"real content")  # the correct digest is used
        assert cache.lookup(f.stat(), f) == hash_bytes(b"real content")  # entry replaced
        # a clean audit records nothing more
        snap.snapshot_file(f)
        assert snap.audit_mismatches == [f]


# R6: audits of git-derived ids repair too
def test_audit_covers_git_hits(git_repo: Path, tmp_path: Path) -> None:
    blob = git(git_repo, "hash-object", "a.sv").strip()
    size = (git_repo / "a.sv").stat().st_size
    with StatCache(tmp_path / "c.sqlite") as cache:
        cas = CountingCAS(tmp_path / "cas")
        wrong = cas.put_bytes(b"x" * size)
        cache.store_git_blob(blob, wrong, size)
        snap = SourceSnapshotter(cas, cache, clock=_later(), audit_fraction=1.0)
        real = hash_bytes((git_repo / "a.sv").read_bytes())
        assert snap.snapshot_file(git_repo / "a.sv") == real
        assert snap.audit_mismatches == [git_repo / "a.sv"]
        assert cache.lookup_git_blob(blob) == (real, size)  # the wrong mapping was replaced


# R6
@pytest.mark.parametrize(("fraction", "low", "high"), [(0.0, 0, 0), (0.2, 20, 60), (1.0, 200, 200)])
def test_audit_fraction(tmp_path: Path, fraction: float, low: int, high: int) -> None:
    for i in range(200):
        write(tmp_path / "src" / f"f{i}.sv", f"{i}")
    with StatCache(tmp_path / "c.sqlite") as cache:
        cas = CountingCAS(tmp_path / "cas")
        SourceSnapshotter(cas, cache, clock=_later(), git=None, audit_fraction=0).snapshot(
            tmp_path / "src", "*.sv"
        )
        cas.puts.clear()
        snap = SourceSnapshotter(
            cas, cache, clock=_later(), git=None, audit_fraction=fraction, rng=random.Random(1)
        )
        snap.snapshot(tmp_path / "src", "*.sv")
        assert low <= len(cas.puts) <= high
        assert snap.audit_mismatches == []


@pytest.mark.parametrize("fraction", [-0.1, 1.5])
def test_audit_fraction_validated(tmp_path: Path, fraction: float) -> None:
    with StatCache(tmp_path / "c.sqlite") as cache, pytest.raises(SourceError, match="audit"):
        SourceSnapshotter(
            CountingCAS(tmp_path / "cas"), cache, clock=_later(), audit_fraction=fraction
        )


# R9
def test_wal_and_busy_timeout(tmp_path: Path) -> None:
    db = tmp_path / "c.sqlite"
    with StatCache(db) as cache:
        cache.store(fake_stat(), Path("/a"), D1, recorded_at=T)
        assert cache._db.execute("PRAGMA busy_timeout").fetchone()[0] == 30_000
    con = sqlite3.connect(db)
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        con.close()


def test_persists_across_instances(tmp_path: Path) -> None:
    db = tmp_path / "deep" / "c.sqlite"
    with StatCache(db) as cache:
        cache.store(fake_stat(**BASE), Path("/a"), D1, recorded_at=T)
        cache.store_git_blob("ab" * 20, D2, 3)
    with StatCache(db) as cache:
        assert cache.lookup(fake_stat(**BASE), Path("/a")) == D1
        assert cache.lookup_git_blob("ab" * 20) == (D2, 3)
        assert cache.lookup_git_blob("cd" * 20) is None


def test_old_schema_is_discarded(tmp_path: Path) -> None:
    db = tmp_path / "c.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE stat_entries (x)")
    con.execute("PRAGMA user_version = 999")
    con.commit()
    con.close()
    with StatCache(db) as cache:
        cache.store(fake_stat(), Path("/a"), D1, recorded_at=T)
        assert cache.lookup(fake_stat(), Path("/a")) == D1


def test_corrupt_db_is_actionable(tmp_path: Path) -> None:
    db = tmp_path / "c.sqlite"
    db.write_bytes(b"this is not a database" * 100)
    with pytest.raises(SourceError, match="delete it"):
        StatCache(db)


def test_invalid_digest_row_is_a_miss(tmp_path: Path) -> None:
    db = tmp_path / "c.sqlite"
    with StatCache(db) as cache:
        cache.store(fake_stat(**BASE), Path("/a"), D1, recorded_at=T)
        cache.store_git_blob("ab" * 20, D2, 3)
    con = sqlite3.connect(db)
    con.execute("UPDATE stat_entries SET digest = 'garbage'")
    con.execute("UPDATE git_blobs SET digest = 'garbage'")
    con.commit()
    con.close()
    with StatCache(db) as cache:
        assert cache.lookup(fake_stat(**BASE), Path("/a")) is None
        assert cache.lookup_git_blob("ab" * 20) is None


def test_default_path() -> None:
    # Local disk per user, never ~/.cache (often NFS, where WAL is unsafe): P0-16 R7.
    assert default_statcache_path(1000) == Path("/var/tmp/ebs-1000/statcache.sqlite")


def test_racy_window_validated(tmp_path: Path) -> None:
    with pytest.raises(SourceError, match="racy_window_s"):
        StatCache(tmp_path / "c.sqlite", racy_window_s=-1)


# R6: auditing a correct git mapping reports nothing and keeps the mapping
def test_audit_of_correct_git_hit_is_clean(git_repo: Path, tmp_path: Path) -> None:
    blob = git(git_repo, "hash-object", "a.sv").strip()
    with StatCache(tmp_path / "c.sqlite") as cache:
        cas = CountingCAS(tmp_path / "cas")
        SourceSnapshotter(cas, cache, clock=_later(), audit_fraction=0).snapshot_file(
            git_repo / "a.sv"
        )
        learned = cache.lookup_git_blob(blob)
        assert learned is not None
        snap = SourceSnapshotter(cas, cache, clock=_later(), audit_fraction=1.0)
        assert snap.snapshot_file(git_repo / "a.sv") == learned[0]
        assert snap.audit_mismatches == []
        assert cache.lookup_git_blob(blob) == learned


# R6: a wrong mapping that cannot be re-learned from this checkout (filtered bytes) is dropped
def test_audit_drops_unverifiable_git_mapping(git_repo: Path, tmp_path: Path) -> None:
    fake_blob = "1" * 40
    size = (git_repo / "a.sv").stat().st_size

    class FilteredGit(CountingGit):
        def __call__(self, argv: Sequence[str], cwd: Path) -> bytes:
            out = super().__call__(argv, cwd)
            real = git(git_repo, "hash-object", "a.sv").strip().encode()
            return out.replace(real, fake_blob.encode()) if "ls-files" in argv else out

    with StatCache(tmp_path / "c.sqlite") as cache:
        cas = CountingCAS(tmp_path / "cas")
        wrong = cas.put_bytes(b"y" * size)
        cache.store_git_blob(fake_blob, wrong, size)
        snap = SourceSnapshotter(
            cas, cache, clock=_later(), git=GitIds(runner=FilteredGit()), audit_fraction=1.0
        )
        assert snap.snapshot_file(git_repo / "a.sv") == hash_bytes((git_repo / "a.sv").read_bytes())
        assert snap.audit_mismatches == [git_repo / "a.sv"]
        assert cache.lookup_git_blob(fake_blob) is None


# R4 (I6): the window boundary, in integer nanoseconds
def test_racy_boundary(tmp_path: Path) -> None:
    with StatCache(tmp_path / "c.sqlite", racy_window_s=3) as cache:
        edge = fake_stat(mtime_ns=(T - 3) * NS)
        inside = fake_stat(mtime_ns=(T - 3) * NS + 1, ino=9)
        cache.store(edge, Path("/edge"), D1, recorded_at=T)
        cache.store(inside, Path("/inside"), D1, recorded_at=T)
        assert cache.lookup(edge, Path("/edge")) == D1
        assert cache.lookup(inside, Path("/inside")) is None


# R4: a zero window is allowed and trusts anything recorded after the last change
def test_racy_window_zero(tmp_path: Path) -> None:
    with StatCache(tmp_path / "c.sqlite", racy_window_s=0) as cache:
        st = fake_stat(mtime_ns=T * NS - 1)
        cache.store(st, Path("/a"), D1, recorded_at=T)
        assert cache.lookup(st, Path("/a")) == D1


# R4 (I6): recording starts before the read, so an edit during a slow read stays racy
def test_recorded_at_taken_before_read(tmp_path: Path) -> None:
    f = write(tmp_path / "src" / "a.sv", "v1")
    clock = FakeClock(SystemClock().now())
    with StatCache(tmp_path / "c.sqlite") as cache:
        cas = CountingCAS(tmp_path / "cas")
        cas.before_put = lambda _path: clock.advance(10)  # the read takes 10 s
        snap = SourceSnapshotter(cas, cache, clock=clock, git=None, audit_fraction=0)
        snap.snapshot_file(f)
        cas.before_put = None
        snap.snapshot_file(f)
        assert len(cas.puts) == 2


# R5: an untrusted mount named through a symlink alias still covers the real path
def test_untrusted_mount_given_as_alias(tmp_path: Path) -> None:
    with StatCache(tmp_path / "c.sqlite") as cache:
        cas = CountingCAS(tmp_path / "cas")
        f = _poisoned(tmp_path, cache, cas, "nfs/a.sv")
        mnt = tmp_path / "mnt"
        mnt.symlink_to(tmp_path / "src" / "nfs")
        snap = SourceSnapshotter(
            cas, cache, clock=_later(), git=None, audit_fraction=0, untrusted_mounts=[mnt]
        )
        assert snap.snapshot_file(f) == hash_bytes(b"real content")


# R9: several processes creating the database together (WAL switch returns BUSY at once)
def test_locked_open_retries_then_explains(tmp_path: Path) -> None:
    db = tmp_path / "c.sqlite"
    StatCache(db).close()
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    clock = FakeClock()
    try:
        with pytest.raises(SourceError, match="locked by another ebs process"):
            StatCache(db, busy_timeout_s=0.05, clock=clock)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert clock.monotonic() >= 0.05  # it waited (retried) until the timeout
    with StatCache(db, busy_timeout_s=0.05, clock=clock) as cache:  # free again
        cache.store(fake_stat(), Path("/a"), D1, recorded_at=T)


# CLAUDE.md errors: sqlite failures during use never escape as raw sqlite3 errors
def test_io_errors_are_misses_not_crashes(tmp_path: Path) -> None:
    class Broken:
        def execute(self, *args: object) -> None:
            raise sqlite3.OperationalError("disk I/O error")

        def close(self) -> None:
            pass

    with StatCache(tmp_path / "c.sqlite") as cache:
        real = cache._db
        cache._db = Broken()  # type: ignore[assignment]
        try:
            assert cache.lookup(fake_stat(), Path("/a")) is None
            assert cache.lookup_git_blob("ab" * 20) is None
            cache.store(fake_stat(), Path("/a"), D1, recorded_at=T)
            cache.store_git_blob("ab" * 20, D1, 1)
            cache.forget_git_blob("ab" * 20)
        finally:
            cache._db = real
