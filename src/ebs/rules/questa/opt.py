"""`questa.opt` rule: optimize (elaborate) a design from upstream libraries into a model.

    vlib <model>
    vopt -modelsimini .ebs/modelsim.ini -work <model> -L <lib>… <opts…> <top>… -o opt

Every `${steps.…}` input is a library (mapped in modelsim.ini, searched with `-L`). Params: `top`
(required; design units, space-separated), `vopt_opts`. Output `model` is the work library
holding the optimized design `opt`: `opt` and `deterministic: false` unless declared.
"""

from __future__ import annotations

import re
from typing import Final

from ebs.core.errors import RuleError
from ebs.flow.model import OutputDef, StepDef
from ebs.rules.api import ActionTemplate, ExpandContext
from ebs.rules.questa._common import (
    QuestaRule,
    declared_or_default_output,
    script,
    script_argv,
    script_path,
    split_opts,
    step_inputs,
)
from ebs.rules.questa.ini import MODELSIM_INI, fanin_libraries, modelsim_ini

__all__ = ["OPT_UNIT", "SCRIPT_PATH", "QuestaOptRule"]

KIND: Final = "questa.opt"
SCRIPT_PATH: Final = script_path(KIND)
OPT_UNIT: Final = "opt"
"""Name of the optimized design unit in the model library (what `questa.sim` loads)."""

_UNIT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$.]*(?:\([A-Za-z_][A-Za-z0-9_]*\))?")


class QuestaOptRule(QuestaRule):
    kind = KIND
    version = "1"
    allowed_params = ("top", "vopt_opts")
    required_params = ("top",)

    def expand(self, step: StepDef, ctx: ExpandContext) -> ActionTemplate:
        self.validate(step)
        iid = ctx.instance.instance_id
        params = ctx.instance.params
        libraries = fanin_libraries(step_inputs(step, ctx).libraries())
        if not libraries:
            raise RuleError(
                f"step {iid!r}: a {KIND!r} step needs at least one library input, e.g. "
                "libs: ${steps.compile.outputs.worklib[*]}"
            )
        tops = split_opts(params["top"], "top", iid)
        for top in tops or [""]:
            if not _UNIT_RE.fullmatch(top):
                raise RuleError(
                    f"step {iid!r}: top {top!r} must be a design unit name such as tb_top, "
                    "lib.tb_top or tb_top(arch); pass options in vopt_opts"
                )
        model, outputs = declared_or_default_output(
            KIND, step, ctx, "model", OutputDef(dir="opt", deterministic=False)
        )
        cmd = ["vopt", "-modelsimini", MODELSIM_INI, "-work", model]
        for name in libraries:
            cmd += ["-L", name]
        cmd += split_opts(params.get("vopt_opts", ""), "vopt_opts", iid)
        cmd += [*tops, "-o", OPT_UNIT]
        return ActionTemplate(
            argv=script_argv(KIND),
            env=dict(ctx.instance.env),
            outputs=outputs,
            config_files={
                SCRIPT_PATH: script(KIND, [["vlib", model], cmd]),
                MODELSIM_INI: modelsim_ini(KIND, libraries),
            },
        )
