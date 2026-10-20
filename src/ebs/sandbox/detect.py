"""Pick the sandbox for an action on this node (task P3-01 R1, open decision D6).

- `staging`: v1 strict staging, never probes anything.
- `bwrap`: bubblewrap, or SandboxError when the node cannot run it (an explicit choice never
  silently degrades; the runner reports an infra failure, so another node may be tried).
- `auto` (D6 default): bubblewrap if it can create its namespaces here, else Apptainer with the
  site image, else staging only with a warning.

Availability is checked by running each candidate once (`probe`) with the same namespaces the
real run uses: a `bwrap` binary on a node without unprivileged user namespaces fails there.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from ebs.core.errors import SandboxError
from ebs.flow.model import SandboxMode
from ebs.sandbox.api import Sandbox
from ebs.sandbox.apptainer import ApptainerSandbox
from ebs.sandbox.bwrap import BWRAP_NAMESPACES, BwrapSandbox
from ebs.sandbox.staging import StagingSandbox

__all__ = ["Detection", "Probe", "detect", "run_probe"]

Probe = Callable[[Sequence[str]], "str | None"]
"""Runs a probe argv; None if it succeeded, else what went wrong (its stderr)."""

PROBE_TIMEOUT_S: Final = 30.0
_PROBE_ENV: Final = {"PATH": "/usr/bin:/bin"}
_MAX_ERROR: Final = 300


@dataclass(frozen=True, slots=True)
class Detection:
    sandbox: Sandbox
    reason: str  # why this sandbox (and why not the better ones)
    warning: str | None = None  # set when `auto` fell back to staging only


def run_probe(argv: Sequence[str]) -> str | None:
    """Run `argv` with a minimal env; None on exit code 0, else its stderr (shortened)."""
    try:
        proc = subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=PROBE_TIMEOUT_S,
            env=_PROBE_ENV,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)[:_MAX_ERROR]
    if proc.returncode == 0:
        return None
    err = proc.stderr.decode("utf-8", errors="replace").strip()
    return (err or f"exit code {proc.returncode}")[:_MAX_ERROR]


def detect(
    mode: SandboxMode,
    *,
    which: Callable[[str], str | None] = shutil.which,
    probe: Probe = run_probe,
    apptainer_image: str | None = None,
) -> Detection:
    """The sandbox to use for `mode` on this node; SandboxError for an unavailable `bwrap`."""
    if mode == "staging":
        return Detection(StagingSandbox(), "the action asks for staging only")
    bwrap, why_not_bwrap = _bwrap(which, probe)
    if bwrap is not None:
        return Detection(bwrap, f"bubblewrap works on this node ({bwrap.binary})")
    if mode == "bwrap":
        raise SandboxError(
            f"the action asks for the bwrap sandbox but {why_not_bwrap}. Install bubblewrap and "
            "enable unprivileged user namespaces on the node (sysctl user.max_user_namespaces), "
            "or set `sandbox: auto` in the flow to fall back to Apptainer or staging only"
        )
    app, why_not_app = _apptainer(which, probe, apptainer_image)
    if app is not None:
        return Detection(app, f"{why_not_bwrap}; using apptainer with image {app.image}")
    reason = f"{why_not_bwrap}; {why_not_app}"
    warning = (
        f"no sandbox available on this node ({reason}); running with staging only, so reads "
        "of undeclared files are not caught"
    )
    return Detection(StagingSandbox(), reason, warning)


def _bwrap(which: Callable[[str], str | None], probe: Probe) -> tuple[BwrapSandbox | None, str]:
    binary = which("bwrap")
    if binary is None:
        return None, "bwrap is not on PATH"
    error = probe([binary, *BWRAP_NAMESPACES, "--ro-bind", "/", "/", "true"])
    if error is not None:
        return None, f"bwrap cannot create its namespaces here: {error}"
    return BwrapSandbox(binary), ""


def _apptainer(
    which: Callable[[str], str | None], probe: Probe, image: str | None
) -> tuple[ApptainerSandbox | None, str]:
    if image is None:
        return None, "no apptainer image is configured ([sandbox].apptainer_image)"
    binary = which("apptainer")
    if binary is None:
        return None, "apptainer is not on PATH"
    error = probe([binary, "exec", "--containall", "--no-mount", "bind-paths", image, "true"])
    if error is not None:
        return None, f"apptainer cannot run {image}: {error}"
    return ApptainerSandbox(binary, image), ""
