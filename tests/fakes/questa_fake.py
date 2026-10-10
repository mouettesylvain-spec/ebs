"""Fake Questa tools for tests (docs/design/testing.md § Fake tools): vlib, vmap, vlog, vcom,
vopt and vsim, each a thin launcher in `tests/fakes/bin/` calling `main(<tool>, argv)`.

They reproduce what the questa rules depend on, nothing more:
- argv parsing of the options the rules generate; `-modelsimini` mappings are checked (every
  mapped library must exist) and `-L` names must be mapped;
- libraries as directories: `vlib` writes `_info`, compilers write one `<unit>/_primary.dat` per
  module/package/entity; every file embeds a nanosecond timestamp, so reruns differ in bytes
  (outputs are `deterministic: false`);
- cross-library resolution: a vlog `import p::*` must find package `p` in the work library or a
  `-L` library; a VHDL `library x;` clause must name a mapped library; vopt tops must exist;
  vsim needs every library the optimized design was built from mapped in its modelsim.ini;
- transcript shapes (`# ` prefixed lines in vsim's `-l` transcript and stdout, UVM report
  summary, `** Error` / `** Fatal`, `** Note: $finish`) and exit codes.

Behaviour is selected by `FAKE_QUESTA_MODE` (vsim; a `+FAKE_QUESTA_MODE=<mode>` plusarg wins) or
`FAKE_QUESTA_MODE_<TOOL>` (any tool): pass, uvm_error, fatal, no_finish, license_fail, segv,
hang. A source line containing `FAKE_COMPILE_ERROR` is a compile error. Message texts are
generic shapes written for these tests, not copied from any vendor tool.
"""

from __future__ import annotations

import hashlib
import os
import re
import resource
import signal
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

MODES = ("pass", "uvm_error", "fatal", "no_finish", "license_fail", "segv", "hang")

_VALUE_OPTS = {
    "vlog": {"-modelsimini", "-work", "-L", "-f", "-timescale", "-suppress", "-error"},
    "vcom": {"-modelsimini", "-work", "-f", "-suppress", "-error"},
    "vopt": {"-modelsimini", "-work", "-L", "-o", "-suppress", "-error"},
    "vsim": {"-modelsimini", "-lib", "-L", "-l", "-do", "-sv_seed", "-t", "-suppress"},
}
_VLOG_UNIT = re.compile(r"^\s*(?:module|package|interface|program)\s+(\w+)", re.MULTILINE)
_VLOG_IMPORT = re.compile(r"\bimport\s+(\w+)\s*::")
_VHDL_UNIT = re.compile(r"^\s*(?:entity|package)\s+(\w+)\s+is\b", re.MULTILINE | re.IGNORECASE)
_VHDL_LIBRARY = re.compile(r"^\s*library\s+(\w+)\s*;", re.MULTILINE | re.IGNORECASE)
_STANDARD_LIBS = {"std", "ieee", "work"}


class Failure(Exception):
    """A tool error: printed as `** <severity>: <message>`, exit `code`."""

    def __init__(self, message: str, code: int = 2, severity: str = "Error") -> None:
        super().__init__(message)
        self.code = code
        self.severity = severity


@dataclass
class Args:
    options: dict[str, list[str]] = field(default_factory=dict)
    flags: set[str] = field(default_factory=set)
    plusargs: list[str] = field(default_factory=list)
    positional: list[str] = field(default_factory=list)

    def one(self, name: str, default: str | None = None) -> str | None:
        values = self.options.get(name)
        return values[-1] if values else default

    def many(self, name: str) -> list[str]:
        return self.options.get(name, [])


def parse(tool: str, argv: Sequence[str]) -> Args:
    args = Args()
    takes_value = _VALUE_OPTS.get(tool, set())
    it = iter(argv)
    for arg in it:
        if arg in takes_value:
            value = next(it, None)
            if value is None:
                raise Failure(f"({tool}-1000) Option {arg} needs a value.")
            args.options.setdefault(arg, []).append(value)
        elif arg.startswith("-"):
            args.flags.add(arg)
        elif arg.startswith("+"):
            args.plusargs.append(arg)
        else:
            args.positional.append(arg)
    return args


def stamp() -> str:
    return f"written {time.time_ns()} by fake-questa pid {os.getpid()}\n"


def read_ini(path: str | None) -> dict[str, str]:
    """`[Library]` mappings of a modelsim.ini (`others` excluded)."""
    if path is None:
        return {}
    ini = Path(path)
    if not ini.is_file():
        raise Failure(f"(vsim-1001) Cannot open modelsim.ini file {path}.")
    mappings: dict[str, str] = {}
    section = None
    for raw in ini.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
        elif section == "Library" and "=" in line:
            name, _, value = line.partition("=")
            if name.strip() != "others":
                mappings[name.strip()] = value.strip()
    return mappings


def check_library(path: str, what: str) -> Path:
    lib = Path(path)
    if not (lib / "_info").is_file():
        raise Failure(f"(vlib-1002) Library {what} not found (no library at '{path}').")
    return lib


def mapped_libraries(args: Args) -> dict[str, Path]:
    """Every library the ini maps must exist; every -L name must be mapped."""
    ini = read_ini(args.one("-modelsimini"))
    libs = {name: check_library(path, name) for name, path in ini.items()}
    for name in args.many("-L"):
        if name not in libs:
            raise Failure(f"(vlog-1003) Library '{name}' is not mapped in the modelsim.ini.")
    return libs


def tool_mode(tool: str) -> str:
    mode = os.environ.get(f"FAKE_QUESTA_MODE_{tool.upper()}", "pass")
    if mode not in MODES:
        raise Failure(f"fake {tool}: unknown mode {mode!r}; one of {', '.join(MODES)}")
    return mode


def infra_mode(mode: str, out: Callable[[str], None]) -> None:
    """The modes every tool shares: license failure, crash, hang."""
    if mode == "license_fail":
        out("** Error: License checkout failed for feature 'simfeature' (status -5)")
        raise Failure("Unable to checkout a license. Check the license settings.", 3, "Fatal")
    if mode == "segv":
        out("** Fatal: (SIGSEGV) Bad handle or reference.")
        sys.stdout.flush()
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))  # no core file in the work dir
        signal.signal(signal.SIGSEGV, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGSEGV)
    if mode == "hang":
        out("waiting for a license that never comes back")
        sys.stdout.flush()
        while True:
            time.sleep(60)


def print_line(text: str) -> None:
    print(text, flush=True)


# --- vlib / vmap ------------------------------------------------------------------------------


def vlib(argv: Sequence[str]) -> int:
    args = parse("vlib", argv)
    if len(args.positional) != 1:
        raise Failure("(vlib-1004) Usage: vlib <library directory>")
    lib = Path(args.positional[0])
    lib.mkdir(parents=True, exist_ok=True)
    (lib / "_info").write_text("fake-questa library\n" + stamp())
    return 0


def vmap(argv: Sequence[str]) -> int:
    args = parse("vmap", argv)
    if len(args.positional) != 2:
        raise Failure("(vmap-1005) Usage: vmap <name> <path>")
    ini = Path("modelsim.ini")
    text = ini.read_text() if ini.exists() else "[Library]\n"
    ini.write_text(text + f"{args.positional[0]} = {args.positional[1]}\n")
    return 0


# --- compilers --------------------------------------------------------------------------------


def _filelist_sources(path: str) -> list[str]:
    fl = Path(path)
    if not fl.is_file():
        raise Failure(f'(vlog-1006) Cannot open filelist "{path}".')
    sources = []
    for raw in fl.read_text().splitlines():
        line = raw.split("//", 1)[0].split("#", 1)[0].strip()
        sources += [t for t in line.split() if not t.startswith(("+", "-"))]
    return sources


def compile_sources(tool: str, argv: Sequence[str]) -> int:
    infra_mode(tool_mode(tool), print_line)
    args = parse(tool, argv)
    work_path = args.one("-work", "work")
    assert work_path is not None
    work = check_library(work_path, "work")
    libs = mapped_libraries(args)
    sources = [s for f in args.many("-f") for s in _filelist_sources(f)] + args.positional
    if not sources:
        raise Failure(f"({tool}-1007) No source files given.")
    units: dict[str, str] = {}
    errors: list[str] = []
    texts = {}
    for src in sources:
        path = Path(src)
        if not path.is_file():
            raise Failure(f'({tool}-1008) Cannot open source file "{src}".')
        text = path.read_text()
        texts[src] = text
        for lineno, line in enumerate(text.splitlines(), 1):
            if "FAKE_COMPILE_ERROR" in line:
                errors.append(f"** Error: {src}({lineno}): ({tool}-1009) Syntax error near 'x'.")
        pattern = _VLOG_UNIT if tool == "vlog" else _VHDL_UNIT
        for unit in pattern.findall(text):
            units[unit.lower() if tool == "vcom" else unit] = src
    for src, text in texts.items():
        if tool == "vlog":
            search = [work, *(libs[name] for name in args.many("-L"))]
            for pkg in _VLOG_IMPORT.findall(text):
                if pkg not in units and not any((lib / pkg).is_dir() for lib in search):
                    errors.append(
                        f"** Error: {src}: ({tool}-1010) Package '{pkg}' is not in the work "
                        "library or any -L library."
                    )
        else:
            for name in _VHDL_LIBRARY.findall(text):
                if name.lower() not in _STANDARD_LIBS and name not in libs:
                    errors.append(f"** Error: {src}: ({tool}-1011) Library '{name}' not found.")
    for unit in sorted(units):
        print_line(f"-- Compiling {unit}")
    if errors:
        for line in errors:
            print_line(line)
        print_line(f"Errors: {len(errors)}, Warnings: 0")
        return 2
    for unit, src in units.items():
        digest = hashlib.sha256(texts[src].encode()).hexdigest()
        (work / unit).mkdir(exist_ok=True)
        (work / unit / "_primary.dat").write_text(
            f"unit {unit} from {src} sha256 {digest}\n" + stamp()
        )
    with open(work / "_info", "a") as f:
        f.write(stamp())
    print_line("Errors: 0, Warnings: 0")
    return 0


# --- vopt -------------------------------------------------------------------------------------


def vopt(argv: Sequence[str]) -> int:
    infra_mode(tool_mode("vopt"), print_line)
    args = parse("vopt", argv)
    work_path = args.one("-work", "work")
    assert work_path is not None
    work = check_library(work_path, "work")
    libs = mapped_libraries(args)
    name = args.one("-o")
    if name is None or not args.positional:
        raise Failure("(vopt-1012) Usage: vopt [options] <top>… -o <name>")
    search = [work, *(libs[n] for n in args.many("-L"))]
    for top in args.positional:
        unit = top.split(".")[-1].split("(")[0]
        if not any((lib / unit / "_primary.dat").is_file() for lib in search):
            raise Failure(f"(vopt-1013) Design unit '{top}' not found in the search libraries.")
    (work / name).mkdir(exist_ok=True)
    (work / name / "_primary.dat").write_text(
        f"optimized {' '.join(args.positional)} libs {' '.join(args.many('-L'))}\n" + stamp()
    )
    print_line(f"Optimized design name is {name}")
    print_line("Errors: 0, Warnings: 0")
    return 0


# --- vsim -------------------------------------------------------------------------------------


@dataclass
class Transcript:
    """vsim output: stdout and the `-l` file, each line prefixed with `# `."""

    file: TextIO | None

    def __call__(self, text: str) -> None:
        line = f"# {text}" if text else "#"
        print(line, flush=True)
        if self.file is not None:
            self.file.write(line + "\n")
            self.file.flush()


def _do_commands(path: str | None) -> tuple[list[str], bool]:
    """(coverage files to save, whether the script runs the simulation)."""
    if path is None:
        raise Failure("(vsim-1014) Batch mode needs a -do script.")
    do = Path(path)
    if not do.is_file():
        raise Failure(f"(vsim-1015) Cannot open do file '{path}'.")
    saves, runs = [], False
    for raw in do.read_text().splitlines():
        words = raw.split()
        if words[:2] == ["coverage", "save"]:
            target = [w for w in words[2:] if not w.startswith("-")]
            saves.append(" ".join(target).strip("{}"))
        elif words[:1] == ["run"]:
            runs = True
    return saves, runs


def vsim(argv: Sequence[str]) -> int:
    args = parse("vsim", argv)
    mode = tool_mode("vsim") if "FAKE_QUESTA_MODE_VSIM" in os.environ else None
    mode = mode or os.environ.get("FAKE_QUESTA_MODE", "pass")
    for plusarg in args.plusargs:
        if plusarg.startswith("+FAKE_QUESTA_MODE="):
            mode = plusarg.partition("=")[2]
    if mode not in MODES:
        raise Failure(f"fake vsim: unknown mode {mode!r}; one of {', '.join(MODES)}")
    log_path = args.one("-l")
    log = open(log_path, "w") if log_path else None  # noqa: SIM115 - closed at exit
    out = Transcript(log)
    try:
        return _simulate(args, mode, out)
    except Failure as exc:
        out(f"** {exc.severity}: {exc}")
        exc.args = ()
        raise
    finally:
        if log is not None:
            log.close()


def _simulate(args: Args, mode: str, out: Transcript) -> int:
    if not {"-c", "-batch"} & args.flags:
        raise Failure("(vsim-1016) The fake simulator only runs in batch mode (-c / -batch).")
    infra_mode(mode, out)
    lib_path = args.one("-lib", "work")
    assert lib_path is not None
    lib = check_library(lib_path, "work")
    libs = mapped_libraries(args)
    if len(args.positional) != 1:
        raise Failure("(vsim-1017) Give exactly one design unit to simulate.")
    unit = args.positional[0]
    if not (lib / unit / "_primary.dat").is_file():
        raise Failure(f"(vsim-1018) Design unit '{unit}' not found in library '{lib_path}'.")
    # The optimized design refers to the libraries it was optimized against.
    header = (lib / unit / "_primary.dat").read_text().splitlines()[0]
    needed = header.partition(" libs ")[2].split()
    missing = [name for name in needed if name not in libs]
    if missing:
        raise Failure(
            f"(vsim-1019) Library {', '.join(missing)} used by '{unit}' is not mapped "
            "in the modelsim.ini."
        )
    saves, runs = _do_commands(args.one("-do"))
    seed = int(args.one("-sv_seed", "0") or 0)
    test = next((p.partition("=")[2] for p in args.plusargs if p.startswith("+UVM_TESTNAME=")), "")

    out(f"Loading work.{unit}(fast)")
    out(f"Seed: {seed}")
    if not runs:
        out("Errors: 0, Warnings: 0")
        return 0
    uvm_errors = 0
    if test:
        out(f"UVM_INFO @ 0: reporter [RNTST] Running test {test}...")
    if mode == "uvm_error":
        uvm_errors = 1
        out(
            f"UVM_ERROR tb/scoreboard.sv(88) @ {1000 + seed}: uvm_test_top.env.sb [SCB] "
            f"data mismatch at 0x{seed:x}: expected 'h{seed % 256:02x} got 'h{(seed + 1) % 256:02x}"
        )
    if mode == "fatal":
        out(f"** Fatal: Assertion failed: handshake lost after {seed % 16} cycles")
        out(f"   Time: {500 + seed} ns  Scope: tb_top.chk File: tb/tb_top.sv Line: 55")
        return 1
    if test:
        out("")
        out("--- UVM Report Summary ---")
        out("")
        out("** Report counts by severity")
        out("UVM_INFO :    1")
        out(f"UVM_ERROR :    {uvm_errors}")
        out("UVM_FATAL :    0")
    if mode != "no_finish":
        out("** Note: $finish    : tb/tb_top.sv(40)")
        out(f"   Time: {1500 + seed} ns  Iteration: 0  Instance: /tb_top")
    for target in saves:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_text(f"fake coverage db test={test} seed={seed}\n" + stamp())
    out("Errors: 0, Warnings: 0")
    return 0


TOOLS: dict[str, Callable[[Sequence[str]], int]] = {
    "vlib": vlib,
    "vmap": vmap,
    "vlog": lambda argv: compile_sources("vlog", argv),
    "vcom": lambda argv: compile_sources("vcom", argv),
    "vopt": vopt,
    "vsim": vsim,
}


def main(tool: str, argv: Sequence[str]) -> int:
    try:
        return TOOLS[tool](argv)
    except Failure as exc:
        if exc.args:
            print(f"** {exc.severity}: {exc}", flush=True)
        return exc.code
