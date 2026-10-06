"""Runner failures, each carrying the `ebs-runner` exit code (docs/design/interfaces.md § 9)."""

from __future__ import annotations

import json
from typing import Final, cast, get_args

from ebs.core.digest import Digest
from ebs.core.errors import EbsError
from ebs.meta.api import InfraReason

__all__ = [
    "EXIT_INFRA",
    "EXIT_INTERNAL",
    "EXIT_OK",
    "EXIT_USAGE",
    "EXIT_VERIFY",
    "INFRA_LINE_PREFIX",
    "InfraError",
    "InputVerificationError",
    "Interrupted",
    "RunnerError",
    "UsageError",
    "format_infra_line",
    "parse_infra_reason",
]

EXIT_OK: Final = 0  # result posted (passed or failed)
EXIT_USAGE: Final = 64  # bad usage: arguments, plan or action do not fit
EXIT_INTERNAL: Final = 70  # a bug in the runner
EXIT_INFRA: Final = 75  # temporary infrastructure failure: the driver retries
EXIT_VERIFY: Final = 76  # an input did not match its expected digest (I10)


class RunnerError(EbsError):
    """A runner failure that ends the run with `exit_code`."""

    exit_code: int = EXIT_INTERNAL


class UsageError(RunnerError):
    exit_code = EXIT_USAGE


class InputVerificationError(RunnerError):
    exit_code = EXIT_VERIFY


class InfraError(RunnerError):
    """Temporary failure (CAS or metadata unreachable, input bytes missing, license, timeout)."""

    exit_code = EXIT_INFRA

    def __init__(
        self, message: str, *, reason: InfraReason = "other", detail: str | None = None
    ) -> None:
        super().__init__(message)
        self.reason: InfraReason = reason
        self.detail = detail or message  # e.g. the rule's own reason ("tool_crash")
        self.tool_exit_code: int | None = None  # set once the tool ran
        self.log: Digest | None = None  # the tool log, uploaded for debugging


class Interrupted(InfraError):
    """The runner got SIGTERM/SIGINT (job cancelled or preempted)."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"interrupted by signal {signum}", reason="preempted")
        self.signum = signum


INFRA_LINE_PREFIX: Final = "ebs-runner: infra_failed "
"""Start of the stderr line that tells an executor why the runner exited 75 (interfaces.md § 9)."""

_INFRA_REASONS: Final[frozenset[str]] = frozenset(get_args(InfraReason))


def format_infra_line(reason: InfraReason, detail: str) -> str:
    """The one stderr line (newline included) an executor parses with `parse_infra_reason`."""
    payload = json.dumps({"reason": reason, "detail": detail}, sort_keys=True)
    return f"{INFRA_LINE_PREFIX}{payload}\n"


def parse_infra_reason(stderr: str) -> InfraReason | None:
    """The reason on the last infra line of the runner's stderr; None if there is no such line.

    A line that is garbled or names an unknown reason yields "other": the runner did say it hit
    an infrastructure failure, just not which one.
    """
    for line in reversed(stderr.splitlines()):
        if not line.startswith(INFRA_LINE_PREFIX):
            continue
        try:
            data = json.loads(line[len(INFRA_LINE_PREFIX) :])
        except ValueError:
            return "other"
        reason = data.get("reason") if isinstance(data, dict) else None
        if isinstance(reason, str) and reason in _INFRA_REASONS:
            return cast(InfraReason, reason)
        return "other"
    return None
