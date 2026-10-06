"""The tool's environment (invariant I11): nothing is inherited unless declared or passed through.

Layers, later ones winning: toolchain env (captured at registration, `$HOME` replaced by the
action's empty scratch home) → the action's declared env → the rule's runtime env (expanded) →
the runner's own variables. Caller variables matching `[runner].passthrough_env` (license
servers, SLURM job info) fill in only names no layer set; they are site facts, never in the key.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping, Sequence
from typing import Final

from ebs.plan.types import ActionSpec
from ebs.rules.api import expand_runtime_env
from ebs.runner.stage import Scratch

__all__ = ["DEFAULT_PATH", "HOME_PLACEHOLDER", "build_env", "passthrough"]

HOME_PLACEHOLDER: Final = "$HOME"
"""How captured toolchain env spells the home directory (`ebs.toolchain.env`)."""

DEFAULT_PATH: Final = "/usr/bin:/bin"
"""PATH when neither the toolchain nor the action sets one (as in toolchain env capture)."""


def passthrough(caller_env: Mapping[str, str], patterns: Sequence[str]) -> dict[str, str]:
    """The caller variables whose names match one of `patterns` (case-sensitive globs)."""
    return {
        name: value
        for name, value in caller_env.items()
        if any(fnmatch.fnmatchcase(name, p) for p in patterns)
    }


def build_env(
    spec: ActionSpec,
    *,
    toolchain_env: Mapping[str, str],
    scratch: Scratch,
    caller_env: Mapping[str, str],
    passthrough_patterns: Sequence[str],
) -> dict[str, str]:
    """The complete environment of the tool process."""
    cpus = spec.resources.cpus if isinstance(spec.resources.cpus, int) else 1
    runner_vars = {
        "HOME": str(scratch.home),
        "TMPDIR": str(scratch.tmp),
        "EBS_CPUS": str(cpus),
        "EBS_ACTION_ID": spec.action_id,
        "EBS_SCRATCH": str(scratch.root),
    }
    env = {
        name: value.replace(HOME_PLACEHOLDER, str(scratch.home))
        for name, value in toolchain_env.items()
    }
    env.update(spec.env)
    env.update(expand_runtime_env(spec.runtime_env, runner_vars))
    env.setdefault("PATH", DEFAULT_PATH)
    for name, value in passthrough(caller_env, passthrough_patterns).items():
        env.setdefault(name, value)
    env.update(runner_vars)
    return env
