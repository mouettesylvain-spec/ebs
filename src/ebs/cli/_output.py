"""Terminal and `--json` output, and the one place errors become messages and exit codes (R5, R6).

Humans get rich tables on a terminal (colors unless `NO_COLOR` is set) and plain lines
otherwise; `--json` prints machine-readable documents on stdout. An error is one line on
stderr, `ebs: error: <where>: <what>; <hint>` (with `--json` also a JSON document on stdout);
`--debug` adds the traceback. Exit codes: interfaces.md § 11.
"""

from __future__ import annotations

import functools
import json
import sys
import traceback
from collections.abc import Callable, Mapping
from typing import Any, Final, Literal, ParamSpec, TypeVar

import typer
from rich.console import Console

from ebs.core.errors import (
    CanonError,
    CasError,
    ConfigError,
    DigestError,
    EbsError,
    ExecutorError,
    ExitCode,
    FlowError,
    MetadataError,
    PlanError,
    RuleError,
    SandboxError,
    SourceError,
    ToolchainError,
    TreeError,
)

__all__ = ["DEBUG_KEY", "Output", "UsageError", "exit_code_for", "handle_errors", "make_output"]

DEBUG_KEY: Final = "ebs.debug"

_USAGE: Final = (
    ConfigError, FlowError, PlanError, RuleError, SourceError, ToolchainError, DigestError,
    CanonError, TreeError,
)  # fmt: skip
_INFRA: Final = (MetadataError, CasError, ExecutorError, SandboxError)


class UsageError(EbsError):
    """A command line that parses but makes no sense (e.g. conflicting options); exit 2."""


P = ParamSpec("P")
R = TypeVar("R")


def exit_code_for(error: BaseException) -> ExitCode:
    if isinstance(error, (*_USAGE, UsageError)):
        return ExitCode.USAGE
    if isinstance(error, _INFRA):
        return ExitCode.INFRA
    if isinstance(error, KeyboardInterrupt):
        return ExitCode.CANCELLED
    return ExitCode.INTERNAL


class Output:
    """Where a command writes: `out` (stdout), `err` (stderr), or JSON documents."""

    def __init__(self, *, json_mode: bool, tty: bool, no_color: bool) -> None:
        self.json = json_mode
        self.tty = tty
        color: Literal["standard"] | None = None if no_color or not tty else "standard"
        self.out = Console(
            file=sys.stdout, force_terminal=tty, no_color=no_color, color_system=color,
            highlight=False, soft_wrap=not tty,
        )  # fmt: skip
        self.err = Console(
            file=sys.stderr, force_terminal=False, no_color=True, highlight=False, soft_wrap=True
        )

    def line(self, text: str = "") -> None:
        self.out.print(text, markup=False)

    def emit(self, doc: Mapping[str, Any]) -> None:
        """One JSON document on one line (stable key order: as built)."""
        sys.stdout.write(json.dumps(doc, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def warn(self, message: str) -> None:
        self.err.print(f"ebs: warning: {message}", markup=False)


def make_output(environ: Mapping[str, str], *, json_mode: bool, tty: bool) -> Output:
    return Output(json_mode=json_mode, tty=tty and not json_mode, no_color="NO_COLOR" in environ)


def _one_line(text: str) -> str:
    return " ".join(part.strip() for part in text.splitlines() if part.strip())


def _render(error: BaseException, code: ExitCode) -> str:
    if code == ExitCode.INTERNAL:
        return (
            f"ebs: internal error: {type(error).__name__}: {_one_line(str(error))}; this is a "
            "bug in ebs, please report it with the output of the same command run with --debug"
        )
    if code == ExitCode.CANCELLED:
        return "ebs: cancelled"
    return f"ebs: error: {_one_line(str(error))}"


def _error_doc(error: BaseException, code: ExitCode) -> dict[str, Any]:
    doc: dict[str, Any] = {"type": type(error).__name__, "message": _one_line(str(error))}
    if isinstance(error, FlowError):
        doc.update(message=error.message, file=error.file, line=error.line, col=error.col)
    return {"error": doc, "exit_code": int(code)}


def handle_errors(fn: Callable[P, R]) -> Callable[P, R]:
    """Run a command; turn any exception into its message and exit code (R5)."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except (typer.Exit, typer.Abort):
            raise
        except (Exception, KeyboardInterrupt) as error:
            code = exit_code_for(error)
            # Every command takes `ctx`; Typer pushes no current context, and its Context class
            # is the one of its vendored click, so read `meta` without an isinstance check.
            meta = getattr(kwargs.get("ctx"), "meta", None)
            if isinstance(meta, dict) and meta.get(DEBUG_KEY):
                sys.stderr.write(traceback.format_exc())
            if kwargs.get("json_mode"):
                sys.stdout.write(json.dumps(_error_doc(error, code)) + "\n")
            sys.stderr.write(_render(error, code) + "\n")
            raise typer.Exit(int(code)) from None

    return wrapper
