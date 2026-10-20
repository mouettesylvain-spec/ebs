"""`ebs discover`: strace parsing, path classification and the proposed `inputs:` (task P3-02)."""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ebs.sandbox.discover import (
    Access,
    Layout,
    Proposal,
    Report,
    analyze,
    classify,
    collapse,
    parse_strace,
    propose_inputs,
    render_patch,
    strace_argv,
)

FIXTURES = Path(__file__).parents[2] / "fixtures" / "strace"
SCRATCH = "/scratch/repro-1000/lint-x1"
WORK = f"{SCRATCH}/work"
FLOW_DIR = "/proj/cpu"
LAYOUT = Layout(
    work=WORK,
    scratch=SCRATCH,
    flow_dir=FLOW_DIR,
    inputs=frozenset(
        {"rtl/core/alu.sv", "rtl/core/regs.sv", "rtl/core/unused.sv", "flows/lint/Makefile"}
        | {"cfg/a.tcl"}
    ),
    trees=("model/elab[]",),
    staged=("/cas/test",),
    toolchain=("/eda/lintool-1.0",),
)
IN_FLOW_DIR = {f"{FLOW_DIR}/common/rules.mk"}  # what exists in the flow directory (not staged)


def _fixture() -> list[Access]:
    return parse_strace((FIXTURES / "make_lint.log").read_text(), cwd=WORK)


# R1
def test_strace_argv() -> None:
    argv = strace_argv("/usr/bin/strace", "/s/logs/strace.log", ["make", "-C", "flows/lint"])
    assert argv[:1] == ["/usr/bin/strace"]
    assert argv[argv.index("-e") + 1] == "trace=file,process,fchdir"
    assert argv[argv.index("-o") + 1] == "/s/logs/strace.log"
    assert "-f" in argv
    assert "-y" in argv
    assert argv[argv.index("--") :] == ["--", "make", "-C", "flows/lint"]


# R1
@pytest.mark.parametrize(
    ("path", "category"),
    [
        (f"{WORK}/rtl/core/alu.sv", "input"),
        (f"{WORK}/model/elab[]/opt.db", "input"),  # inside a tree input
        ("/cas/test/blobs/sha256/ab/cd/abcd", "input"),  # a large input linked from the CAS
        (f"{WORK}/flows/lint/reports/lint.txt", "scratch"),  # written by the tool
        (f"{SCRATCH}/home/.lintoolrc", "scratch"),
        ("/eda/lintool-1.0/lib/rules.db", "toolchain"),
        ("/eda/lintool-1.0x/lib/rules.db", "undeclared"),  # a prefix is not a parent directory
        ("/usr/lib/x86_64-linux-gnu/libc.so.6", "system"),
        ("/etc/ld.so.cache", "system"),
        ("/proc/self/status", "system"),
        ("/etc/shadow-config", "undeclared"),  # bwrap shows only some /etc files
        ("/home/alice/.lintrc", "undeclared"),
        ("/proj/cpu/common/defs.mk", "undeclared"),
        (f"{WORK}/rtl/core", "scratch"),  # a parent of inputs is not itself an input
    ],
)
def test_classification(path: str, category: str) -> None:
    assert classify(path, LAYOUT) == category


# R1: every read of the fixture trace, classified; failed, write-only and directory accesses are
# not reads, except a failed read in the work dir of a file the flow directory has (not staged).
def test_classification_of_trace() -> None:
    report = analyze(_fixture(), LAYOUT, exists=IN_FLOW_DIR.__contains__)
    got = {(r.path, r.category, r.missing) for r in report.reads}
    assert got == {
        ("/usr/bin/make", "system", False),
        ("/etc/ld.so.cache", "system", False),
        (f"{WORK}/flows/lint/Makefile", "input", False),
        (f"{WORK}/rtl/core/alu.sv", "input", False),
        (f"{WORK}/rtl/core/regs.sv", "input", False),
        ("/home/alice/.lintrc", "undeclared", False),
        ("/proj/cpu/common/defs.mk", "undeclared", False),
        ("/proj/cpu/common/rules.mk", "undeclared", True),
        ("/eda/lintool-1.0/bin/lintool", "toolchain", False),
        ("/eda/lintool-1.0/lib/rules.db", "toolchain", False),
        (f"{WORK}/flows/lint/reports/lint.txt", "scratch", False),
        ("/cas/test/blobs/sha256/ab/cd/abcd0123", "input", False),
        ("/opt/vendor/patch.tcl", "undeclared", False),
        ("/proc/self/status", "system", False),
        (f"{SCRATCH}/home/.lintoolrc", "scratch", False),
    }
    undeclared = {r.path: (r.pid, r.process) for r in report.undeclared()}
    assert undeclared == {
        "/home/alice/.lintrc": (4001, "/usr/bin/make"),  # make read it first, then lintool
        "/proj/cpu/common/defs.mk": (4001, "/usr/bin/make"),
        "/proj/cpu/common/rules.mk": (4001, "/usr/bin/make"),
        "/opt/vendor/patch.tcl": (4002, "/eda/lintool-1.0/bin/lintool"),
    }
    assert [r.path for r in report.undeclared()] == sorted(undeclared)
    assert report.counts() == {
        "input": 4, "toolchain": 2, "system": 3, "scratch": 2, "undeclared": 4
    }  # fmt: skip


# R1 (control): without the flow directory's file, the failed read is just a probe.
def test_failed_read_not_in_flow_dir_is_dropped() -> None:
    report = analyze(_fixture(), LAYOUT, exists=lambda _p: False)
    assert "/proj/cpu/common/rules.mk" not in {r.path for r in report.reads}


# R1: $HOME/$TMPDIR and a Makefile's flow path reach the tool unresolved: the aliases map them.
def test_symlinked_roots() -> None:
    layout = Layout(
        work="/real/s/work", scratch="/real/s", flow_dir="/real/proj",
        aliases=(("/link/s", "/real/s"), ("/proj", "/real/proj")),
    )  # fmt: skip
    accesses = [
        Access(1, "openat", p, ok=True, directory=False, write=False, process=None)
        for p in ("/link/s/home/.rc", "/proj/common/defs.mk", "/link/sx/other")
    ]
    reads = {(r.path, r.category) for r in analyze(accesses, layout).reads}
    assert reads == {
        ("/real/s/home/.rc", "scratch"),
        ("/real/proj/common/defs.mk", "undeclared"),
        ("/link/sx/other", "undeclared"),  # a prefix is not an alias
    }
    proposal = propose_inputs({}, {}, analyze(accesses, layout), layout)
    assert proposal.inputs == {"discovered": "common/defs.mk"}


# R1: make stats its targets; a stale output in the checkout is not a missing input.
def test_failed_read_of_an_output_is_not_missing() -> None:
    layout = Layout(work=WORK, scratch=SCRATCH, flow_dir=FLOW_DIR, outputs=("flows/lint/reports",))
    accesses = [
        Access(1, "stat", f"{WORK}/{p}", ok=False, directory=False, write=False, process=None)
        for p in ("flows/lint/reports/lint.txt", "flows/lint/old.txt")
    ]
    report = analyze(accesses, layout, exists=lambda _p: True)
    assert [(r.path, r.missing) for r in report.reads] == [(f"{FLOW_DIR}/flows/lint/old.txt", True)]


RAW_INPUTS: dict[str, object] = {
    "rtl": "rtl/**",
    "model": "${steps.elab.outputs.model}",
    "cfg": {"from": ["cfg/*.tcl"], "writable": True},
}
SOURCES = {
    "rtl/core/alu.sv": "rtl",
    "rtl/core/regs.sv": "rtl",
    "rtl/core/unused.sv": "rtl",
    "cfg/a.tcl": "cfg",
    "flows/lint/Makefile": "ebs.workdir",  # implicit (make workdir): not in the flow's inputs
}


# R2
def test_patch_proposal_golden() -> None:
    report = analyze(_fixture(), LAYOUT, exists=IN_FLOW_DIR.__contains__)
    proposal = propose_inputs(RAW_INPUTS, SOURCES, report, LAYOUT)
    assert proposal == Proposal(
        inputs={
            "rtl": "rtl/core/*.sv",
            "model": "${steps.elab.outputs.model}",
            "discovered": "common/*.mk",
        },
        unused=("cfg",),
        added="discovered",
        outside=("/home/alice/.lintrc", "/opt/vendor/patch.tcl"),
    )
    assert render_patch("lint", "lint", proposal) == (
        "# proposed by `ebs discover lint` from the files action lint read; review it, then\n"
        "# apply it by hand or rerun with --write\n"
        "# no file of these inputs was read (dropped): cfg\n"
        "# read outside the flow directory (import them, or add a toolchain root):\n"
        "#   /home/alice/.lintrc\n"
        "#   /opt/vendor/patch.tcl\n"
        "steps:\n"
        "  lint:\n"
        "    inputs:\n"
        "      rtl: rtl/core/*.sv\n"
        "      model: ${steps.elab.outputs.model}\n"
        "      discovered: common/*.mk\n"
    )


# R2: a trace cut short (tool failed) never drops or narrows a declared input.
def test_partial_trace_keeps_inputs() -> None:
    report = analyze(_fixture(), LAYOUT, exists=IN_FLOW_DIR.__contains__)
    proposal = propose_inputs(RAW_INPUTS, SOURCES, report, LAYOUT, partial=True)
    assert proposal.inputs == {**RAW_INPUTS, "discovered": "common/*.mk"}
    assert proposal.unused == ()


# R2: an input with a reference anywhere in it (list, long form) is kept, never dropped.
def test_templated_inputs_kept() -> None:
    raw: dict[str, object] = {
        "mix": ["rtl/*.sv", "${steps.a.outputs.b}"],
        "long": {"from": "${steps.a.outputs.c}", "writable": True},
    }
    proposal = propose_inputs(raw, {}, Report(()), LAYOUT)
    assert proposal.inputs == raw
    assert proposal.unused == ()


def test_collapse_hidden_and_escaped_dirs() -> None:
    assert collapse(["a/.rc", "a/x.sv", "a/y.sv"]) == ["a/*.sv", "a/.rc"]
    assert collapse(["m[1]/x.sv", "m[1]/y.sv"]) == ["m[[]1]/*.sv"]
    assert collapse(["top.sv"]) == ["top.sv"]


# R2: a sibling of the flow dir sharing its prefix is outside it; writes are not reads.
def test_sibling_dir_is_outside_and_writes_ignored() -> None:
    accesses = [
        Access(
            1, "openat", "/proj/cpu2/defs.mk", ok=True, directory=False, write=False, process=None
        ),
        Access(1, "openat", "/proj/cpu/gen.mk", ok=True, directory=False, write=True, process=None),
    ]
    report = analyze(accesses, LAYOUT, exists=lambda _p: True)
    assert [r.path for r in report.reads] == ["/proj/cpu2/defs.mk"]
    proposal = propose_inputs({}, {}, report, LAYOUT)
    assert proposal.outside == ("/proj/cpu2/defs.mk",)
    assert proposal.added is None


# R2: globs collapse per directory; one file stays a path; mixed extensions give `*`; the long
# form keeps `writable`; a name already taken gets a suffix; glob characters are escaped.
def test_patch_collapse_rules() -> None:
    accesses = [
        Access(1, "openat", f"{WORK}/{p}", ok=True, directory=False, write=False, process=None)
        for p in ("a/x.sv", "a/y.sv", "b/only.v", "c/m.sv", "c/n.svh", "cfg/a.tcl")
    ] + [
        Access(1, "openat", f"{FLOW_DIR}/{p}", ok=True, directory=False, write=False, process=None)
        for p in ("inc/defs[1].vh",)
    ]
    layout = Layout(
        work=WORK, scratch=SCRATCH, flow_dir=FLOW_DIR,
        inputs=frozenset({"a/x.sv", "a/y.sv", "b/only.v", "c/m.sv", "c/n.svh", "cfg/a.tcl"}),
    )  # fmt: skip
    sources = {"a/x.sv": "src", "a/y.sv": "src", "b/only.v": "src", "c/m.sv": "src",
               "c/n.svh": "src", "cfg/a.tcl": "cfg"}  # fmt: skip
    raw: dict[str, object] = {
        "src": ["a/*", "b/*", "c/*"],
        "cfg": {"from": "cfg/*", "writable": True},
        "discovered": "old/*",
    }
    proposal = propose_inputs(raw, sources, analyze(accesses, layout), layout)
    assert proposal.inputs == {
        "src": ["a/*.sv", "b/only.v", "c/*"],
        "cfg": {"from": "cfg/a.tcl", "writable": True},
        "discovered_2": "inc/defs[[]1].vh",
    }
    assert proposal.unused == ("discovered",)


def _edge(text: str, cwd: str = "/w") -> list[tuple[int, str, bool]]:
    return [(a.pid, a.path, a.ok) for a in parse_strace(text, cwd=cwd)]


EDGE_CASES = {
    "pid-bracket-prefix": (
        '[pid  77] openat(AT_FDCWD, "a.v", O_RDONLY) = 3\n',
        [(77, "/w/a.v", True)],
    ),
    "no-pid-prefix": ('openat(AT_FDCWD, "/x/a.v", O_RDONLY) = 3\n', [(0, "/x/a.v", True)]),
    "timestamps": (
        '12 10:01:02.123456 openat(AT_FDCWD, "/x/a.v", O_RDONLY) = 3\n'
        '13 1729418462.123456 stat("/x/b.v", {st_mode=S_IFREG|0644, ...}) = 0\n',
        [(12, "/x/a.v", True), (13, "/x/b.v", True)],
    ),
    "unfinished-resumed-interleaved": (
        '5 openat(AT_FDCWD, "/x/a.v", O_RDONLY <unfinished ...>\n'
        '6 openat(AT_FDCWD, "/x/b.v", O_RDONLY) = 3\n'
        "5 <... openat resumed>) = 4\n",
        [(6, "/x/b.v", True), (5, "/x/a.v", True)],
    ),
    "resumed-mid-arguments": (
        '5 newfstatat(AT_FDCWD, "/x/a.v",  <unfinished ...>\n'
        "5 <... newfstatat resumed>{st_mode=S_IFREG|0644, st_size=3, ...}, 0) = 0\n",
        [(5, "/x/a.v", True)],
    ),
    "failed-call": (
        '5 openat(AT_FDCWD, "/x/a.v", O_RDONLY) = -1 ENOENT (No such file or directory)\n',
        [(5, "/x/a.v", False)],
    ),
    "at-fdcwd-annotated": (
        '5 openat(AT_FDCWD</d/e>, "../f.v", O_RDONLY) = 3</d/f.v>\n',
        [(5, "/d/f.v", True)],
    ),
    "relative-dirfd-annotated": (
        '5 openat(7</lib/dir>, "x.so", O_RDONLY|O_CLOEXEC) = 3</lib/dir/x.so>\n',
        [(5, "/lib/dir/x.so", True)],
    ),
    "relative-dirfd-from-fd-table": (
        '5 openat(AT_FDCWD, "/inc", O_RDONLY|O_DIRECTORY) = 9\n'
        '5 openat(9, "defs.vh", O_RDONLY) = 3\n',
        [(5, "/inc", True), (5, "/inc/defs.vh", True)],
    ),
    "unknown-dirfd-skipped": ('5 openat(42, "defs.vh", O_RDONLY) = 3\n', []),
    "chdir-then-relative": (
        '5 chdir("sub") = 0\n5 stat("a.v", {st_mode=S_IFREG|0644, ...}) = 0\n',
        [(5, "/w/sub", True), (5, "/w/sub/a.v", True)],
    ),
    "child-inherits-cwd": (
        '5 chdir("/d") = 0\n'
        "5 clone(child_stack=NULL, flags=CLONE_CHILD_SETTID|SIGCHLD <unfinished ...>\n"
        '6 openat(AT_FDCWD, "a.v", O_RDONLY) = 3\n'
        "5 <... clone resumed>, child_tidptr=0x7f) = 6\n",
        [(5, "/d", True), (6, "/d/a.v", True)],
    ),
    "escaped-strings": (
        '5 openat(AT_FDCWD, "/x/a\\"b \\303\\251.v", O_RDONLY) = 3\n'
        '5 openat(AT_FDCWD, "/x/tab\\tx\\x41", O_RDONLY) = 3\n',
        [(5, '/x/a"b é.v', True), (5, "/x/tab\tx" + "A", True)],
    ),
    "truncated-path-skipped": ('5 openat(AT_FDCWD, "/x/very/lo"..., O_RDONLY) = 3\n', []),
    "noise-lines": (
        "5 +++ exited with 0 +++\n5 --- SIGCHLD {si_signo=SIGCHLD} ---\n"
        "strace: Process 9 attached\n5 exit_group(0) = ?\n\n",
        [],
    ),
    "normalized": ('5 openat(AT_FDCWD, "//x/./y/../a.v", O_RDONLY) = 3\n', [(5, "/x/a.v", True)]),
    "execve-and-access": (
        '5 execve("./run.sh", ["./run.sh"], 0x7ffd /* 3 vars */) = 0\n'
        '5 access("/x/cfg", R_OK) = 0\n'
        '5 readlink("/x/link", "target", 4095) = 6\n',
        [(5, "/w/run.sh", True), (5, "/x/cfg", True), (5, "/x/link", True)],
    ),
    "statx-and-openat2": (
        '5 statx(AT_FDCWD, "/x/a.v", AT_STATX_SYNC_AS_STAT, STATX_ALL, {stx_mask=STATX_ALL, '
        "stx_mode=S_IFREG|0644, ...}) = 0\n"
        '5 openat2(AT_FDCWD, "/x/b.v", {flags=O_RDONLY, resolve=0}, 24) = 3\n',
        [(5, "/x/a.v", True), (5, "/x/b.v", True)],
    ),
    "fchdir": (
        '5 openat(AT_FDCWD, "/d", O_RDONLY|O_DIRECTORY) = 3\n5 fchdir(3) = 0\n'
        '5 stat("a.v", {st_mode=S_IFREG|0644, ...}) = 0\n',
        [(5, "/d", True), (5, "/d/a.v", True)],
    ),
    "fchdir-annotated": (
        '5 fchdir(3</d/e>) = 0\n5 access("a.v", R_OK) = 0\n',
        [(5, "/d/e/a.v", True)],
    ),
    "annotation-escaped": (
        '5 openat(AT_FDCWD</r/caf\\303\\251>, "a.v", O_RDONLY) = 3\n',
        [(5, "/r/café/a.v", True)],
    ),
    "raw-bytes-in-log": (
        '5 openat(AT_FDCWD, "/x/\udcff.v", O_RDONLY) = 3\n',
        [(5, "/x/\udcff.v", True)],
    ),
    "annotation-with-unbalanced-parens": (
        '5 openat(AT_FDCWD</w/a(b>, "x.v", O_RDONLY) = 3</w/a(b/x.v>\n'
        '5 openat(4</w/m[1>, "y.v", O_RDONLY) = 3\n',
        [(5, "/w/a(b/x.v", True), (5, "/w/m[1/y.v", True)],
    ),
    "fork-inherits-fds-and-program": (
        '5 execve("/usr/bin/make", ["make"], 0x1 /* 1 var */) = 0\n'
        '5 openat(AT_FDCWD, "/inc", O_RDONLY|O_DIRECTORY) = 9\n'
        "5 vfork() = 6\n"
        '6 openat(9, "a.vh", O_RDONLY) = 3\n',
        [(5, "/usr/bin/make", True), (5, "/inc", True), (6, "/inc/a.vh", True)],
    ),
    "failed-fchdir-keeps-cwd": (
        '5 fchdir(3</d>) = -1 EBADF (Bad file descriptor)\n5 stat("a.v", {st_mode=S_IFREG}) = 0\n',
        [(5, "/w/a.v", True)],
    ),
    "unknown-result-is-not-ok": (
        '5 openat(AT_FDCWD, "/x/a.v", O_RDONLY) = ?\n',
        [(5, "/x/a.v", False)],
    ),
    "empty-path-fstat-skipped": (
        '5 newfstatat(3, "", {st_mode=S_IFREG|0644, ...}, AT_EMPTY_PATH) = 0\n',
        [],
    ),
}


# R3
@pytest.mark.parametrize("case", list(EDGE_CASES))
def test_parser_edge_cases(case: str) -> None:
    text, expected = EDGE_CASES[case]
    assert _edge(text) == expected


# R3: flags that make an access not a read, and directories.
def test_parser_flags() -> None:
    text = (
        '5 openat(AT_FDCWD, "/x/out", O_WRONLY|O_CREAT|O_TRUNC, 0666) = 3\n'
        '5 openat(AT_FDCWD, "/x/rw", O_RDWR) = 3\n'
        '5 creat("/x/new", 0644) = 3\n'
        '5 openat(AT_FDCWD, "/x/d", O_RDONLY|O_DIRECTORY) = 3\n'
        '5 newfstatat(AT_FDCWD, "/x/e", {st_mode=S_IFDIR|0755, ...}, 0) = 0\n'
        '5 openat2(AT_FDCWD, "/x/o2", {flags=O_WRONLY|O_CREAT, mode=0600}, 24) = 3\n'
        '5 openat(AT_FDCWD, "/x/handle", O_RDONLY|O_PATH) = 3\n'
        '5 open("/x/out2", O_WRONLY) = 3\n'
        '5 open("/x/d2", O_RDONLY|O_DIRECTORY) = 3\n'
        '5 open("/x/h2", O_PATH) = 3\n'
    )
    got = {a.path: (a.write, a.directory) for a in parse_strace(text, cwd="/w")}
    assert got == {
        "/x/out": (True, False),
        "/x/rw": (False, False),
        "/x/new": (True, False),
        "/x/d": (False, True),
        "/x/e": (False, True),
        "/x/o2": (True, False),
        "/x/handle": (False, True),  # O_PATH: a handle, not a read
        "/x/out2": (True, False),  # open() has its flags second
        "/x/d2": (False, True),
        "/x/h2": (False, True),
    }


# R3: the program each pid runs is tracked across exec and fork.
def test_parser_tracks_process() -> None:
    procs = {a.path: (a.pid, a.process) for a in _fixture()}
    assert procs["/etc/ld.so.cache"] == (4001, "/usr/bin/make")
    assert procs["/opt/vendor/patch.tcl"] == (4002, "/eda/lintool-1.0/bin/lintool")


PATH_SYSCALLS = {
    "open": '5 open("/x/a.v", O_RDONLY) = 3',
    "stat": '5 stat("/x/a.v", {st_mode=S_IFREG|0644, ...}) = 0',
    "lstat": '5 lstat("/x/a.v", {st_mode=S_IFREG|0644, ...}) = 0',
    "stat64": '5 stat64("/x/a.v", {st_mode=S_IFREG|0644, ...}) = 0',
    "lstat64": '5 lstat64("/x/a.v", {st_mode=S_IFREG|0644, ...}) = 0',
    "oldstat": '5 oldstat("/x/a.v", 0x7ffd) = 0',
    "oldlstat": '5 oldlstat("/x/a.v", 0x7ffd) = 0',
    "access": '5 access("/x/a.v", R_OK) = 0',
    "readlink": '5 readlink("/x/a.v", "b", 4095) = 1',
    "execve": '5 execve("/x/a.v", ["a.v"], 0x7ffd /* 1 var */) = 0',
    "openat": '5 openat(AT_FDCWD, "/x/a.v", O_RDONLY) = 3',
    "openat2": '5 openat2(AT_FDCWD, "/x/a.v", {flags=O_RDONLY}, 24) = 3',
    "newfstatat": '5 newfstatat(AT_FDCWD, "/x/a.v", {st_mode=S_IFREG|0644, ...}, 0) = 0',
    "fstatat64": '5 fstatat64(AT_FDCWD, "/x/a.v", {st_mode=S_IFREG|0644, ...}, 0) = 0',
    "statx": '5 statx(AT_FDCWD, "/x/a.v", 0, STATX_ALL, {stx_mode=S_IFREG|0644, ...}) = 0',
    "faccessat": '5 faccessat(AT_FDCWD, "/x/a.v", R_OK) = 0',
    "faccessat2": '5 faccessat2(AT_FDCWD, "/x/a.v", R_OK, 0) = 0',
    "readlinkat": '5 readlinkat(AT_FDCWD, "/x/a.v", "b", 4095) = 1',
    "execveat": '5 execveat(AT_FDCWD, "/x/a.v", ["a.v"], 0x7ffd /* 1 var */, 0) = 0',
}


# R3: every path syscall the parser knows yields the read.
@pytest.mark.parametrize("name", sorted(PATH_SYSCALLS))
def test_parser_every_path_syscall(name: str) -> None:
    assert _edge(PATH_SYSCALLS[name] + "\n") == [(5, "/x/a.v", True)]


def _c_escape(text: str) -> str:
    out = []
    for byte in text.encode():
        ch = chr(byte)
        if ch in '"\\':
            out.append("\\" + ch)
        elif 0x20 <= byte < 0x7F:
            out.append(ch)
        else:
            out.append(f"\\{byte:03o}")
    return "".join(out)


# R3 (property): any path, escaped as strace prints it, parses back.
@given(
    st.lists(
        st.text(st.characters(codec="utf-8", blacklist_characters="/\x00"), min_size=1),
        min_size=1,
    )
)
def test_parser_roundtrip(parts: list[str]) -> None:
    parts = [p for p in parts if p not in (".", "..")] or ["x"]
    path = "/" + "/".join(parts)
    line = f'5 openat(AT_FDCWD, "{_c_escape(path)}", O_RDONLY) = 3\n'
    assert [a.path for a in parse_strace(line, cwd="/w")] == [path]
