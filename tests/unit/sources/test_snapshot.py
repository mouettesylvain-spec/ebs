"""Source snapshots: tree layout, hash-while-copy (R7) and zero reads when unchanged (R8)."""

from __future__ import annotations

import os
import random
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from ebs.cas.fs import FsCAS
from ebs.core.clock import FakeClock, SystemClock
from ebs.core.digest import Digest, hash_bytes, hash_file
from ebs.core.errors import SourceError, SourceEscapeError
from ebs.core.tree import TreeManifest, build_tree
from ebs.sources.snapshot import SnapshotResult, SourceSnapshotter
from ebs.sources.statcache import StatCache
from tests.helpers.sources import CountingCAS, fake_stat, write


def _later(seconds: float = 60) -> FakeClock:
    clock = FakeClock(SystemClock().now())
    clock.advance(seconds)
    return clock


def _snap(tmp_path: Path, cache: StatCache, cas: CountingCAS | None = None) -> SourceSnapshotter:
    return SourceSnapshotter(
        cas or CountingCAS(tmp_path / "cas"),
        cache,
        clock=_later(),
        git=None,
        audit_fraction=0,
        rng=random.Random(0),
    )


@pytest.fixture
def cache(tmp_path: Path) -> Iterator[StatCache]:
    with StatCache(tmp_path / "statcache.sqlite") as c:
        yield c


# R7
def test_tree_layout(tmp_path: Path, cache: StatCache) -> None:
    src = tmp_path / "src"
    write(src / "top.sv", "top")
    write(src / "rtl" / "a.sv", "a")
    write(src / "rtl" / "deep" / "b.sv", "bb")
    write(src / "rtl" / "notes.txt", "ignored")
    write(src / "run.sh", "#!/bin/sh\n", executable=True)
    (src / "rtl" / "alias.sv").symlink_to("./a.sv")
    cas = CountingCAS(tmp_path / "cas")
    result = _snap(tmp_path, cache, cas).snapshot(src, "**/*.sv")

    assert result.files == ("rtl/a.sv", "rtl/alias.sv", "rtl/deep/b.sv", "top.sv")
    assert result.size == 6
    out = tmp_path / "out"
    cas.materialize(result.digest, "tree", out, mode="copy")
    assert sorted(str(p.relative_to(out)) for p in out.rglob("*")) == [
        "rtl",
        "rtl/a.sv",
        "rtl/alias.sv",
        "rtl/deep",
        "rtl/deep/b.sv",
        "top.sv",
    ]
    assert os.readlink(out / "rtl" / "alias.sv") == "./a.sv"  # kept as a symlink
    assert (out / "rtl" / "deep" / "b.sv").read_bytes() == b"bb"
    # symlink targets are not read: only the three regular files were stored
    assert sorted(p.name for p in cas.puts) == ["a.sv", "b.sv", "top.sv"]

    exe = _snap(tmp_path, cache, cas).snapshot(src, "run.sh")
    assert cas.get_tree(exe.digest).entries[0].executable


# R7: the same inputs give the same tree digest, whatever the base path
def test_digest_is_location_independent(tmp_path: Path, cache: StatCache) -> None:
    for root in ("one", "two"):
        write(tmp_path / root / "x" / "a.sv", "a")
    snap = _snap(tmp_path, cache)
    assert (
        snap.snapshot(tmp_path / "one", "x/*.sv").digest
        == snap.snapshot(tmp_path / "two", "x/*.sv").digest
    )


# R7
def test_hash_while_copy(tmp_path: Path, cache: StatCache) -> None:
    src = tmp_path / "src"
    f = write(src / "a.sv", "old bytes")
    cas = CountingCAS(tmp_path / "cas")

    def edit(path: Path) -> None:  # an edit that lands just before the bytes are read
        cas.before_put = None
        path.write_bytes(b"new bytes!")

    cas.before_put = edit
    result = _snap(tmp_path, cache, cas).snapshot(src, "a.sv")

    entry = cas.get_tree(result.digest).entries[0]
    # The id is the digest of the bytes that were copied: a snapshotter that hashed the old
    # bytes first and copied later would reference a blob the CAS does not hold.
    assert entry.digest == hash_bytes(b"new bytes!")
    assert entry.size == len(b"new bytes!")
    assert cas.has(entry.digest)
    assert cas.verify(entry.digest)
    assert set(cas.puts) == {f}


# R7: a read that overlapped an edit (stat differs before/after) is repeated, never used
def test_changed_during_read_is_reread(tmp_path: Path, cache: StatCache) -> None:
    f = write(tmp_path / "src" / "a.sv", "v1")
    cas = CountingCAS(tmp_path / "cas")
    original_put = FsCAS.put_file
    edits = [b"v2 longer"]

    def put_then_edit(path: Path, *, expected: Digest | None = None) -> Digest:
        cas.puts.append(path)
        d = original_put(cas, path, expected=expected)
        if edits:
            path.write_bytes(edits.pop())  # lands after the bytes were read
        return d

    cas.put_file = put_then_edit  # type: ignore[method-assign]
    snap = _snap(tmp_path, cache, cas)
    assert snap.snapshot_file(f) == hash_bytes(b"v2 longer")
    assert cas.puts == [f, f]
    assert cache.lookup(f.stat(), f.absolute()) in (None, hash_bytes(b"v2 longer"))


# R7
def test_keeps_changing_is_an_error(tmp_path: Path, cache: StatCache) -> None:
    f = write(tmp_path / "src" / "a.sv", "v1")
    cas = CountingCAS(tmp_path / "cas")
    original_put = FsCAS.put_file
    counter = iter(range(100))

    def put_then_edit(path: Path, *, expected: Digest | None = None) -> Digest:
        d = original_put(cas, path, expected=expected)
        path.write_bytes(b"v" * (3 + next(counter)))  # a new size each time
        return d

    cas.put_file = put_then_edit  # type: ignore[method-assign]
    with pytest.raises(SourceError, match="kept changing"):
        _snap(tmp_path, cache, cas).snapshot_file(f)


# R7
def test_existing_blob_not_rewritten(tmp_path: Path, cache: StatCache) -> None:
    src = tmp_path / "src"
    write(src / "a.sv", "a")
    cas = CountingCAS(tmp_path / "cas")
    _snap(tmp_path, cache, cas).snapshot(src, "a.sv")
    cas.puts.clear()
    _snap(tmp_path, cache, cas).snapshot(src, "a.sv")
    assert cas.puts == []


# R7: a stat hit whose blob is missing from this CAS (e.g. another domain) is uploaded again
def test_missing_blob_is_uploaded(tmp_path: Path, cache: StatCache) -> None:
    src = tmp_path / "src"
    write(src / "a.sv", "a")
    _snap(tmp_path, cache, CountingCAS(tmp_path / "cas1")).snapshot(src, "a.sv")
    other = CountingCAS(tmp_path / "cas2")
    result = _snap(tmp_path, cache, other).snapshot(src, "a.sv")
    assert len(other.puts) == 1
    assert other.has(hash_bytes(b"a"))
    assert other.has(result.digest)


# R1: optional inputs with no match give the empty tree
def test_optional_empty(tmp_path: Path, cache: StatCache) -> None:
    (tmp_path / "src").mkdir()
    cas = CountingCAS(tmp_path / "cas")
    snap = _snap(tmp_path, cache, cas)
    result = snap.snapshot(tmp_path / "src", "*.sv", optional=True)
    assert result == SnapshotResult(TreeManifest(()).digest(), (), 0)
    assert cas.has(result.digest)
    with pytest.raises(SourceError, match="matched no files"):
        snap.snapshot(tmp_path / "src", "*.sv")


# R2
def test_symlink_escaping_lexically_rejected(tmp_path: Path, cache: StatCache) -> None:
    src = tmp_path / "src"
    write(src / "a.sv", "a")
    # resolves inside src, but its text climbs out of the snapshot root first
    (src / "l.sv").symlink_to("../src/a.sv")
    with pytest.raises(SourceEscapeError, match=r"l\.sv"):
        _snap(tmp_path, cache).snapshot(src, "l.sv")


# R2
def test_absolute_symlink_rejected(tmp_path: Path, cache: StatCache) -> None:
    src = tmp_path / "src"
    write(src / "a.sv", "a")
    (src / "l.sv").symlink_to(src / "a.sv")
    with pytest.raises(SourceError, match="absolute"):
        _snap(tmp_path, cache).snapshot(src, "l.sv")


def test_special_file_rejected(tmp_path: Path, cache: StatCache) -> None:
    src = tmp_path / "src"
    src.mkdir()
    os.mkfifo(src / "p.sv")
    with pytest.raises(SourceError, match="regular file"):
        _snap(tmp_path, cache).snapshot(src, "p.sv")
    with pytest.raises(SourceError, match="regular file"):
        _snap(tmp_path, cache).snapshot_file(src / "p.sv")


def test_snapshot_file_missing(tmp_path: Path, cache: StatCache) -> None:
    with pytest.raises(SourceError, match="cannot read"):
        _snap(tmp_path, cache).snapshot_file(tmp_path / "nope.sv")


def test_snapshot_file_stores_blob(tmp_path: Path, cache: StatCache) -> None:
    f = write(tmp_path / "a.sv", "content")
    cas = CountingCAS(tmp_path / "cas")
    d = _snap(tmp_path, cache, cas).snapshot_file(f)
    assert d == hash_bytes(b"content")
    assert cas.has(d)


# R8
@pytest.mark.slow
def test_no_reads_when_unchanged(tmp_path: Path) -> None:
    src = tmp_path / "src"
    for i in range(5000):
        write(src / f"d{i % 50}" / f"s{i // 50}" / f"f{i}.sv", f"module f{i}; endmodule\n")
    db = tmp_path / "statcache.sqlite"
    cas = CountingCAS(tmp_path / "cas")
    with StatCache(db) as cache:
        first = _snap(tmp_path, cache, cas).snapshot(src, "**/*.sv")
    assert len(cas.puts) == 5000
    cas.puts.clear()

    start = time.monotonic()
    with StatCache(db) as cache:  # a new process would open the same database
        again = _snap(tmp_path, cache, cas).snapshot(src, "**/*.sv")
    elapsed = time.monotonic() - start

    assert cas.puts == []  # zero file reads
    assert again == first
    assert elapsed < 3.0, f"re-snapshot of 5,000 unchanged files took {elapsed:.2f} s"


# R7
def test_replaced_by_directory_during_read(tmp_path: Path, cache: StatCache) -> None:
    f = write(tmp_path / "src" / "a.sv", "v1")
    cas = CountingCAS(tmp_path / "cas")
    original_put = FsCAS.put_file

    def put_then_swap(path: Path, *, expected: Digest | None = None) -> Digest:
        d = original_put(cas, path, expected=expected)
        path.unlink()
        path.mkdir()
        return d

    cas.put_file = put_then_swap  # type: ignore[method-assign]
    with pytest.raises(SourceError, match="stopped being a regular file"):
        _snap(tmp_path, cache, cas).snapshot_file(f)


def test_non_nfc_name_rejected(tmp_path: Path, cache: StatCache) -> None:
    write(tmp_path / "src" / "é.sv", "x")  # NFD spelling
    with pytest.raises(SourceError, match="NFC"):
        _snap(tmp_path, cache).snapshot(tmp_path / "src", "*.sv")


BRACKET_FIELDS = [
    ("dev", {"dev": 1}),
    ("ino", {"ino": 1}),
    ("size", {"size": 1}),
    ("mtime_ns", {"mtime_ns": 1}),
    ("ctime_ns", {"ctime_ns": 1}),
    ("mode", {"mode": 0o100}),
]


# R7: every stat field brackets the read; a change in any one of them repeats it
@pytest.mark.parametrize(("field", "bump"), BRACKET_FIELDS, ids=[f for f, _ in BRACKET_FIELDS])
def test_read_bracket_every_field(
    tmp_path: Path,
    cache: StatCache,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    bump: dict[str, int],
) -> None:
    f = write(tmp_path / "src" / "a.sv", "v1")
    real = os.stat(f)
    fields = {
        "dev": real.st_dev,
        "ino": real.st_ino,
        "size": real.st_size,
        "mtime_ns": real.st_mtime_ns,
        "ctime_ns": real.st_ctime_ns,
        "mode": real.st_mode,
    }
    changed = fake_stat(**{**fields, **{k: fields[k] + v for k, v in bump.items()}})
    after_reads = iter([changed])  # the stat after the first read differs only in `field`

    def stat_(path: Path, *, follow: bool) -> os.stat_result:
        return next(after_reads, None) or os.stat(path)

    cas = CountingCAS(tmp_path / "cas")
    snap = _snap(tmp_path, cache, cas)
    monkeypatch.setattr(SourceSnapshotter, "_stat", staticmethod(stat_))
    assert snap.snapshot_file(f) == hash_bytes(b"v1")
    assert len(cas.puts) == 2, field


# R7: stable on the last allowed attempt is accepted
def test_stable_on_last_attempt(tmp_path: Path, cache: StatCache) -> None:
    f = write(tmp_path / "src" / "a.sv", "v1")
    cas = CountingCAS(tmp_path / "cas")
    original_put = FsCAS.put_file
    edits = [b"v" * 4, b"v" * 3]

    def put_then_edit(path: Path, *, expected: Digest | None = None) -> Digest:
        cas.puts.append(path)
        d = original_put(cas, path, expected=expected)
        if edits:
            path.write_bytes(edits.pop())
        return d

    cas.put_file = put_then_edit  # type: ignore[method-assign]
    assert _snap(tmp_path, cache, cas).snapshot_file(f) == hash_bytes(b"v" * 4)
    assert len(cas.puts) == 3


# R7: the executable bit is the user-x bit, as in build_tree/put_tree
def test_exec_bit_matches_put_tree(tmp_path: Path, cache: StatCache) -> None:
    src = tmp_path / "src"
    for name, mode in (("g.x", 0o654), ("n.sv", 0o644), ("u.x", 0o744)):
        write(src / name, name).chmod(mode)
    cas = CountingCAS(tmp_path / "cas")
    result = _snap(tmp_path, cache, cas).snapshot(src, "*")
    assert [e.executable for e in cas.get_tree(result.digest).entries] == [False, False, True]
    assert result.digest == build_tree(src, hash_file)[0]


# R7: a chmod during a re-read is reflected in the tree (mode taken from the accepted read)
def test_chmod_during_read_recorded(tmp_path: Path, cache: StatCache) -> None:
    write(tmp_path / "src" / "a.sv", "v1")
    cas = CountingCAS(tmp_path / "cas")
    original_put = FsCAS.put_file
    pending = [True]

    def put_then_chmod(path: Path, *, expected: Digest | None = None) -> Digest:
        d = original_put(cas, path, expected=expected)
        if pending:
            pending.pop()
            path.chmod(0o755)
            path.write_bytes(b"v22")  # also change size so the stat surely differs
        return d

    cas.put_file = put_then_chmod  # type: ignore[method-assign]
    result = _snap(tmp_path, cache, cas).snapshot(tmp_path / "src", "a.sv")
    entry = cas.get_tree(result.digest).entries[0]
    assert (entry.digest, entry.size, entry.executable) == (hash_bytes(b"v22"), 3, True)


# R2: a relative symlink that climbs within the base is fine
def test_symlink_up_inside_base_allowed(tmp_path: Path, cache: StatCache) -> None:
    src = tmp_path / "src"
    write(src / "top.sv", "t")
    (src / "rtl").mkdir()
    (src / "rtl" / "l.sv").symlink_to("../top.sv")
    cas = CountingCAS(tmp_path / "cas")
    result = _snap(tmp_path, cache, cas).snapshot(src, "**/*.sv")
    assert result.files == ("rtl/l.sv", "top.sv")
