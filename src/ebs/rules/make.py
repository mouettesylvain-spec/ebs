"""`make` rule: wraps an existing Makefile as-is (architecture.md § Legacy wrapping).

`make -C <workdir> [<target>] [VAR=value…]` with the step's params as make variables in sorted
order. The whole workdir tree is an implicit input. Parallelism comes from `resources.cpus`
through the runner (`MAKEFLAGS=-j$EBS_CPUS`), so it never changes the action key.
"""

from __future__ import annotations

import posixpath

from ebs.core.errors import RuleError
from ebs.core.log import get_logger
from ebs.flow.model import StepDef
from ebs.rules.api import ActionTemplate, BaseRule, ExpandContext, param_text

__all__ = ["MAKE_ENV_VARS", "WORKDIR_INPUT", "MakeRule"]

MAKE_ENV_VARS = frozenset({"MAKEFLAGS", "MFLAGS", "MAKELEVEL"})
"""Make's own control variables: never taken from the step's env or params."""

WORKDIR_INPUT = "ebs.workdir"

_log = get_logger(__name__)


def _normalize_workdir(workdir: str, instance_id: str) -> str:
    normalized = posixpath.normpath(workdir) if workdir else ""
    if not normalized or normalized.startswith("/") or normalized.split("/")[0] == "..":
        raise RuleError(
            f"step {instance_id!r}: make workdir {workdir!r} must be relative and stay inside "
            "the flow directory (no leading '/', no '..')"
        )
    return normalized


class MakeRule(BaseRule):
    kind = "make"
    version = "1"

    def validate(self, step: StepDef) -> None:
        if step.workdir is None:
            raise RuleError(
                "a 'make' step's 'workdir' is required: the directory holding the Makefile, "
                "relative to the flow directory (e.g. workdir: flows/lint)"
            )
        self._reject_fields(
            step, ("script", "command"), "it runs `make -C <workdir> <target>`; use kind 'shell'"
        )
        reserved = sorted(MAKE_ENV_VARS & step.params.keys())
        if reserved:
            raise RuleError(
                f"a 'make' step cannot set {', '.join(reserved)} as a param: ebs controls make's "
                "flags (parallelism comes from resources.cpus)"
            )

    def expand(self, step: StepDef, ctx: ExpandContext) -> ActionTemplate:
        self.validate(step)
        assert step.workdir is not None
        instance_id = ctx.instance.instance_id
        workdir = _normalize_workdir(ctx.render_str(step.workdir, field="workdir"), instance_id)
        argv = ["make", "-C", workdir]
        if step.target is not None:
            target = ctx.render_str(step.target, field="target")
            if not target or target.startswith("-") or "=" in target:
                raise RuleError(
                    f"step {instance_id!r}: make target {target!r} must not be empty, start "
                    "with '-' or contain '=' (make would read an option or a variable; use "
                    "params for variables, omit 'target' for make's default goal)"
                )
            argv.append(target)
        params = ctx.instance.params
        argv += [f"{name}={param_text(params[name])}" for name in sorted(params)]

        env = dict(ctx.instance.env)
        dropped = sorted(MAKE_ENV_VARS & env.keys())
        if dropped:
            _log.warning("make_env_dropped", step=instance_id, variables=dropped)
            for name in dropped:
                del env[name]
        pattern = "**" if workdir == "." else f"{workdir}/**"
        return ActionTemplate(
            argv=tuple(argv),
            env=env,
            inputs={WORKDIR_INPUT: pattern},
            runtime_env={"MAKEFLAGS": "-j$EBS_CPUS"},
        )
