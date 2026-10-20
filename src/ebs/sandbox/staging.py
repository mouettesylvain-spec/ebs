"""Sandbox v1: strict staging only (the work dir holds only declared inputs; I10, I11).

The tool runs unwrapped: nothing stops it from reading an absolute path on the host. The planner
rejects absolute paths into user homes or project trees, and `bwrap` closes the rest (P3-01).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ebs.sandbox.api import Mount

__all__ = ["StagingSandbox"]


@dataclass(frozen=True, slots=True)
class StagingSandbox:
    @property
    def name(self) -> str:
        return "staging"

    def wrap(
        self, argv: Sequence[str], mounts: Sequence[Mount], env: Mapping[str, str], cwd: str
    ) -> list[str]:
        return list(argv)

    def setup_error(self, exit_code: int, log_head: str) -> str | None:
        return None
