"""Shared helpers for source snapshot tests: a read-counting CAS, git and stat helpers."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from ebs.cas.fs import FsCAS
from ebs.core.digest import Digest

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


class CountingCAS(FsCAS):
    """FsCAS that counts `put_file` calls (the snapshotter's only way to read a source file).

    `before_put`, if set, runs on the source path just before it is read, to simulate an edit
    racing the snapshot.
    """

    def __init__(self, root: Path, domain: str = "test") -> None:
        super().__init__(root, domain)
        self.puts: list[Path] = []
        self.before_put: Callable[[Path], None] | None = None

    def put_file(self, path: Path, *, expected: Digest | None = None) -> Digest:
        self.puts.append(path)
        if self.before_put is not None:
            self.before_put(path)
        return super().put_file(path, expected=expected)


class CountingGit:
    """A git runner that records every argv and delegates to the real `git`."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str], cwd: Path) -> bytes:
        self.calls.append(list(argv))
        return git_output(argv, cwd)


def git_output(argv: Sequence[str], cwd: Path) -> bytes:
    env = {**os.environ, **GIT_ENV}
    return subprocess.run(list(argv), cwd=cwd, env=env, check=True, capture_output=True).stdout


def git(repo: Path, *args: str) -> str:
    return git_output(["git", *args], repo).decode()


def write(path: Path, data: bytes | str, *, executable: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    path.chmod(0o755 if executable else 0o644)
    return path


def fake_stat(
    *,
    dev: int = 1,
    ino: int = 2,
    size: int = 3,
    mtime_ns: int = 1_000_000_000_000_000_000,
    ctime_ns: int | None = None,
    mode: int = 0o100644,
) -> os.stat_result:
    """An `os.stat_result` with the fields the stat key uses."""
    ctime_ns = mtime_ns if ctime_ns is None else ctime_ns
    seq = (mode, ino, dev, 1, 0, 0, size, mtime_ns // 10**9, mtime_ns // 10**9, ctime_ns // 10**9)
    return os.stat_result(seq, {"st_mtime_ns": mtime_ns, "st_ctime_ns": ctime_ns})


def touch_all(paths: Sequence[Path], seconds_back: int = 3600) -> None:
    """Change the mtime (not the content) of every path."""
    for p in paths:
        st = p.stat()
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns - seconds_back * 10**9))
