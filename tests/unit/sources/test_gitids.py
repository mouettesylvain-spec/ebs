"""Git guard (R3): clean tracked files take their id from git, one call per command per repo."""

from __future__ import annotations

import os
import random
from collections.abc import Sequence
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ebs.core.clock import FakeClock, SystemClock
from ebs.core.digest import hash_bytes
from ebs.sources.gitids import GitIds, _dirty_paths, git_blob_hex
from ebs.sources.snapshot import SourceSnapshotter
from ebs.sources.statcache import StatCache
from tests.helpers.sources import CountingCAS, CountingGit, git, touch_all, write


def _later(seconds: float = 60) -> FakeClock:
    """A clock well after every file's mtime/ctime, so no stat entry is racy."""
    clock = FakeClock(SystemClock().now())
    clock.advance(seconds)
    return clock


def _snapshotter(cas: CountingCAS, cache: StatCache, git_ids: GitIds | None) -> SourceSnapshotter:
    return SourceSnapshotter(
        cas, cache, clock=_later(), git=git_ids, audit_fraction=0.0, rng=random.Random(0)
    )


def _populate(repo: Path, n: int) -> list[Path]:
    files = [write(repo / f"d{i % 3}" / f"m{i}.sv", f"module m{i}; endmodule\n") for i in range(n)]
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "more")
    return files


# R3
def test_blob_id_matches_git_hash_object(git_repo: Path) -> None:
    ids = GitIds()
    expected = git(git_repo, "hash-object", "a.sv").strip()
    assert ids.blob_id(git_repo / "a.sv") == expected
    assert (
        ids.blob_id(git_repo / "sub" / "b.sv") == git(git_repo, "hash-object", "sub/b.sv").strip()
    )
    data = (git_repo / "a.sv").read_bytes()
    assert git_blob_hex([data], len(data), "sha1") == expected


# R3
def test_clean_files_use_git(git_repo: Path, tmp_path: Path) -> None:
    files = _populate(git_repo, 20)
    db = tmp_path / "statcache.sqlite"
    first_cas = CountingCAS(tmp_path / "cas")
    with StatCache(db) as cache:
        first = _snapshotter(first_cas, cache, GitIds()).snapshot(git_repo, "**/*.sv")
    assert len(first_cas.puts) == 22  # cold: every file is read once

    # New mtimes, same content: every stat key changes, git still reports the files clean.
    touch_all([*files, git_repo / "a.sv", git_repo / "sub" / "b.sv"])

    with StatCache(db) as cache:
        cas = CountingCAS(tmp_path / "cas")
        again = _snapshotter(cas, cache, GitIds()).snapshot(git_repo, "**/*.sv")
        assert cas.puts == []  # every id came from git's blob ids through the persistent map
        assert again.digest == first.digest

        without_git = CountingCAS(tmp_path / "cas")
        _snapshotter(without_git, cache, None).snapshot(git_repo, "**/*.sv")
        assert len(without_git.puts) == 22  # the stat cache alone would have rehashed them


# R3
def test_dirty_falls_through(git_repo: Path, tmp_path: Path) -> None:
    ids = GitIds()
    write(git_repo / "a.sv", "module a_changed; endmodule\n")  # modified in the work tree
    write(git_repo / "new.sv", "module n; endmodule\n")  # untracked
    write(git_repo / "sub" / "b.sv", "module b2; endmodule\n")
    git(git_repo, "add", "sub/b.sv")  # staged, work tree equal to the index
    write(git_repo / "sub" / "b.sv", "module b3; endmodule\n")  # then modified again
    assert ids.blob_id(git_repo / "a.sv") is None
    assert ids.blob_id(git_repo / "new.sv") is None
    assert ids.blob_id(git_repo / "sub" / "b.sv") is None

    with StatCache(tmp_path / "db.sqlite") as cache:
        cas = CountingCAS(tmp_path / "cas")
        snap = _snapshotter(cas, cache, ids)
        for rel in ("a.sv", "new.sv", "sub/b.sv"):
            assert snap.snapshot_file(git_repo / rel) == hash_bytes((git_repo / rel).read_bytes())


# R3: a stale or poisoned map entry for a dirty file is never consulted
def test_dirty_file_ignores_git_map(git_repo: Path, tmp_path: Path) -> None:
    old_blob = git(git_repo, "hash-object", "a.sv").strip()
    new = b"module a; endmodule\n".replace(b"a;", b"z;")  # same size
    write(git_repo / "a.sv", new)
    with StatCache(tmp_path / "db.sqlite") as cache:
        cas = CountingCAS(tmp_path / "cas")
        stale = cas.put_bytes(b"module a; endmodule\n")
        cache.store_git_blob(old_blob, stale, len(new))
        snap = _snapshotter(cas, cache, GitIds())
        assert snap.snapshot_file(git_repo / "a.sv") == hash_bytes(new)


# R3: the map is only learned from bytes that really hash to the git blob id
def test_map_not_learned_from_filtered_bytes(git_repo: Path, tmp_path: Path) -> None:
    class LyingGit(CountingGit):
        """Reports a.sv clean with a blob id that does not match its bytes (like a filter)."""

        def __call__(self, argv: Sequence[str], cwd: Path) -> bytes:
            out = super().__call__(argv, cwd)
            if "ls-files" in argv:
                real = git(git_repo, "hash-object", "a.sv").strip().encode()
                out = out.replace(real, b"1" * len(real))
            return out

    with StatCache(tmp_path / "db.sqlite") as cache:
        cas = CountingCAS(tmp_path / "cas")
        d = _snapshotter(cas, cache, GitIds(runner=LyingGit())).snapshot_file(git_repo / "a.sv")
        assert d == hash_bytes((git_repo / "a.sv").read_bytes())
        assert cache.lookup_git_blob("1" * 40) is None
        real = git(git_repo, "hash-object", "sub/b.sv").strip()
        _snapshotter(cas, cache, GitIds()).snapshot_file(git_repo / "sub" / "b.sv")
        assert cache.lookup_git_blob(real) is not None


# R3: a map entry whose size differs from the file (e.g. an LFS pointer) is not trusted
def test_map_size_mismatch_falls_through(git_repo: Path, tmp_path: Path) -> None:
    blob = git(git_repo, "hash-object", "a.sv").strip()
    with StatCache(tmp_path / "db.sqlite") as cache:
        cas = CountingCAS(tmp_path / "cas")
        other = cas.put_bytes(b"pointer")
        cache.store_git_blob(blob, other, 7)
        d = _snapshotter(cas, cache, GitIds()).snapshot_file(git_repo / "a.sv")
        assert d == hash_bytes((git_repo / "a.sv").read_bytes())


# R3
def test_single_git_call(git_repo: Path, tmp_path: Path) -> None:
    files = _populate(git_repo, 30)
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")
    write(other / "x.sv", "x")
    git(other, "add", "-A")
    git(other, "commit", "-qm", "x")

    runner = CountingGit()
    with StatCache(tmp_path / "db.sqlite") as cache:
        snap = _snapshotter(CountingCAS(tmp_path / "cas"), cache, GitIds(runner=runner))
        snap.snapshot(git_repo, "**/*.sv")
        for f in files:
            snap.snapshot_file(f)
        assert len(runner.calls) == 2
        assert any("ls-files" in c for c in runner.calls)
        assert any("status" in c for c in runner.calls)
        snap.snapshot(other, "*.sv")
        assert len(runner.calls) == 4


# R3: outside any work tree git is never run
def test_no_repo_no_calls(tmp_path: Path) -> None:
    runner = CountingGit()
    f = write(tmp_path / "plain" / "x.sv", "x")
    assert GitIds(runner=runner).blob_id(f) is None
    assert runner.calls == []


# R3: a failing git (missing binary, broken repo) degrades to the stat cache
def test_git_failure_falls_through(git_repo: Path) -> None:
    def broken(argv: object, cwd: Path) -> bytes:
        raise FileNotFoundError("git")

    ids = GitIds(runner=broken)
    assert ids.blob_id(git_repo / "a.sv") is None
    assert ids.blob_id(git_repo / "sub" / "b.sv") is None


# R3: symlinks and submodules are never given a blob id
def test_symlink_not_mapped(git_repo: Path) -> None:
    os.symlink("a.sv", git_repo / "l.sv")
    git(git_repo, "add", "l.sv")
    git(git_repo, "commit", "-qm", "link")
    assert GitIds().blob_id(git_repo / "l.sv") is None


def test_git_blob_hex_sha256_format() -> None:
    import hashlib

    data = b"hello\n"
    assert git_blob_hex([data], 6, "sha256") == hashlib.sha256(b"blob 6\0hello\n").hexdigest()


_PATH_BYTES = st.binary(min_size=1, max_size=20).filter(lambda b: b"\0" not in b)


# R3: every path git status names is dirty, whatever bytes (spaces, tabs) it contains
@given(
    st.lists(st.tuples(st.sampled_from(["1", "2", "u", "?"]), _PATH_BYTES, _PATH_BYTES), max_size=8)
)
def test_dirty_paths_parses_every_record_kind(records: list[tuple[str, bytes, bytes]]) -> None:
    out: list[bytes] = [b"# branch.oid 0000"]
    expected: set[bytes] = set()
    h = b"0" * 40
    for kind, path, orig in records:
        if kind == "1":
            out.append(b"1 .M N... 100644 100644 100644 " + h + b" " + h + b" " + path)
        elif kind == "2":
            out.append(b"2 R. N... 100644 100644 100644 " + h + b" " + h + b" R100 " + path)
            out.append(orig)
            expected.add(orig)
        elif kind == "u":
            out.append(
                b"u UU N... 100644 100644 100644 100644 " + h + b" " + h + b" " + h + b" " + path
            )
        else:
            out.append(b"? " + path)
        expected.add(path)
    assert _dirty_paths(b"\0".join(out) + b"\0") == expected


# R3: files git status never re-checks (assume-unchanged, skip-worktree) are not trusted
def test_assume_unchanged_and_skip_worktree_fall_through(git_repo: Path, tmp_path: Path) -> None:
    with StatCache(tmp_path / "db.sqlite") as cache:
        cas = CountingCAS(tmp_path / "cas")
        for rel in ("a.sv", "sub/b.sv"):
            _snapshotter(cas, cache, GitIds()).snapshot_file(git_repo / rel)
        git(git_repo, "update-index", "--assume-unchanged", "a.sv")
        git(git_repo, "update-index", "--skip-worktree", "sub/b.sv")
        new_a = b"module z; endmodule\n"
        new_b = b"module y; endmodule\n"
        write(git_repo / "a.sv", new_a)  # same sizes as before
        write(git_repo / "sub" / "b.sv", new_b)
        assert git(git_repo, "status", "--porcelain") == ""  # git itself does not see the edits
        ids = GitIds()
        assert ids.blob_id(git_repo / "a.sv") is None
        assert ids.blob_id(git_repo / "sub" / "b.sv") is None
        snap = _snapshotter(cas, cache, ids)
        assert snap.snapshot_file(git_repo / "a.sv") == hash_bytes(new_a)
        assert snap.snapshot_file(git_repo / "sub" / "b.sv") == hash_bytes(new_b)


# R3: only "H" entries at stage 0 get an id (fake ls-files output)
@pytest.mark.parametrize(
    "record",
    [
        b"H 100644 " + b"1" * 40 + b" 2\ta.sv",  # conflict stage
        b"h 100644 " + b"1" * 40 + b" 0\ta.sv",  # assume-unchanged
        b"S 100644 " + b"1" * 40 + b" 0\ta.sv",  # skip-worktree
        b"H 120000 " + b"1" * 40 + b" 0\ta.sv",  # symlink
        b"H 160000 " + b"1" * 40 + b" 0\ta.sv",  # submodule
    ],
)
def test_untrusted_index_entries_not_mapped(tmp_path: Path, record: bytes) -> None:
    (tmp_path / ".git").mkdir()
    ok = b"H 100755 " + b"2" * 40 + b" 0\tok.sv"

    def runner(argv: Sequence[str], cwd: Path) -> bytes:
        return record + b"\0" + ok + b"\0" if "ls-files" in argv else b""

    ids = GitIds(runner=runner)
    assert ids.blob_id(tmp_path / "a.sv") is None
    assert ids.blob_id(tmp_path / "ok.sv") == "2" * 40  # executable files are mapped too


# R3: executable tracked files use git ids
def test_executable_file_uses_git(git_repo: Path) -> None:
    write(git_repo / "run.sh", "#!/bin/sh\n", executable=True)
    git(git_repo, "add", "run.sh")
    git(git_repo, "commit", "-qm", "exe")
    assert GitIds().blob_id(git_repo / "run.sh") == git(git_repo, "hash-object", "run.sh").strip()


# R3: SHA-256 repositories are learned too (64-hex blob ids)
def test_sha256_repository(tmp_path: Path) -> None:
    repo = tmp_path / "r256"
    repo.mkdir()
    try:
        git(repo, "init", "-q", "--object-format=sha256")
    except Exception:
        pytest.skip("git without sha256 object format support")
    write(repo / "a.sv", "module a; endmodule\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "x")
    blob = git(repo, "hash-object", "a.sv").strip()
    assert len(blob) == 64
    with StatCache(tmp_path / "db.sqlite") as cache:
        _snapshotter(CountingCAS(tmp_path / "cas"), cache, GitIds()).snapshot_file(repo / "a.sv")
        assert cache.lookup_git_blob(blob) == (hash_bytes((repo / "a.sv").read_bytes()), 20)
