"""Git guard: content ids of clean, tracked files straight from git's index (R3).

Per repository, `git ls-files -s -v` gives the blob id of every tracked file and one
`git status --porcelain=v2` lists the paths whose work-tree content may differ from the index;
both run once per repository and are cached for the lifetime of a `GitIds` (one plan). Only
regular files at stage 0, without the assume-unchanged or skip-worktree bit (which make
`git status` skip them), that git reports clean get a blob id; everything else falls through
to the stat cache. The snapshotter maps blob ids to SHA-256 through the persistent map in the
stat cache database.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Final, Literal

from ebs.core.log import get_logger

_log = get_logger(__name__)

GitRunner = Callable[[Sequence[str], Path], bytes]
GitHashAlgo = Literal["sha1", "sha256"]

_REGULAR_MODES: Final = frozenset({b"100644", b"100755"})
# Variables that would make git read another repository or index than the one at `cwd`.
_REPO_ENV: Final = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
        "GIT_PREFIX",
    }
)
# fsmonitor off: a stale daemon could report an edited file as clean.
_GIT: Final = ("git", "--no-optional-locks", "-c", "core.fsmonitor=false")
# -v tags each entry: "H" is a plain tracked file; lowercase (assume-unchanged) and "S"
# (skip-worktree) entries are ones `git status` never re-checks, so they are not trusted.
_LS_FILES: Final = (*_GIT, "ls-files", "-s", "-v", "-z")
_STATUS: Final = (
    *_GIT,
    "status",
    "--porcelain=v2",
    "-z",
    "--untracked-files=no",
    "--ignore-submodules=all",
)


def run_git(argv: Sequence[str], cwd: Path) -> bytes:
    """Run git (argv list, no shell) in `cwd` and return its stdout."""
    env = {k: v for k, v in os.environ.items() if k not in _REPO_ENV}
    return subprocess.run(
        list(argv), cwd=cwd, env=env, check=True, capture_output=True, stdin=subprocess.DEVNULL
    ).stdout


def git_blob_hex(chunks: Iterable[bytes], size: int, algo: GitHashAlgo) -> str:
    """The git blob id of content streamed as `chunks` (`size` bytes in total)."""
    h = hashlib.new(algo, usedforsecurity=False)
    h.update(b"blob %d\0" % size)
    for chunk in chunks:
        h.update(chunk)
    return h.hexdigest()


def blob_algo(blob: str) -> GitHashAlgo:
    """The object format of a git blob id: 40 hex chars for sha1, 64 for sha256."""
    return "sha256" if len(blob) == 64 else "sha1"


class GitIds:
    """Blob ids of clean tracked files, one `ls-files` and one `status` call per repository."""

    def __init__(self, runner: GitRunner = run_git) -> None:
        self._runner = runner
        self._root_of: dict[str, str | None] = {}
        self._repos: dict[str, dict[str, str]] = {}

    def blob_id(self, path: Path) -> str | None:
        """The git blob id of `path` if it is a clean, tracked regular file; else None."""
        abspath = os.path.abspath(path)
        root = self._find_root(os.path.dirname(abspath))
        if root is None:
            return None
        clean = self._repos.get(root)
        if clean is None:
            clean = self._repos[root] = self._load(root)
        return clean.get(abspath)

    def _find_root(self, directory: str) -> str | None:
        visited: list[str] = []
        current = directory
        while True:
            if current in self._root_of:
                root = self._root_of[current]
                break
            visited.append(current)
            if os.path.lexists(os.path.join(current, ".git")):
                root = current
                break
            parent = os.path.dirname(current)
            if parent == current:
                root = None
                break
            current = parent
        for d in visited:
            self._root_of[d] = root
        return root

    def _load(self, root: str) -> dict[str, str]:
        try:
            listing = self._runner(list(_LS_FILES), Path(root))
            status = self._runner(list(_STATUS), Path(root))
        except (OSError, subprocess.SubprocessError) as exc:
            _log.warning("sources.git_unavailable", repo=root, error=str(exc))
            return {}
        dirty = _dirty_paths(status)
        clean: dict[str, str] = {}
        for record in listing.split(b"\0"):
            if not record:
                continue
            meta, _, rel = record.partition(b"\t")
            tag, mode, blob, stage = meta.split(b" ")
            if tag != b"H" or mode not in _REGULAR_MODES or stage != b"0" or rel in dirty:
                continue
            clean[os.path.join(root, os.fsdecode(rel))] = blob.decode("ascii")
        return clean


def _dirty_paths(status: bytes) -> set[bytes]:
    """Every path `git status --porcelain=v2 -z` mentions (changed, renamed, unmerged)."""
    dirty: set[bytes] = set()
    records = iter(status.split(b"\0"))
    for record in records:
        kind = record[:1]
        if kind == b"1":
            dirty.add(record.split(b" ", 8)[8])
        elif kind == b"2":
            dirty.add(record.split(b" ", 9)[9])
            dirty.add(next(records, b""))  # the original path of a rename or copy
        elif kind == b"u":
            dirty.add(record.split(b" ", 10)[10])
        elif kind in (b"?", b"!"):
            dirty.add(record[2:])
    return dirty
