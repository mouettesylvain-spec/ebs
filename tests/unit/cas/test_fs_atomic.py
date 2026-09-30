"""I8 / R2 / R9: the blob write sequence, first-writer-wins, and cleanup on failure."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ebs.cas.fs import FsCAS
from ebs.core.digest import hash_bytes
from ebs.core.errors import CasError


class OsSpy:
    """Records the os-level calls the CAS makes, in order, resolving fds to paths."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.events: list[tuple[str, Any]] = []
        self.open_flags: list[int] = []
        self._fd_paths: dict[int, str] = {}
        real_open, real_fsync, real_fchmod = os.open, os.fsync, os.fchmod
        real_rename = os.rename

        def spy_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            fd = real_open(path, flags, *args, **kwargs)
            self._fd_paths[fd] = os.fspath(path)
            self.open_flags.append(flags)
            self.events.append(("open", os.fspath(path)))
            return fd

        def spy_fsync(fd: int) -> None:
            self.events.append(("fsync", self._fd_paths.get(fd)))
            real_fsync(fd)

        def spy_fchmod(fd: int, mode: int) -> None:
            self.events.append(("fchmod", (self._fd_paths.get(fd), mode)))
            real_fchmod(fd, mode)

        def spy_rename(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
            self.events.append(("rename", (os.fspath(src), os.fspath(dst))))
            real_rename(src, dst, *args, **kwargs)

        def forbidden(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("the CAS must not rely on file locks (R9)")

        monkeypatch.setattr(os, "open", spy_open)
        monkeypatch.setattr(os, "fsync", spy_fsync)
        monkeypatch.setattr(os, "fchmod", spy_fchmod)
        monkeypatch.setattr(os, "rename", spy_rename)
        monkeypatch.setattr(fcntl, "flock", forbidden)
        monkeypatch.setattr(fcntl, "lockf", forbidden)

    def index(self, kind: str, pred: Callable[[Any], bool]) -> int:
        for i, (k, arg) in enumerate(self.events):
            if k == kind and pred(arg):
                return i
        raise AssertionError(f"no {kind} event matching; events: {self.events}")


# R2, R9
@pytest.mark.parametrize("via", ["bytes", "file"])
def test_write_sequence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, via: str) -> None:
    cas = FsCAS(tmp_path / "cas", "test")
    src = tmp_path / "src.bin"
    src.write_bytes(b"content" * 1000)
    expected = hash_bytes(src.read_bytes())
    spy = OsSpy(monkeypatch)
    d = cas.put_bytes(src.read_bytes()) if via == "bytes" else cas.put_file(src, expected=expected)
    assert d == expected
    target = str(cas.blob_path(d))
    tmp_dir = str(tmp_path / "cas" / "test" / "tmp")
    shard_dir = os.path.dirname(target)

    i_open = spy.index("open", lambda p: os.path.dirname(p) == tmp_dir)
    tmp_file = spy.events[i_open][1]
    i_fsync_file = spy.index("fsync", lambda p: p == tmp_file)
    i_chmod = spy.index("fchmod", lambda a: a == (tmp_file, 0o444))
    i_rename = spy.index("rename", lambda a: a == (tmp_file, target))
    i_fsync_dir = spy.index("fsync", lambda p: p == shard_dir)
    assert i_open < i_fsync_file < i_chmod < i_rename < i_fsync_dir
    assert os.stat(target).st_mode & 0o777 == 0o444
    assert os.listdir(tmp_dir) == []
    # R9: no O_EXCL lock-file semantics anywhere in the write path.
    assert all(flags & os.O_EXCL == 0 for flags in spy.open_flags)


# R2
def test_existing_target_first_writer_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = FsCAS(tmp_path / "cas", "test")
    second = FsCAS(tmp_path / "cas", "test")
    d = first.put_bytes(b"same content")
    target = first.blob_path(d)
    inode = target.stat().st_ino
    spy = OsSpy(monkeypatch)
    assert second.put_bytes(b"same content") == d
    assert target.stat().st_ino == inode  # the first object was not replaced
    assert not [e for e in spy.events if e[0] == "rename"]
    assert os.listdir(tmp_path / "cas" / "test" / "tmp") == []


# R2
def test_race_lost_between_check_and_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another writer lands the object after our exists-check: rename over it is harmless."""
    cas = FsCAS(tmp_path / "cas", "test")
    other = FsCAS(tmp_path / "cas", "test")
    real_rename = os.rename
    d = hash_bytes(b"racy")

    def rename_after_other_writer(src: str, dst: str) -> None:
        monkeypatch.setattr(os, "rename", real_rename)
        other.put_bytes(b"racy")
        real_rename(src, dst)

    monkeypatch.setattr(os, "rename", rename_after_other_writer)
    assert cas.put_bytes(b"racy") == d
    assert cas.verify(d)
    assert os.listdir(tmp_path / "cas" / "test" / "tmp") == []


# R2 (I8): a failure before the rename leaves no visible object and no tmp file.
@pytest.mark.parametrize("failing", ["fsync", "fchmod", "rename"])
def test_failure_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    cas = FsCAS(tmp_path / "cas", "test")

    def boom(*args: Any, **kwargs: Any) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os, failing, boom)
    with pytest.raises(CasError, match="Input/output error"):
        cas.put_bytes(b"doomed")
    monkeypatch.undo()
    assert not cas.has(hash_bytes(b"doomed"))
    assert os.listdir(tmp_path / "cas" / "test" / "tmp") == []


def test_dir_fsync_failure_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cas = FsCAS(tmp_path / "cas", "test")
    real_fsync = os.fsync
    calls = {"n": 0}

    def second_fsync_fails(fd: int) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(5, "Input/output error")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", second_fsync_fails)
    with pytest.raises(CasError, match="Input/output error"):
        cas.put_bytes(b"durable?")
    monkeypatch.undo()
    # The object was complete when it became visible; only durability is in doubt.
    assert cas.verify(hash_bytes(b"durable?"))
    assert os.listdir(tmp_path / "cas" / "test" / "tmp") == []
