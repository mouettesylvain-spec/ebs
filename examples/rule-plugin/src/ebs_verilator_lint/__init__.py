"""`verilator.lint`: an example EBS rule plugin (docs site: Tutorials > Write a rule plugin).

A step of this kind lints the files of its `rtl` input with Verilator:

    steps:
      lint:
        kind: verilator.lint
        toolchain: verilator
        inputs: { rtl: "rtl/alu/*.sv" }
        params: { top: alu, opts: "-Wno-DECLFILENAME" }

`expand` writes a small bash script as a config file (hashed into the action key, so editing the
generated command changes the key) and declares the implicit output `log`. `classify` keeps
the default crash and license handling and adds Verilator's own error shape; `summarize`
counts the warnings and errors for `ebs status`.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from ebs.core.errors import RuleError
from ebs.flow.model import OutputDef, StepDef, input_texts
from ebs.rules.api import (
    FAILED,
    GENERATED_DIR,
    INFRA,
    ActionTemplate,
    BaseRule,
    Classification,
    ExpandContext,
    param_text,
)

__all__ = ["VerilatorLintRule", "glob_word"]

SCRIPT_PATH: Final = f"{GENERATED_DIR}/verilator_lint.sh"
LOG: Final = "lint.log"
PARAMS: Final = ("top", "opts", "fatal_warnings")
_GLOB_RUN = re.compile(r"[*?]+|\[[^]/]*\]")
_MESSAGE = re.compile(r"^%(Warning|Error)(?:-[A-Z0-9_]+)?: ", re.MULTILINE)


def glob_word(pattern: str) -> str:
    """`pattern` as one bash word: glob characters stay live, everything else is quoted."""
    parts, pos = [], 0
    for m in _GLOB_RUN.finditer(pattern):
        if m.start() > pos:
            parts.append(shlex.quote(pattern[pos : m.start()]))
        parts.append(m.group())
        pos = m.end()
    if pos < len(pattern):
        parts.append(shlex.quote(pattern[pos:]))
    return "".join(parts)


class VerilatorLintRule(BaseRule):
    kind = "verilator.lint"
    version = "1"  # bump whenever the generated script changes: it is part of every action key

    def validate(self, step: StepDef) -> None:
        self._reject_fields(
            step, ("script", "command", "workdir", "target"), "the rule generates the command"
        )
        for name in step.params:
            if name not in PARAMS:
                raise RuleError(
                    f"a {self.kind!r} step has unknown param {name!r}: "
                    f"its params are {', '.join(PARAMS)}"
                )
        if "top" not in step.params:
            raise RuleError(f"a {self.kind!r} step's 'top' param is required")
        if "rtl" not in step.inputs:
            raise RuleError(f"a {self.kind!r} step needs an 'rtl' input: the files to lint")

    def expand(self, step: StepDef, ctx: ExpandContext) -> ActionTemplate:
        self.validate(step)
        params = ctx.instance.params
        patterns: list[str] = []
        for field, text in input_texts("rtl", step.inputs["rtl"]):
            rendered = ctx.render(text, field=field)
            patterns.extend(rendered if isinstance(rendered, list) else [rendered])
        flags = ["--lint-only", "-Wall", "--top-module", param_text(params["top"])]
        if not params.get("fatal_warnings", False):
            flags.append("-Wno-fatal")
        opts = shlex.split(param_text(params["opts"])) if "opts" in params else []
        script = "\n".join(
            [
                "set -eo pipefail",
                "shopt -s nullglob globstar",
                f"files=({' '.join(glob_word(p) for p in patterns)})",
                "[ ${#files[@]} -gt 0 ] || "
                '{ echo "verilator.lint: no file matches rtl" >&2; exit 1; }',
                f'verilator {shlex.join([*flags, *opts])} "${{files[@]}}" 2>&1 | tee {LOG}',
                "",
            ]
        )
        return ActionTemplate(
            argv=("bash", "--noprofile", "--norc", SCRIPT_PATH),
            env=dict(ctx.instance.env),
            config_files={SCRIPT_PATH: script},
            outputs={"log": OutputDef(file=LOG)},
        )

    def classify(
        self, exit_code: int, log_tail: str, outputs: Mapping[str, Path]
    ) -> Classification:
        if exit_code in (126, 127):  # bash: verilator not executable / not found
            return INFRA("tool_missing")
        default = super().classify(exit_code, log_tail, outputs)  # crash signals, licenses
        if default.status == "failed" and "%Error" in log_tail:
            return FAILED  # the design has lint errors: a valid, cacheable result
        return default

    def summarize(self, outputs: Mapping[str, Path], log: Path) -> dict[str, str | int]:
        source = outputs.get("log", log)
        try:
            text = source.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            text = ""
        kinds = [m.group(1) for m in _MESSAGE.finditer(text)]
        return {"warnings": kinds.count("Warning"), "errors": kinds.count("Error")}
