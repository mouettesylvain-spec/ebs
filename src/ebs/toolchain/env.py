"""Toolchain environment capture (docs/design/interfaces.md § 13).

`capture_env` runs a setup command (e.g. a `module load` wrapper) in a clean, non-interactive bash
and returns the environment it leaves behind. The bash script is a fixed string; the command
arrives as positional parameters and runs as `"$@"`, so nothing in argv or in the environment is
ever re-parsed by the shell. The command runs in the same shell as the final `env -0`, so
builtins such as `export`, `source` or a `module` shell function change the captured
environment.

The temporary HOME is replaced by the literal `$HOME` in every value, so the same setup gives
the same environment, and therefore the same toolchain id, for every user and on every host.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from ebs.core.errors import ToolchainError
from ebs.core.log import get_logger

DEFAULT_PATH: Final = "/usr/bin:/bin"
DEFAULT_LANG: Final = "C.UTF-8"
HOME_PLACEHOLDER: Final = "$HOME"

VOLATILE_VARS: Final = frozenset(
    {
        "PWD",
        "OLDPWD",
        "SHLVL",
        "_",
        "RANDOM",
        "SRANDOM",
        "SECONDS",
        "EPOCHSECONDS",
        "EPOCHREALTIME",
        "BASHPID",
        "PPID",
        "LINENO",
        "HISTCMD",
        "BASH_ARGV0",
        "BASH_COMMAND",
    }
)
"""Variables that differ between runs of the same setup; never part of a captured env."""

# Fixed script: the setup command is "$@" (positional parameters, never interpolated). Its stdout
# goes to stderr so only `env -0` writes to stdout. `command -p` finds `env` on the default PATH
# even if the setup replaced PATH; `builtin` keeps a setup-defined `command` function from
# shadowing it (the setup command itself is trusted and could still subvert the capture).
_SCRIPT: Final = (
    '"$@" >&2 || { s=$?; '
    'printf "ebs: toolchain setup command exited with status %s\\n" "$s" >&2; exit "$s"; }\n'
    "builtin command -p env -0\n"
)
_STDERR_TAIL: Final = 2000
_NAME_RE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Variables bash itself acts on before or while running the fixed script: BASH_ENV is sourced
# (with command substitution) by every non-interactive bash, the others change how it parses
# and runs the script. Exported functions arrive as `BASH_FUNC_<name>%%`, which _NAME_RE rejects.
SHELL_CONTROL_VARS: Final = frozenset(
    {"BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "PS4", "CDPATH", "GLOBIGNORE", "IFS"}
)

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RunResult:
    returncode: int
    stdout: bytes
    stderr: bytes


EnvRunner = Callable[[Sequence[str], Mapping[str, str], str, float], RunResult]
"""`(cmd, env, cwd, timeout_s) -> RunResult`; raises `subprocess.TimeoutExpired` on timeout."""


def run_subprocess(
    cmd: Sequence[str], env: Mapping[str, str], cwd: str, timeout_s: float
) -> RunResult:
    """Default `EnvRunner`: runs `cmd` in a new session and kills the whole group on timeout."""
    # The new session lets a timeout kill grandchildren too, which would otherwise keep the
    # pipes open and make the final communicate() wait for them.
    with subprocess.Popen(
        list(cmd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env),
        cwd=cwd,
        start_new_session=True,
    ) as proc:
        try:
            out, err = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise
    return RunResult(proc.returncode, out, err)


def capture_env(
    argv: Sequence[str],
    *,
    base_env: Mapping[str, str],
    timeout_s: float = 60.0,
    runner: EnvRunner = run_subprocess,
) -> dict[str, str]:
    """Run `argv` in a clean bash, then return its environment minus volatile variables.

    The shell starts with only `PATH=/usr/bin:/bin`, a fresh empty `HOME` and `LANG=C.UTF-8`,
    plus `base_env` (which may override PATH and LANG but not HOME). An empty argv captures the
    starting environment. Raises ToolchainError if the command fails or times out.
    """
    _check_base_env(base_env)
    bash = shutil.which("bash", path=DEFAULT_PATH)
    if bash is None:
        raise ToolchainError(
            f"bash not found on {DEFAULT_PATH}; toolchain environment capture needs bash"
        )
    home = tempfile.mkdtemp(prefix="ebs-env-home-")
    try:
        env = {"PATH": DEFAULT_PATH, "LANG": DEFAULT_LANG, **base_env, "HOME": home}
        cmd = [bash, "--noprofile", "--norc", "-c", _SCRIPT, "ebs-capture-env", *argv]
        out = _run(runner, cmd, env=env, cwd=home, timeout_s=timeout_s, argv=argv)
        captured = _parse(out, argv)
        return {
            name: _normalize_home(value, home)
            for name, value in captured.items()
            if name not in VOLATILE_VARS
        }
    finally:
        shutil.rmtree(home, ignore_errors=True)


def _check_base_env(base_env: Mapping[str, str]) -> None:
    for name, value in base_env.items():
        if name == "HOME":
            raise ToolchainError(
                "base_env must not set HOME: environment capture always uses a fresh empty HOME "
                "so user dotfiles cannot leak into the toolchain"
            )
        if _NAME_RE.fullmatch(name) is None or "\0" in value:
            raise ToolchainError(
                f"invalid environment variable {name!r} in base_env: names must match "
                "[A-Za-z_][A-Za-z0-9_]* (no exported shell functions) and values contain no NUL"
            )
        if name in SHELL_CONTROL_VARS or name.startswith("BASH_"):
            raise ToolchainError(
                f"base_env must not set {name}: bash acts on it while starting the capture "
                "shell, so it could run code or change the capture script; set it in the "
                "setup command instead if the tool really needs it"
            )


def _run(
    runner: EnvRunner,
    cmd: list[str],
    *,
    env: dict[str, str],
    cwd: str,
    timeout_s: float,
    argv: Sequence[str],
) -> bytes:
    try:
        proc = runner(cmd, env, cwd, timeout_s)
    except subprocess.TimeoutExpired:
        raise ToolchainError(
            f"toolchain setup command {list(argv)!r} timed out after {timeout_s:g} s; "
            "check that it does not wait for input or a license"
        ) from None
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace")[-_STDERR_TAIL:].strip()
        raise ToolchainError(
            f"toolchain setup command {list(argv)!r} failed with status {proc.returncode}"
            + (f":\n{tail}" if tail else "")
        )
    if not proc.stdout:
        raise ToolchainError(
            f"toolchain setup command {list(argv)!r} exited the shell before the environment "
            "was captured; the setup must not call `exit`"
        )
    _log.debug("captured toolchain env", argv=list(argv), bytes=len(proc.stdout))
    return proc.stdout


def _parse(out: bytes, argv: Sequence[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for item in out.split(b"\0"):
        if not item:
            continue
        name, sep, value = item.partition(b"=")
        try:
            env[name.decode()] = value.decode()
        except UnicodeDecodeError:
            raise ToolchainError(
                f"toolchain setup command {list(argv)!r} set variable "
                f"{name.decode('utf-8', 'replace')!r} to a value that is not UTF-8; "
                "toolchain environments must be UTF-8"
            ) from None
        if not sep:  # pragma: no cover  # env(1) always prints NAME=VALUE
            raise ToolchainError(f"unexpected env output entry {item!r}")
    return env


def _normalize_home(value: str, home: str) -> str:
    for spelling in sorted({home, os.path.realpath(home)}, key=len, reverse=True):
        value = value.replace(spelling, HOME_PLACEHOLDER)
    return value
