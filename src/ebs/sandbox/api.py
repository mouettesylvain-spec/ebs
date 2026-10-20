"""The `Sandbox` protocol the runner runs tools through (interfaces.md § 9a, task P3-01).

A sandbox turns the tool's argv into the argv that starts it isolated: `staging` (v1, strict
staging only: the argv as is), `bwrap` (bubblewrap mount namespace) or `apptainer` (fallback,
open decision D6). Inside, the tool sees the same absolute paths as outside, but only for the
mounts it is given plus a fixed set of system directories; any other read fails with ENOENT.
`undeclared_read_hint` turns such a failure in the tool log into advice.

The sandbox is never part of the action key (I3): it changes where the tool may look, not what
it is given.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from ebs.core.errors import SandboxError

__all__ = ["Mount", "Sandbox", "check_path", "undeclared_read_hint"]


@dataclass(frozen=True, slots=True)
class Mount:
    """`src` on the host visible at `dst` inside the sandbox; read-only unless `writable`."""

    src: str
    dst: str
    writable: bool = False


class Sandbox(Protocol):
    @property
    def name(self) -> str:
        """`staging`, `bwrap` or `apptainer`: recorded in the result manifest (R5)."""
        ...

    def wrap(
        self, argv: Sequence[str], mounts: Sequence[Mount], env: Mapping[str, str], cwd: str
    ) -> list[str]:
        """The argv that runs `argv` in `cwd` with exactly `env`, seeing only `mounts` plus the
        system directories. Mounts apply in order (a later one may cover an earlier one)."""
        ...

    def setup_error(self, exit_code: int, log_head: str) -> str | None:
        """The sandbox's own error if it failed before starting the tool, else None."""
        ...


def check_path(path: str, what: str) -> str:
    """`path` if it is absolute and normalized; SandboxError otherwise."""
    if not path.startswith("/") or os.path.normpath(path) != path:
        raise SandboxError(
            f"{what} {path!r} must be an absolute, normalized path (no '..', '.' or trailing "
            "'/'); fix the toolchain install root or [sandbox].ro_binds entry"
        )
    return path


_MISSING: Final = re.compile(
    r"no such file|does not exist|doesn't exist|cannot open|can't open|could not open"
    r"|failed to open|unable to open|cannot find|can't find|could not find|no rule to make target",
    re.IGNORECASE,
)
_ABS_PATH: Final = re.compile(r"(?<![\w.~$/-])(/[^\s'\"`:,;()<>\[\]]+)")
_MAX_PATH: Final = 200


def undeclared_read_hint(log_tail: str, *, sandbox: str, step: str) -> str | None:
    """Advice for a tool that failed inside a real sandbox after a file-not-found message.

    None under `staging` (nothing is hidden there) or when no line of the log looks like a
    missing file. The hint names the first absolute path on the first such line, if any.
    """
    if sandbox == "staging":
        return None
    for line in log_tail.splitlines():
        if _MISSING.search(line) is None:
            continue
        found = _ABS_PATH.search(line)
        what = "a file" if found is None else found.group(1)[:_MAX_PATH]
        return (
            f"the tool could not read {what} inside the {sandbox} sandbox, which shows only the "
            "declared inputs, the toolchain install roots and system directories. If the step "
            "needs it, declare it as an input (or import it); `ebs discover "
            f"{step}` lists every file the step reads"
        )
    return None
