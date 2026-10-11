"""`questa.compile` rule: one work library per action (architecture.md § Risks: compile per
library, not per module).

    vlib <worklib>
    vlog|vcom -modelsimini .ebs/modelsim.ini -work <worklib> [-L <lib>…] <opts…>
              [-f <filelist>] [<files>…]

Inputs: `filelist` (one file, passed with `-f`) and/or `files` (explicit source files, passed as
arguments); every `${steps.…}` input is an upstream library, mapped in the generated
modelsim.ini and, for vlog, searched with `-L`. Other inputs (`srcs: rtl/**/*.sv`, include
dirs) are only staged. The tool is `vcom` for VHDL and `vlog` otherwise: `params.lang` (`sv`,
`verilog`, `vhdl`) decides, else the extensions of `files`, else vlog.

Params: `lib` (required; the library's logical name, which must be the `lib` matrix value, or
the step name without a matrix, since consumers derive it from this action's id), `lang`,
`vlog_opts`, `vcom_opts` (split like a shell). Output `worklib` is the library directory:
`work/<lib>` and `deterministic: false` unless the flow declares it.
"""

from __future__ import annotations

import posixpath
from typing import Final

from ebs.core.errors import RuleError
from ebs.flow.model import OutputDef, StepDef
from ebs.rules.api import ActionTemplate, ExpandContext, param_text
from ebs.rules.questa._common import (
    QuestaRule,
    declared_or_default_output,
    script,
    script_argv,
    script_path,
    split_opts,
    step_inputs,
)
from ebs.rules.questa.ini import (
    MODELSIM_INI,
    check_library_name,
    fanin_libraries,
    library_name,
    modelsim_ini,
)

__all__ = ["LANGS", "SCRIPT_PATH", "QuestaCompileRule"]

KIND: Final = "questa.compile"
SCRIPT_PATH: Final = script_path(KIND)
LANGS: Final = {"sv": "vlog", "verilog": "vlog", "vhdl": "vcom"}
_EXTENSIONS: Final = {
    ".sv": "vlog",
    ".svh": "vlog",
    ".v": "vlog",
    ".vh": "vlog",
    ".vhd": "vcom",
    ".vhdl": "vcom",
}
_SOURCE_INPUTS: Final = ("filelist", "files")
_GLOB_CHARS: Final = frozenset("*?[")


class QuestaCompileRule(QuestaRule):
    kind = KIND
    version = "1"
    allowed_params = ("lib", "lang", "vlog_opts", "vcom_opts")
    required_params = ("lib",)

    def validate(self, step: StepDef) -> None:
        super().validate(step)
        if not any(name in step.inputs for name in _SOURCE_INPUTS):
            raise RuleError(
                f"a {KIND!r} step needs a 'filelist' input or a 'files' input (or both): the "
                "sources to compile"
            )

    def expand(self, step: StepDef, ctx: ExpandContext) -> ActionTemplate:
        self.validate(step)
        iid = ctx.instance.instance_id
        params = ctx.instance.params
        lib = check_library_name(param_text(params["lib"]), f"step {iid!r}: param lib")
        self._check_consumer_name(step, iid, lib)
        inputs = step_inputs(step, ctx)

        filelists = [*inputs.sources.get("filelist", ()), *inputs.refs.get("filelist", ())]
        if len(filelists) > 1:
            raise RuleError(f"step {iid!r}: input 'filelist' must be one file, got {filelists}")
        files = [*inputs.sources.get("files", ()), *inputs.refs.get("files", ())]
        for path in inputs.sources.get("files", ()):
            if _GLOB_CHARS & set(path):
                raise RuleError(
                    f"step {iid!r}: 'files' entry {path!r} is a glob; list the files to compile "
                    "(their order matters), or put them in a filelist and stage them with "
                    "another input such as 'srcs'"
                )
        tool = self._tool(iid, params.get("lang"), files)

        libraries = fanin_libraries(inputs.libraries(exclude=_SOURCE_INPUTS))
        if lib in libraries:
            raise RuleError(
                f"step {iid!r}: upstream library {libraries[lib]!r} has the same name as the "
                f"library this step compiles ({lib!r}); give one of them another 'lib' value"
            )
        worklib, outputs = declared_or_default_output(
            KIND, step, ctx, "worklib", OutputDef(dir=f"work/{lib}", deterministic=False)
        )
        opts_name = "vlog_opts" if tool == "vlog" else "vcom_opts"
        opts = split_opts(params.get(opts_name, ""), opts_name, iid)
        cmd = [tool, "-modelsimini", MODELSIM_INI, "-work", worklib]
        if tool == "vlog":  # VHDL finds libraries through `library` clauses and the ini
            for name in libraries:
                cmd += ["-L", name]
        cmd += opts
        for filelist in filelists:
            cmd += ["-f", filelist]
        cmd += files
        return ActionTemplate(
            argv=script_argv(KIND),
            env=dict(ctx.instance.env),
            outputs=outputs,
            config_files={
                SCRIPT_PATH: script(KIND, [["vlib", worklib], cmd]),
                MODELSIM_INI: modelsim_ini(KIND, libraries),
            },
        )

    def _check_consumer_name(self, step: StepDef, iid: str, lib: str) -> None:
        """Consumers name this library from this action's id (`ebs.rules.questa.ini`); compiling
        it under another name would break their references to it."""
        try:
            seen_as = library_name(f"worklib/{iid}")
        except RuleError as exc:
            raise RuleError(f"step {iid!r} compiles library {lib!r}, but {exc}") from None
        if seen_as != lib:
            fix = (
                "set 'lib: ${row.lib}' so the name is the matrix 'lib' value"
                if step.matrix is not None
                else f"name the step {lib!r} or set 'lib: {seen_as}'"
            )
            raise RuleError(
                f"step {iid!r} compiles library {lib!r}, but downstream steps will call it "
                f"{seen_as!r} (libraries are named after their producer's 'lib' matrix value, "
                f"or its step name without a matrix); {fix}"
            )

    def _tool(self, iid: str, lang: object, files: list[str]) -> str:
        if lang is not None:
            tool = LANGS.get(str(lang))
            if tool is None:
                raise RuleError(
                    f"step {iid!r}: lang {lang!r} is not supported; use one of {', '.join(LANGS)}"
                )
            return tool
        tools = set()
        for path in files:
            ext = posixpath.splitext(path)[1].lower()
            if ext not in _EXTENSIONS:
                raise RuleError(
                    f"step {iid!r}: cannot tell the language of {path!r} from its extension "
                    f"({', '.join(_EXTENSIONS)}); set params.lang"
                )
            tools.add(_EXTENSIONS[ext])
        if len(tools) > 1:
            raise RuleError(
                f"step {iid!r}: 'files' mixes Verilog and VHDL sources; compile them in separate "
                "steps (one library per language) or set params.lang"
            )
        return tools.pop() if tools else "vlog"
