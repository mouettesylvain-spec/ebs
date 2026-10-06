"""Runner failures, each carrying the `ebs-runner` exit code (docs/design/interfaces.md § 9)."""

from __future__ import annotations

from typing import Final

from ebs.core.digest import Digest
from ebs.core.errors import EbsError
from ebs.meta.api import InfraReason

__all__ = [
    "EXIT_INFRA",
    "EXIT_INTERNAL",
    "EXIT_OK",
    "EXIT_USAGE",
    "EXIT_VERIFY",
    "InfraError",
    "InputVerificationError",
    "Interrupted",
    "RunnerError",
    "UsageError",
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
