"""Error hierarchy and CLI exit codes (docs/design/interfaces.md § 11).

Every error raised by ebs is an `EbsError` subclass whose message says what failed, where
(file, step, action) and what the user can do about it.
"""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    """Process exit codes of the `ebs` CLI."""

    OK = 0
    ACTIONS_FAILED = 1  # build finished, some actions failed
    USAGE = 2  # usage or flow error
    INFRA = 3  # infrastructure error, retries exhausted
    INTERNAL = 4  # bug in ebs ("please report")


class EbsError(Exception):
    """Base class of all ebs errors."""


class ConfigError(EbsError):
    """Invalid or missing site/user configuration."""


class FlowError(EbsError):
    """Invalid flow description; carries the source location when known."""

    def __init__(
        self,
        message: str,
        *,
        file: str | None = None,
        line: int | None = None,
        col: int | None = None,
    ) -> None:
        self.message = message
        self.file = file
        self.line = line
        self.col = col
        super().__init__(self._format())

    def _format(self) -> str:
        if self.file is None:
            return self.message
        location = self.file
        if self.line is not None:
            location += f":{self.line}"
            if self.col is not None:
                location += f":{self.col}"
        return f"{location}: {self.message}"


class DigestError(EbsError):
    """Malformed digest or unsupported digest algorithm."""


class CanonError(EbsError):
    """Value that cannot be canonicalized (e.g. floats, non-string keys)."""


class TreeError(EbsError):
    """Directory that cannot be represented as a tree manifest, or an invalid manifest."""


class PlanError(EbsError):
    """The flow could not be expanded into a valid action graph."""


class CasError(EbsError):
    """Content-addressed store failure (missing object, digest mismatch, I/O)."""


class MetadataError(EbsError):
    """Metadata store or service failure."""


class ExecutorError(EbsError):
    """Failure submitting or tracking actions on an executor."""


class RuleError(EbsError):
    """A rule plugin rejected its step or failed to build a command."""


class SandboxError(EbsError):
    """The execution sandbox could not be set up."""
