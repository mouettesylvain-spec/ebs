"""`questa.sim` rule: one simulation (a test with one seed) of an optimized model.

    printf '%s\\n' '<stub>' > <result>
    vsim -c -batch -modelsimini .ebs/modelsim.ini -lib <model> [-L <lib>…] -l transcript
         -do .ebs/questa_sim.do [-coverage] -sv_seed <seed> [+UVM_TESTNAME=<test>]
         <plusargs…> <vsim_opts…> opt

Inputs: `model` (required, the `model` output of a `questa.opt` step); every other `${steps.…}`
input is a library (modelsim.ini, `-L`); other inputs are only staged (memory images, …).
Params: `seed` (required, a non-negative integer: seeds are explicit table values), `test`
(the UVM test name; omit for UVM-free benches), `plusargs` (`+args`, split like a shell),
`vsim_opts`. Outputs: `result` (file, default `result.json`) and, when declared, `cov` (file:
coverage is enabled and saved there on exit).

`result.json` is written in two stages, because the runner classifies a run before it collects
its outputs: the script writes a stub naming the test, seed and the result's own path, then
`classify` (in the runner, after the tool exits) parses the transcript, decides, and replaces
the stub with the final document. The document holds no timestamps, so equal outcomes give
byte-identical results and downstream steps keep their keys (early cutoff).
"""

from __future__ import annotations

import contextlib
import json
import os
import posixpath
import re
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from ebs.core.errors import RuleError
from ebs.flow.model import OutputDef, StepDef
from ebs.rules.api import (
    FAILED,
    INFRA,
    PASSED,
    ActionTemplate,
    Classification,
    ExpandContext,
    default_classify,
    param_text,
)
from ebs.rules.questa._common import (
    TOOL_MISSING_EXITS,
    QuestaRule,
    declared_or_default_output,
    script,
    script_argv,
    script_path,
    split_opts,
    step_inputs,
)
from ebs.rules.questa.ini import MODELSIM_INI, fanin_libraries, modelsim_ini
from ebs.rules.questa.opt import OPT_UNIT
from ebs.rules.questa.transcript import TranscriptSummary, parse_transcript, sim_passed
from ebs.rules.tcl import tcl_quote

__all__ = ["DO_PATH", "RESULT_SCHEMA", "SCRIPT_PATH", "TRANSCRIPT", "QuestaSimRule"]

KIND: Final = "questa.sim"
SCRIPT_PATH: Final = script_path(KIND)
DO_PATH: Final = f"{posixpath.dirname(SCRIPT_PATH)}/questa_sim.do"
TRANSCRIPT: Final = "transcript"
"""vsim's `-l` transcript, at the root of the work dir (collect it with `debug.collect`)."""
RESULT_SCHEMA: Final = 1
_PENDING: Final = "ebs_pending"
_SEED_RE = re.compile(r"[0-9]+")
_TCL_BARE = re.compile(r"[A-Za-z0-9_./+-]+")
_SUMMARY_FIELDS: Final = (
    "status",
    "test",
    "seed",
    "uvm_errors",
    "uvm_fatals",
    "errors",
    "fatals",
    "signature",
)


def _tcl_word(value: str) -> str:
    return value if _TCL_BARE.fullmatch(value) else tcl_quote(value)


class QuestaSimRule(QuestaRule):
    kind = KIND
    version = "1"
    allowed_params = ("seed", "test", "plusargs", "vsim_opts")
    required_params = ("seed",)

    def validate(self, step: StepDef) -> None:
        super().validate(step)
        if "model" not in step.inputs:
            raise RuleError(
                f"a {KIND!r} step's 'model' input is required: the optimized design, e.g. "
                "model: ${steps.elab.outputs.model}"
            )

    def expand(self, step: StepDef, ctx: ExpandContext) -> ActionTemplate:
        self.validate(step)
        iid = ctx.instance.instance_id
        params = ctx.instance.params
        inputs = step_inputs(step, ctx)
        model = inputs.refs.get("model", [])
        if len(model) != 1 or "model" in inputs.sources:
            raise RuleError(
                f"step {iid!r}: the 'model' input must be one ${{steps.<step>.outputs.model}} "
                f"reference to a single {'questa.opt'!r} action, got {len(model)} staged model(s)"
            )
        libraries = fanin_libraries(inputs.libraries(exclude=("model",)))

        seed = param_text(params["seed"])
        if isinstance(params["seed"], bool) or not _SEED_RE.fullmatch(seed):
            raise RuleError(
                f"step {iid!r}: seed {seed!r} must be a non-negative integer: seeds are explicit "
                "table values (`ebs seeds gen` writes them), never generated at run time"
            )
        test: str | None = None
        if "test" in params:
            test = param_text(params["test"])
            if not test or any(c.isspace() for c in test):
                raise RuleError(f"step {iid!r}: test {test!r} must be one non-empty word")
        plusargs = split_opts(params.get("plusargs", ""), "plusargs", iid)
        for arg in plusargs:
            if not arg.startswith("+"):
                raise RuleError(
                    f"step {iid!r}: plusarg {arg!r} must start with '+'; pass simulator "
                    "options in vsim_opts"
                )

        result, outputs = declared_or_default_output(
            KIND, step, ctx, "result", OutputDef(file="result.json")
        )
        cov: str | None = None
        if "cov" in step.outputs:
            cov, _ = declared_or_default_output(KIND, step, ctx, "cov", OutputDef(file="cov.ucdb"))
        if cov is not None and posixpath.normpath(cov) == posixpath.normpath(result):
            raise RuleError(f"step {iid!r}: outputs 'result' and 'cov' both use {result!r}")
        for path in (result, cov):
            if path is not None and posixpath.normpath(path) == TRANSCRIPT:
                raise RuleError(
                    f"step {iid!r}: output path {path!r} is where vsim writes its transcript; "
                    "choose another path"
                )

        stub = json.dumps(
            {_PENDING: KIND, "result": posixpath.normpath(result), "seed": seed, "test": test},
            sort_keys=True,
            separators=(",", ":"),
        )
        dirs = sorted(
            {d for p in (result, cov) if p for d in [posixpath.dirname(posixpath.normpath(p))] if d}
        )
        prelude = [f"mkdir -p {' '.join(map(shlex.quote, dirs))}"] if dirs else []
        prelude.append(f"printf '%s\\n' {shlex.quote(stub)} > {shlex.quote(result)}")

        cmd = ["vsim", "-c", "-batch", "-modelsimini", MODELSIM_INI, "-lib", model[0]]
        for name in libraries:
            cmd += ["-L", name]
        cmd += ["-l", TRANSCRIPT, "-do", DO_PATH]
        if cov is not None:
            cmd.append("-coverage")
        cmd += ["-sv_seed", seed]
        if test is not None:
            cmd.append(f"+UVM_TESTNAME={test}")
        cmd += plusargs
        cmd += split_opts(params.get("vsim_opts", ""), "vsim_opts", iid)
        cmd.append(OPT_UNIT)

        do = [] if cov is None else [f"coverage save -onexit {_tcl_word(cov)}"]
        do += ["run -all", "quit -f"]
        return ActionTemplate(
            argv=script_argv(KIND),
            env=dict(ctx.instance.env),
            outputs=outputs,
            config_files={
                SCRIPT_PATH: script(KIND, [cmd], prelude=prelude),
                DO_PATH: "\n".join(do) + "\n",
                MODELSIM_INI: modelsim_ini(KIND, libraries),
            },
        )

    # --- after the run (runner side) ------------------------------------------------------------

    def classify(
        self, exit_code: int, log_tail: str, outputs: Mapping[str, Path]
    ) -> Classification:
        result = outputs.get("result")
        final = _read_final(result) if result is not None else None
        if final is not None:  # already classified (and finalized): the same answer again
            return PASSED if final["status"] == "passed" else FAILED
        stub = _read_stub(result) if result is not None else None
        transcript = _transcript(result, stub) if result is not None and stub else None
        if transcript is not None and transcript.is_file():
            with open(transcript, encoding="utf-8", errors="replace") as f:
                summary = parse_transcript(f)
            found: Path | None = transcript
        else:
            summary = parse_transcript(log_tail.splitlines())
            found = None

        classification = self._decide(exit_code, log_tail, found, summary)
        # Only a stub that matches where it lies is finalized; anything else is left as it is.
        if (
            result is not None
            and stub is not None
            and transcript is not None
            and classification.status != "infra"
        ):
            _write_result(result, stub, exit_code, summary, classification)
        return classification

    def _decide(
        self,
        exit_code: int,
        log_tail: str,
        transcript: Path | None,
        summary: TranscriptSummary,
    ) -> Classification:
        if sim_passed(exit_code, summary):
            return PASSED
        crash = default_classify(exit_code, "", ())
        if crash.status == "infra":
            return crash
        if exit_code in TOOL_MISSING_EXITS:
            return INFRA("tool_missing")
        # A run that reached $finish got its licenses: a license message is history then.
        if not summary.finished and self._license_error(log_tail, transcript):
            return INFRA("license")
        if summary.crashed:
            return INFRA("tool_crash")
        return FAILED

    def _license_error(self, log_tail: str, transcript: Path | None) -> bool:
        if any(p.search(log_tail) for p in self._license_patterns):
            return True
        if transcript is None:
            return False
        with open(transcript, encoding="utf-8", errors="replace") as f:
            return any(p.search(line) for line in f for p in self._license_patterns)

    def summarize(self, outputs: Mapping[str, Path], log: Path) -> dict[str, str | int]:
        result = outputs.get("result")
        doc = _read_final(result) if result is not None else None
        if doc is None:
            return {}
        return {
            name: value
            for name in _SUMMARY_FIELDS
            if isinstance(value := doc.get(name), str | int) and not isinstance(value, bool)
        }


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _read_final(path: Path) -> dict[str, Any] | None:
    doc = _read_json(path)
    if doc is None or doc.get("schema") != RESULT_SCHEMA:
        return None
    return doc if doc.get("status") in ("passed", "failed") else None


def _read_stub(path: Path) -> dict[str, Any] | None:
    doc = _read_json(path)
    return doc if doc is not None and doc.get(_PENDING) == KIND else None


def _transcript(result: Path, stub: Mapping[str, Any]) -> Path | None:
    """The transcript next to the work dir root, found from the result's recorded logical path;
    None if the record does not match where the result actually is."""
    logical = stub.get("result")
    if not isinstance(logical, str):
        return None
    parts = logical.split("/")
    if logical.startswith("/") or any(p in ("", ".", "..") for p in parts):
        return None
    if list(result.parts[-len(parts) :]) != parts:
        return None
    work = result.parents[len(parts) - 1]
    return work / TRANSCRIPT


def _write_result(
    result: Path,
    stub: Mapping[str, Any],
    exit_code: int,
    summary: TranscriptSummary,
    classification: Classification,
) -> None:
    doc = {
        "schema": RESULT_SCHEMA,
        "status": classification.status,
        "test": stub.get("test"),
        "seed": stub.get("seed"),
        "exit_code": exit_code,
        "finished": summary.finished,
        "uvm_errors": summary.uvm_errors,
        "uvm_fatals": summary.uvm_fatals,
        "errors": summary.errors,
        "fatals": summary.fatals,
        "first_error": summary.first_error,
        "signature": summary.signature,
    }
    tmp = result.with_name(result.name + ".ebs-tmp")
    tmp.write_text(json.dumps(doc, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    try:
        os.replace(tmp, result)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
