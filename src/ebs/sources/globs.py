"""Resolve a declared source glob under a base directory (gitignore-style, sandbox v1 rule).

Semantics (R1):
- `/` separates segments; `*`, `?` and `[…]` (`[!…]` negates) match within one segment and
  `**` matches zero or more directories (as a last segment: everything below).
- A name starting with `.` matches only a segment that also starts with `.`, so `*` and `**`
  never enter hidden files or directories.
- A matched directory stands for `dir/**`. Only files and symlinks are returned.
- A symlink is returned as itself (never expanded) when it is the last segment matched; a
  symlink to a directory is followed when later segments name something inside it, but `**`
  never follows symlinks (no cycles, no duplicates).

Escapes (R2): absolute patterns, `..` that climbs out of `base`, and symlinks whose target
resolves outside `base` raise `SourceEscapeError` naming the pattern and the resolved path.
"""

from __future__ import annotations

import fnmatch
import os
import posixpath
import re
import stat
from collections.abc import Callable
from pathlib import Path

from ebs.core.errors import SourceError, SourceEscapeError

_Matcher = Callable[[str], bool]


def resolve_glob(base: Path, pattern: str, *, optional: bool = False) -> list[str]:
    """Relative POSIX paths of the files and symlinks matching `pattern`, sorted bytewise."""
    real_base = os.path.realpath(base)
    if not os.path.isdir(real_base):
        raise SourceError(
            f"source base {base} is not a directory (pattern {pattern!r}); check the path"
        )
    segments = _segments(base, pattern)
    found: set[str] = set()
    _Walker(Path(real_base), pattern, found).match(Path(real_base), (), segments)
    if not found and not optional:
        raise SourceError(
            f"source pattern {pattern!r} matched no files under {base}; fix the pattern or mark "
            "the input optional"
        )
    # Code-point order equals UTF-8 byte order.
    return sorted(found)


def _segments(base: Path, pattern: str) -> tuple[str, ...]:
    if pattern.startswith("/"):
        raise SourceEscapeError(
            f"source pattern {pattern!r} is absolute and resolves to {pattern} outside "
            f"{base}; use a path relative to the base"
        )
    norm = posixpath.normpath(pattern) if pattern else "."
    if norm == ".":
        raise SourceError(f"source pattern {pattern!r} is empty; name the files to include")
    if norm == ".." or norm.startswith("../"):
        resolved = os.path.normpath(os.path.join(base, pattern))
        raise SourceEscapeError(
            f"source pattern {pattern!r} climbs out of the base with '..' and resolves to "
            f"{resolved}; sources must stay inside {base}"
        )
    return tuple(norm.split("/"))


def _compile(segment: str) -> _Matcher:
    if not any(c in segment for c in "*?["):
        return segment.__eq__
    regex = re.compile(fnmatch.translate(segment))
    hidden_ok = segment.startswith(".")
    return lambda name: (hidden_ok or not name.startswith(".")) and regex.match(name) is not None


class _Walker:
    def __init__(self, real_base: Path, pattern: str, found: set[str]) -> None:
        self._base = real_base
        self._base_str = str(real_base)
        self._pattern = pattern
        self._found = found

    def match(self, directory: Path, rel: tuple[str, ...], segments: tuple[str, ...]) -> None:
        segment, rest = segments[0], segments[1:]
        if segment == "**":
            if not rest:
                self._everything_below(directory, rel)
                return
            self.match(directory, rel, rest)  # zero directories
            for name, st in self._scan(directory):
                if stat.S_ISDIR(st.st_mode) and not name.startswith("."):
                    self.match(directory / name, (*rel, name), segments)
            return
        matcher = _compile(segment)
        for name, st in self._scan(directory):
            if not matcher(name):
                continue
            path, child = directory / name, (*rel, name)
            if stat.S_ISLNK(st.st_mode):
                target = self._check_link(path, child)
                if rest and os.path.isdir(target):
                    self.match(path, child, rest)
                elif not rest:
                    self._found.add("/".join(child))
            elif stat.S_ISDIR(st.st_mode):
                if rest:
                    self.match(path, child, rest)
                else:
                    self._everything_below(path, child)
            elif not rest:
                self._found.add("/".join(child))

    def _everything_below(self, directory: Path, rel: tuple[str, ...]) -> None:
        """`dir/**`: every non-hidden file and symlink, not following symlinked directories."""
        for name, st in self._scan(directory):
            if name.startswith("."):
                continue
            child = (*rel, name)
            if stat.S_ISDIR(st.st_mode):
                self._everything_below(directory / name, child)
            else:
                if stat.S_ISLNK(st.st_mode):
                    self._check_link(directory / name, child)
                self._found.add("/".join(child))

    def _check_link(self, path: Path, rel: tuple[str, ...]) -> str:
        """The resolved target of a symlink; SourceEscapeError if it leaves the base."""
        resolved = os.path.realpath(path)
        if os.path.commonpath([self._base_str, resolved]) != self._base_str:
            raise SourceEscapeError(
                f"source pattern {self._pattern!r} matches symlink {'/'.join(rel)}, which "
                f"resolves to {resolved} outside the base {self._base}; sources must stay "
                "inside the base (copy the file in or declare it as a separate input)"
            )
        return resolved

    def _scan(self, directory: Path) -> list[tuple[str, os.stat_result]]:
        try:
            with os.scandir(directory) as it:
                return [(e.name, e.stat(follow_symlinks=False)) for e in it]
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise SourceError(
                f"cannot read {directory} while resolving source pattern {self._pattern!r}: "
                f"{exc.strerror or exc}; check its permissions"
            ) from exc
