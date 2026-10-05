"""Regenerate the golden action-key fixtures (tests/fixtures/golden_keys/, invariant I4).

Usage: uv run python scripts/regen_golden_keys.py [--fixtures DIR]

Golden keys must never change silently: every cache entry is addressed by them. This script
refuses to run while the fixtures already carry the current `KEY_SCHEMA_VERSION`; bump the
version in src/ebs/plan/keys.py first and explain the change in the commit body. Each fixture
holds `{"name", "key_schema", "spec", "key"}`, with the spec encoded as in plan.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from ebs.core.digest import hash_bytes
from ebs.flow.model import Resources
from ebs.plan.keys import KEY_SCHEMA_VERSION, compute_key, nondeterministic_output_id
from ebs.plan.planfile import spec_to_json
from ebs.plan.types import (
    ActionOutputInput,
    ActionSpec,
    DebugSpec,
    InputRef,
    OutputSpec,
    RuleRef,
    SourceInput,
    ToolchainRef,
)

DEFAULT_FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "golden_keys"


def _resources(
    cpus: int | None = None, mem: int | None = None, time: int | None = None
) -> Resources:
    return Resources.model_construct(
        _fields_set={"cpus", "mem", "time"}, cpus=cpus, mem=mem, time=time
    )


def _spec(action_id: str, step: str, **fields: object) -> ActionSpec:
    base: dict[str, object] = {
        "action_id": action_id,
        "step": step,
        "params": {},
        "env": {},
        "toolchain": None,
        "inputs": (),
        "outputs": (),
        "config_files": {},
        "resources": _resources(),
        "licenses": {},
        "debug": DebugSpec(),
        "domain": "demo",
        "key": None,
    }
    base.update(fields)
    return ActionSpec(**base)  # type: ignore[arg-type]


def samples() -> dict[str, ActionSpec]:
    """The golden specs, by fixture name; they cover every key-document field."""
    questa = ToolchainRef("questa", "questa/2025.2", hash_bytes(b"golden toolchain questa"))
    compile_key = hash_bytes(b"golden compile key")
    return {
        "shell_script": _spec(
            "lint",
            "lint",
            rule=RuleRef("shell", "1"),
            argv=("bash", "--noprofile", "--norc", "-eo", "pipefail", ".ebs/script.sh"),
            params={"mode": "strict"},
            env={"LINT_LEVEL": "2"},
            config_files={".ebs/script.sh": "lint --mode strict rtl/top.sv > lint.txt\n"},
            inputs=(
                InputRef("rtl/top.sv", "file", SourceInput("rtl", "rtl/**"), hash_bytes(b"top")),
            ),
            outputs=(OutputSpec("report", "lint.txt", "file"),),
        ),
        "make_legacy": _spec(
            "lint_legacy",
            "lint_legacy",
            rule=RuleRef("make", "1"),
            argv=("make", "-C", "flows/lint", "lint", "STRICT=1"),
            params={"STRICT": 1},
            inputs=(
                InputRef(
                    "flows/lint/Makefile",
                    "file",
                    SourceInput("ebs.workdir", "flows/lint/**"),
                    hash_bytes(b"mk"),
                ),
                InputRef("rtl/alu.sv", "file", SourceInput("rtl", "rtl/**"), hash_bytes(b"alu")),
            ),
            outputs=(OutputSpec("reports", "reports/lint", "dir"),),
            runtime_env={"MAKEFLAGS": "-j$EBS_CPUS"},
            resources=_resources(4, 8 << 30, 1800),
        ),
        "questa_compile": _spec(
            "compile[filelist=core.f,lib=core]",
            "compile",
            rule=RuleRef("questa.compile", "1"),
            argv=("vlog", "-work", "work/core", "-f", "rtl/core.f", "-sv", "-timescale", "1ns/1ps"),
            params={"lib": "core", "vlog_opts": "-sv -timescale 1ns/1ps"},
            toolchain=questa,
            inputs=(
                InputRef(
                    "rtl/core.f", "file", SourceInput("filelist", "rtl/core.f"), hash_bytes(b"f")
                ),
                InputRef(
                    "rtl/core.sv", "file", SourceInput("srcs", "rtl/**/*.sv"), hash_bytes(b"c")
                ),
            ),
            outputs=(OutputSpec("worklib", "work/core", "dir", deterministic=False),),
            licenses={"msimhdlsim": 1},
        ),
        "questa_elab_fan_in": _spec(
            "elab",
            "elab",
            rule=RuleRef("questa.opt", "1"),
            argv=("vopt", "-L", "worklib/compile[filelist=core.f,lib=core]", "tb_top", "-o", "opt"),
            params={"top": "tb_top", "libs": ("core", "alu")},
            toolchain=questa,
            inputs=(
                InputRef(
                    "worklib/compile[filelist=core.f,lib=core]",
                    "tree",
                    ActionOutputInput("compile[filelist=core.f,lib=core]", "worklib"),
                    nondeterministic_output_id(compile_key, "worklib"),
                ),
            ),
            outputs=(OutputSpec("model", "opt", "dir", deterministic=False),),
            config_files={
                "modelsim.ini": "[Library]\ncore = worklib/compile[filelist=core.f,lib=core]\n"
            },
        ),
        "questa_sim_unicode": _spec(
            "sim[seed=17,test=caf%C3%A9]",
            "sim",
            rule=RuleRef("questa.sim", "1"),
            argv=("vsim", "-c", "-sv_seed", "17", "+name=café", "model/elab"),
            params={"test": "café", "seed": "17", "verbose": True, "timeout_s": -1},
            env={"UVM_TESTNAME": "café"},
            toolchain=questa,
            inputs=(
                InputRef(
                    "model/elab", "tree", ActionOutputInput("elab", "model"), hash_bytes(b"model")
                ),
            ),
            outputs=(
                OutputSpec("cov", "cov.ucdb", "file", optional=True),
                OutputSpec("result", "result.json", "file"),
            ),
            debug=DebugSpec(("transcript",), 2 << 30, False),
        ),
    }


def _fixture_schemas(directory: Path) -> list[int | None]:
    schemas: list[int | None] = []
    for path in sorted(directory.glob("*.json")):
        try:
            schema = json.loads(path.read_text(encoding="utf-8")).get("key_schema")
        except (OSError, ValueError, AttributeError):
            schema = None
        schemas.append(schema if isinstance(schema, int) else None)
    return schemas


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    args = parser.parse_args(argv)
    directory: Path = args.fixtures
    schemas = _fixture_schemas(directory)
    if any(s == KEY_SCHEMA_VERSION for s in schemas):
        print(
            f"refusing to regenerate: the fixtures in {directory} already use key schema "
            f"{KEY_SCHEMA_VERSION}. A changed key needs a KEY_SCHEMA_VERSION bump in "
            "src/ebs/plan/keys.py first (docs/design/invariants.md I4); if keys changed without "
            "an intended schema change, fix the regression instead.",
            file=sys.stderr,
        )
        return 1
    directory.mkdir(parents=True, exist_ok=True)
    for stale in directory.glob("*.json"):
        stale.unlink()
    for name, spec in samples().items():
        doc = {
            "name": name,
            "key_schema": KEY_SCHEMA_VERSION,
            "spec": spec_to_json(spec),
            "key": str(compute_key(spec)),
        }
        text = json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
        (directory / f"{name}.json").write_text(text, encoding="utf-8")
    print(f"wrote {len(samples())} golden keys (schema {KEY_SCHEMA_VERSION}) to {directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
