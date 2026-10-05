"""`shell` rule: an inline bash script (written as a hashed config file) or an argv list."""

from __future__ import annotations

from ebs.core.errors import RuleError
from ebs.flow.model import StepDef
from ebs.rules.api import GENERATED_DIR, ActionTemplate, BaseRule, ExpandContext, render_command

__all__ = ["BASH_ARGV", "SCRIPT_PATH", "ShellRule"]

SCRIPT_PATH = f"{GENERATED_DIR}/script.sh"
BASH_ARGV = ("bash", "--noprofile", "--norc", "-eo", "pipefail")


class ShellRule(BaseRule):
    """`script:` runs as `bash --noprofile --norc -eo pipefail .ebs/script.sh`, `command:` as is."""

    kind = "shell"
    version = "1"

    def validate(self, step: StepDef) -> None:
        present = [n for n in ("script", "command") if getattr(step, n) is not None]
        if len(present) != 1:
            found = ", ".join(repr(n) for n in present) or "none"
            raise RuleError(
                "a 'shell' step needs exactly one of 'script' or 'command' "
                f"(found: {found}): use 'script' for bash text, 'command' for an argv list"
            )
        if step.command is not None and not step.command:
            raise RuleError("a 'shell' step's 'command' must not be empty: give at least a program")
        self._reject_fields(
            step, ("workdir", "target"), "only 'make' steps use 'workdir' and 'target'"
        )

    def expand(self, step: StepDef, ctx: ExpandContext) -> ActionTemplate:
        self.validate(step)
        env = dict(ctx.instance.env)
        if step.script is not None:
            script = ctx.render_str(step.script, field="script")
            return ActionTemplate(
                argv=(*BASH_ARGV, SCRIPT_PATH), env=env, config_files={SCRIPT_PATH: script}
            )
        assert step.command is not None  # validate() guarantees exactly one
        return ActionTemplate(argv=tuple(render_command(step.command, ctx)), env=env)
