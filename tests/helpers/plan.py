"""Shared helpers for planner tests: fake snapshotter, toolchains and rules, flow builders."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ebs.cas.api import CAS
from ebs.core.digest import Digest, hash_bytes
from ebs.core.errors import SourceError
from ebs.core.tree import TreeEntry, TreeManifest
from ebs.flow.model import Flow, Resources, StepDef
from ebs.plan.planner import Planner
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
from ebs.rules.api import ActionTemplate, BaseRule, ExpandContext, param_text
from ebs.rules.make import MakeRule
from ebs.rules.registry import RuleRegistry
from ebs.rules.shell import ShellRule
from ebs.sources.snapshot import SnapshotResult
from ebs.toolchain.model import Toolchain

TOOLS_ROOT = Path("/opt/eda-tools")


def _glob_regex(pattern: str) -> re.Pattern[str]:
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


class FakeSnapshotter:
    """Snapshots from an in-memory file dict ({relative path: content}) into a real CAS.

    Globs: `**`, `*`, `?`; a pattern naming a directory means `dir/**`. `links` holds symlinks
    ({path: target}). Every call is recorded in `calls`.
    """

    def __init__(
        self,
        cas: CAS,
        files: Mapping[str, str],
        *,
        links: Mapping[str, str] | None = None,
        rehash: bool = False,
    ) -> None:
        self._cas = cas
        self.files = dict(files)
        self.links = dict(links or {})
        self._rehash = rehash
        self.calls: list[tuple[Path, str]] = []

    @property
    def rehash(self) -> bool:
        return self._rehash

    def snapshot(self, base: Path, pattern: str, *, optional: bool = False) -> SnapshotResult:
        self.calls.append((base, pattern))
        regex = _glob_regex(pattern)
        prefix = pattern.rstrip("/") + "/"
        paths = sorted(
            p for p in (*self.files, *self.links) if regex.fullmatch(p) or p.startswith(prefix)
        )
        if not paths and not optional:
            raise SourceError(f"source pattern {pattern!r} matched nothing under {base}")
        root: dict[str, Any] = {}
        for path in paths:
            *dirs, leaf = path.split("/")
            node = root
            for d in dirs:
                node = node.setdefault(d, {})
            node[leaf] = path
        digest, size = self._store(root)
        return SnapshotResult(digest, tuple(paths), size)

    def _store(self, node: dict[str, Any]) -> tuple[Digest, int]:
        entries = []
        total = 0
        for name, child in node.items():
            if isinstance(child, dict):
                d, size = self._store(child)
                entries.append(TreeEntry(name, "dir", d, size, False, None))
            elif child in self.links:
                entries.append(TreeEntry(name, "symlink", None, 0, False, self.links[child]))
                size = 0
            else:
                data = self.files[child].encode()
                size = len(data)
                entries.append(
                    TreeEntry(name, "file", self._cas.put_bytes(data), size, False, None)
                )
            total += size
        entries.sort(key=lambda e: e.name.encode())
        return self._cas.put_manifest(TreeManifest(tuple(entries))), total


class FakeToolchains:
    """Resolves any module to a toolchain with a pinned fingerprint; `bump` changes one's id."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.salt: dict[str, str] = {}

    def resolve(self, name: str, module: str) -> Toolchain:
        self.calls.append((name, module))
        return Toolchain.from_parts(
            name=name,
            module=module,
            version=module.rsplit("/", 1)[-1],
            install_roots=(TOOLS_ROOT / module,),
            env={"TOOL_HOME": str(TOOLS_ROOT / module)},
            fingerprint=hash_bytes(f"{module}{self.salt.get(module, '')}".encode()),
        )


class FakeQuestaRule(BaseRule):
    """Stand-in for the P1-08 Questa rules: `<tool> k=v…` with params sorted, plus `command`."""

    version = "1"

    def __init__(self, kind: str) -> None:
        super().__init__()
        self.kind = kind

    def expand(self, step: StepDef, ctx: ExpandContext) -> ActionTemplate:
        params = ctx.instance.params
        argv = [self.kind.rsplit(".", 1)[-1]]
        argv += [f"{k}={param_text(params[k])}" for k in sorted(params)]
        for i, element in enumerate(step.command or ()):
            value = ctx.render(element, field=f"command[{i}]")
            argv.extend(value if isinstance(value, list) else [value])
        return ActionTemplate(argv=tuple(argv), env=dict(ctx.instance.env))


def registry() -> RuleRegistry:
    return RuleRegistry(
        [
            ShellRule(),
            MakeRule(),
            FakeQuestaRule("questa.compile"),
            FakeQuestaRule("questa.opt"),
            FakeQuestaRule("questa.sim"),
        ]
    )


def flow(steps: Mapping[str, Mapping[str, Any]], **top: Any) -> Flow:
    data: dict[str, Any] = {
        "version": 1,
        "project": "demo",
        "domain": "test",
        "toolchains": {"questa": {"module": "questa/2025.2", "licenses": {"msimhdlsim": 1}}},
        "steps": steps,
    }
    data.update(top)
    return Flow.model_validate(data)


def planner(cas: CAS, files: Mapping[str, str], **kwargs: Any) -> tuple[Planner, FakeSnapshotter]:
    snap = FakeSnapshotter(cas, files, **kwargs)
    return Planner(cas, snap, FakeToolchains(), registry()), snap


def write_tables(base: Path, tables: Mapping[str, str]) -> None:
    for name, text in tables.items():
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


RTL = {
    "rtl/core.sv": "module core; endmodule\n",
    "rtl/alu.sv": "module alu; endmodule\n",
    "rtl/core.f": "core.sv\n",
    "rtl/alu.f": "alu.sv\n",
    "flows/lint/Makefile": "lint:\n\ttrue\n",
}
LIBS_CSV = "lib,filelist\ncore,core.f\nalu,alu.f\n"
REGRESSION_CSV = (
    "test,seed,plusargs,timeout\nsmoke,1,,10m\nsmoke,2,+verbose,10m\nalu_rand,17,+a=1,1h\n"
)

# The architecture.md example flow with local sources instead of an import (P2-03).
ARCH_STEPS: dict[str, Any] = {
    "compile": {
        "kind": "questa.compile",
        "toolchain": "questa",
        "matrix": {"table": "libs.csv"},
        "inputs": {"srcs": "rtl/**/*.sv", "filelist": "rtl/${row.filelist}"},
        "params": {"lib": "${row.lib}", "vlog_opts": "-sv -timescale 1ns/1ps"},
        "outputs": {"worklib": {"dir": "work/${row.lib}", "deterministic": False}},
        "resources": {"cpus": 2, "mem": "8G", "time": "30m"},
    },
    "elab": {
        "kind": "questa.opt",
        "toolchain": "questa",
        "inputs": {"libs": "${steps.compile.outputs.worklib[*]}"},
        "params": {"top": "tb_top"},
        "outputs": {"model": {"dir": "opt", "deterministic": False}},
    },
    "sim": {
        "kind": "questa.sim",
        "toolchain": "questa",
        "matrix": {"table": "tests/regression.csv"},
        "inputs": {"model": "${steps.elab.outputs.model}"},
        "params": {"test": "${row.test}", "seed": "${row.seed}", "plusargs": "${row.plusargs}"},
        "outputs": {"result": "result.json", "cov": {"file": "cov.ucdb", "optional": True}},
        "debug": {"collect": ["transcript", "*.log"], "max_size": "2G"},
        "resources": {"cpus": 1, "mem": "4G", "time": "${row.timeout}"},
    },
    "lint_legacy": {
        "kind": "make",
        "workdir": "flows/lint",
        "target": "lint",
        "inputs": {"rtl": "rtl/**", "flow": "flows/lint/**"},
        "outputs": {"reports": {"dir": "reports/lint"}},
        "toolchain": "spyglass",
    },
}
ARCH_TOOLCHAINS = {
    "questa": {"module": "questa/2025.2", "licenses": {"msimhdlsim": 1}},
    "spyglass": {"module": "vc_spyglass/2025.06", "licenses": {"vcspyglass": 1}},
}


def arch_flow(base: Path, steps: Mapping[str, Any] | None = None) -> Flow:
    write_tables(base, {"libs.csv": LIBS_CSV, "tests/regression.csv": REGRESSION_CSV})
    return flow(
        steps if steps is not None else ARCH_STEPS,
        project="rv32x-cpu",
        domain="cpu-nda",
        toolchains=ARCH_TOOLCHAINS,
    )


def sample_spec(**overrides: Any) -> ActionSpec:
    """A small, fully keyed-ready ActionSpec for key and planfile tests."""
    fields: dict[str, Any] = {
        "action_id": "sim[seed=1,test=smoke]",
        "step": "sim",
        "rule": RuleRef("questa.sim", "1"),
        "argv": ("vsim", "-c", "model/elab"),
        "params": {"seed": "1", "test": "smoke", "n": 3, "flag": True, "libs": ("a", "b")},
        "env": {"UVM_VERBOSITY": "LOW"},
        "toolchain": ToolchainRef("questa", "questa/2025.2", hash_bytes(b"questa")),
        "inputs": (
            InputRef(
                "model/elab", "tree", ActionOutputInput("elab", "model"), hash_bytes(b"model")
            ),
            InputRef("rtl/a.sv", "file", SourceInput("srcs", "rtl/**"), hash_bytes(b"a")),
        ),
        "outputs": (
            OutputSpec("cov", "cov.ucdb", "file", True, True),
            OutputSpec("result", "result.json", "file"),
        ),
        "config_files": {".ebs/script.sh": "echo hi\n"},
        "resources": resources(1, 4 << 30, 600),
        "licenses": {"msimhdlsim": 1},
        "debug": DebugSpec(("transcript",), 2 << 30, False),
        "domain": "cpu-nda",
        "key": None,
        "runtime_env": {"MAKEFLAGS": "-j$EBS_CPUS"},
    }
    fields.update(overrides)
    return ActionSpec(**fields)


def resources(cpus: int | None, mem: int | None, time: int | None) -> Resources:
    return Resources.model_construct(
        _fields_set={"cpus", "mem", "time"}, cpus=cpus, mem=mem, time=time
    )
