"""`ebs discover`: which files a step really reads, from an strace log (task P3-02).

The CLI stages one action like the runner (strict staging, no namespace sandbox), runs it under
`strace_argv` and hands the log to `parse_strace`. Every successful open, exec and stat-like call
becomes an `Access` with an absolute, normalized path (`-f` pid prefixes, `<unfinished ...>` /
`<... resumed>` pairs, `AT_FDCWD` and relative dirfds are resolved; `-y` annotations are used
when present, else each pid's cwd and fd table are tracked across chdir and fork).

`analyze` classifies the reads against the action's `Layout`:

- `input`: a declared input staged in the work dir (or the CAS objects large inputs link to);
- `scratch`: anything else in the action's scratch (outputs being re-read, the fake HOME, TMPDIR);
- `toolchain`: the toolchain's install roots and `[sandbox].ro_binds`;
- `system`: what the bubblewrap sandbox shows of the OS (`/usr`, `/lib*`, part of `/etc`,
  `/proc`, `/dev`), plus `/sys`, which it does not mount but tools tolerate missing;
- `undeclared`: everything else. Also a *failed* read inside the work dir of a file the flow
  directory has: the step needs a file that was not staged (`Read.missing`).

`propose_inputs` turns the reads into a new `inputs:` mapping for the step (literal source globs
tightened to the files read, collapsed by directory; templated inputs kept as written; files read
from the flow directory but not declared added as `discovered`), and `render_patch` prints it as
YAML. Nothing here writes files or runs processes.
"""

from __future__ import annotations

import os
import posixpath
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from io import StringIO
from typing import Final, Literal

from ruamel.yaml import YAML

from ebs.sandbox.bwrap import ETC_FILES, SYSTEM_DIRS

__all__ = [
    "SYSTEM_PATHS",
    "Access",
    "Category",
    "Layout",
    "Proposal",
    "Read",
    "Report",
    "analyze",
    "classify",
    "parse_strace",
    "propose_inputs",
    "render_patch",
    "strace_argv",
    "yaml_dumper",
]

Category = Literal["input", "toolchain", "system", "scratch", "undeclared"]
CATEGORIES: Final[tuple[Category, ...]] = ("input", "toolchain", "system", "scratch", "undeclared")

SYSTEM_PATHS: Final = ("/usr", *SYSTEM_DIRS, *ETC_FILES, "/proc", "/dev", "/sys")
DISCOVERED: Final = "discovered"
"""The input name proposed for files read from the flow directory but not declared."""


def strace_argv(strace: str, log: str, argv: Sequence[str]) -> list[str]:
    """`argv` under strace: follow forks, file and process syscalls (and fchdir, which moves
    the cwd) only, fds annotated with
    their paths (`-y`), strings never truncated, the trace written to `log`."""
    return [
        strace, "-f", "-y", "-qq", "-s", "65535", "-e", "trace=file,process,fchdir", "-o", log,
        "--", *argv,
    ]  # fmt: skip


@dataclass(frozen=True, slots=True)
class Access:
    """One path syscall of the trace. `ok`: it succeeded; `write`: opened for writing only;
    `directory`: the path is a directory (O_DIRECTORY, S_IFDIR or chdir); `process`: the program
    the pid was running (its last successful execve, inherited across fork), None if unknown."""

    pid: int
    syscall: str
    path: str
    ok: bool
    directory: bool
    write: bool
    process: str | None


# --- parsing --------------------------------------------------------------------------------

_PREFIX: Final = re.compile(
    r"^\s*(?:\[pid\s+(?P<bpid>\d+)\]|(?P<pid>\d+))?\s*(?:\d+(?::\d+)*(?:\.\d+)?\s+)?(?P<rest>.*)$"
)
_CALL: Final = re.compile(r"^(?P<name>[a-z_][a-z0-9_]*)\(")
_RESUMED: Final = re.compile(r"^<\.\.\. (?P<name>[a-z_][a-z0-9_]*) resumed>(?P<rest>.*)$")
_UNFINISHED: Final = " <unfinished ...>"
_RESULT: Final = re.compile(r"^\s*=\s*(?P<ret>-?\d+|\?|0x[0-9a-f]+)")
_FD: Final = re.compile(r"^(?P<fd>AT_FDCWD|-?\d+)(?:<(?P<path>.*)>)?$", re.DOTALL)

_PATH0: Final = frozenset(
    {"open", "creat", "stat", "lstat", "stat64", "lstat64", "oldstat", "oldlstat", "access",
     "readlink", "execve", "chdir"}
)  # fmt: skip
_DIRFD_PATH: Final = frozenset(
    {"openat", "openat2", "newfstatat", "fstatat64", "statx", "faccessat", "faccessat2",
     "readlinkat", "execveat"}
)  # fmt: skip
_OPEN_FLAGS: Final = {"open": 1, "openat": 2, "openat2": 2}
_FORKS: Final = frozenset({"clone", "clone3", "fork", "vfork"})


def _split_call(text: str) -> tuple[list[str], str] | None:
    """The top-level arguments of `name(args) = ...` (text starts after `(`) and what follows
    the closing parenthesis; None if the call is not closed."""
    args: list[str] = []
    start = depth = 0
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 1
        elif c == "<":  # a -y path (`3</a(b>`): opaque, strace escapes `<` and `>` inside it
            end = text.find(">", i + 1)
            i = n if end < 0 else end
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0 and c == ")":
                last = text[start:i].strip()
                if last or args:
                    args.append(last)
                return args, text[i + 1 :]
            depth = max(depth - 1, 0)
        elif c == "," and depth == 0:
            args.append(text[start:i].strip())
            start = i + 1
        i += 1
    return None


_ESCAPES: Final = {"n": 10, "t": 9, "r": 13, "v": 11, "f": 12, "a": 7, "b": 8, "e": 27}


def _c_string(arg: str) -> str | None:
    """Decode an strace string literal; None if it is not one or was truncated (`"..."...`)."""
    if len(arg) < 2 or arg[0] != '"' or arg[-1] != '"':
        return None
    return _unescape(arg[1:-1])


def _unescape(body: str) -> str:
    """strace's C escapes (`\\"`, `\\n`, `\\303`, `\\x41`) decoded to a file name."""
    out, i = bytearray(), 0
    while i < len(body):
        c = body[i]
        if c != "\\":
            out += c.encode("utf-8", "surrogateescape")
            i += 1
            continue
        nxt = body[i + 1 : i + 2]
        if nxt == "x":
            out.append(int(body[i + 2 : i + 4], 16))
            i += 4
        elif nxt.isdigit():
            j = i + 1
            while j < len(body) and j < i + 4 and body[j] in "01234567":
                j += 1
            out.append(int(body[i + 1 : j], 8))
            i = j
        else:
            out += bytes([_ESCAPES[nxt]]) if nxt in _ESCAPES else (nxt or "\\").encode()
            i += 2
    return os.fsdecode(bytes(out))


def _norm(path: str) -> str:
    norm = posixpath.normpath(path)
    return "/" + norm.lstrip("/") if norm.startswith("//") else norm


@dataclass
class _Proc:
    cwd: str
    exe: str | None = None
    fds: dict[int, str] = field(default_factory=dict)


def _children(lines: Iterable[tuple[int, str, str]]) -> dict[int, int]:
    """child pid -> parent pid, from the return values of fork-like calls."""
    parents: dict[int, int] = {}
    for pid, name, after in lines:
        if name in _FORKS and (m := _RESULT.match(after)) and m["ret"].isdigit():
            parents[int(m["ret"])] = pid
    return parents


def _calls(text: str) -> list[tuple[int, str, str]]:
    """(pid, name, `args) = result` text) for every complete call, unfinished/resumed pairs
    joined, in the order the calls finished."""
    calls: list[tuple[int, str, str]] = []
    pending: dict[int, str] = {}
    for line in text.splitlines():
        m = _PREFIX.match(line)
        if m is None:  # pragma: no cover - the pattern matches any line
            continue
        pid = int(m["bpid"] or m["pid"] or 0)
        rest = m["rest"]
        if (r := _RESUMED.match(rest)) is not None:
            head = pending.pop(pid, None)
            if head is not None:
                rest = head + r["rest"]
            else:
                continue
        if rest.endswith(_UNFINISHED):
            pending[pid] = rest[: -len(_UNFINISHED)]
            continue
        if (c := _CALL.match(rest)) is not None:
            calls.append((pid, c["name"], rest[c.end() :]))
    return calls


def parse_strace(text: str, *, cwd: str) -> list[Access]:
    """Every path access of an `strace -f -e trace=file,process` log, in completion order.

    `cwd` is the directory the traced command started in. Paths that cannot be resolved (a
    relative path against an unknown dirfd, a truncated string) are left out.
    """
    calls = _calls(text)
    parsed = [(pid, name, _split_call(after)) for pid, name, after in calls]
    parents = _children((pid, name, s[1]) for pid, name, s in parsed if s is not None)
    procs: dict[int, _Proc] = {}

    def proc(pid: int) -> _Proc:
        if pid not in procs:
            parent = parents.get(pid)
            if parent is not None and parent != pid:
                p = proc(parent)
                procs[pid] = _Proc(p.cwd, p.exe, dict(p.fds))
            else:
                procs[pid] = _Proc(cwd)
        return procs[pid]

    out: list[Access] = []
    for pid, name, split in parsed:
        if split is None:
            continue
        args, after = split
        p = proc(pid)
        result = _RESULT.match(after)
        ret = result["ret"] if result else "?"
        ok = ret != "?" and not ret.startswith("-")
        if name in _FORKS:
            if ok and ret.isdigit():
                proc(int(ret))
            continue
        if name == "fchdir":
            target = _dirfd(args[0], p) if args else None
            if ok and target is not None:
                p.cwd = target
            continue
        if name in _PATH0:
            base: str | None = p.cwd
            raw = args[0] if args else ""
        elif name in _DIRFD_PATH and len(args) >= 2:
            raw = args[1]
            base = _dirfd(args[0], p)
        else:
            continue
        path_arg = _c_string(raw)
        if not path_arg:  # truncated, not a string, or "" (AT_EMPTY_PATH: an fstat)
            continue
        if path_arg.startswith("/"):
            path = _norm(path_arg)
        elif base is None:
            continue
        else:
            path = _norm(posixpath.join(base, path_arg))
        index = _OPEN_FLAGS.get(name)
        flags = args[index] if index is not None and len(args) > index else ""
        write = name == "creat" or "O_WRONLY" in flags
        buffers = ",".join(args[1 if name in _PATH0 else 2 :])  # the stat buffer, if any
        # O_PATH opens a handle for *at() calls or fchdir: nothing is read, like a directory.
        directory = name == "chdir" or "O_DIRECTORY" in flags or "O_PATH" in flags
        directory = directory or "S_IFDIR" in buffers
        if ok and name == "execve":
            p.exe = path
        if ok and name == "chdir":
            p.cwd = path
        if ok and name in _OPEN_FLAGS and ret.isdigit():
            p.fds[int(ret)] = path
        out.append(Access(pid, name, path, ok, directory, write, p.exe))
    return out


def _dirfd(arg: str, p: _Proc) -> str | None:
    m = _FD.match(arg)
    if m is None:
        return None
    if m["path"] is not None:
        path = _norm(_unescape(m["path"]))  # -y escapes non-ASCII bytes as in strings
        if m["fd"] == "AT_FDCWD":
            p.cwd = path  # -y shows the real cwd: it also corrects what chdir tracking missed
        return path
    if m["fd"] == "AT_FDCWD":
        return p.cwd
    return p.fds.get(int(m["fd"]))


# --- classification -------------------------------------------------------------------------


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


@dataclass(frozen=True)
class Layout:
    """Where one staged action's files are: `work` (inside `scratch`), the flow directory the
    source inputs come from, the logical paths of its file `inputs` (incl. config files) and
    `trees`, `staged` roots large inputs link to (the domain's CAS) and `toolchain` roots.
    `work`, `scratch` and `flow_dir` are real paths (strace resolves the cwd); `aliases` maps
    other spellings of them to those."""

    work: str
    scratch: str
    flow_dir: str
    inputs: frozenset[str] = frozenset()
    trees: tuple[str, ...] = ()
    staged: tuple[str, ...] = ()
    toolchain: tuple[str, ...] = ()
    system: tuple[str, ...] = SYSTEM_PATHS
    outputs: tuple[str, ...] = ()  # logical output paths: a failed read there is no input
    aliases: tuple[tuple[str, str], ...] = ()  # (symlinked spelling, real path) of the roots

    def canonical(self, path: str) -> str:
        """`path` with a symlinked spelling of a root (`$HOME`, `$TMPDIR`, an automounted flow
        dir as written in a Makefile) replaced by the real path the layout uses."""
        for alias, real in self.aliases:
            if _under(path, alias):
                return real + path[len(alias) :]
        return path


def classify(path: str, layout: Layout) -> Category:
    """The category of an absolute, normalized path read by the action."""
    if _under(path, layout.work) and path != layout.work:
        rel = path[len(layout.work) + 1 :]
        if rel in layout.inputs or any(_under(rel, t) for t in layout.trees):
            return "input"
    if any(_under(path, root) for root in layout.staged):
        return "input"
    if _under(path, layout.scratch):
        return "scratch"
    if any(_under(path, root) for root in layout.toolchain):
        return "toolchain"
    if any(_under(path, root) for root in layout.system):
        return "system"
    return "undeclared"


@dataclass(frozen=True, slots=True)
class Read:
    """The first read of one path: by which pid and program, through which syscall. `missing`:
    the read failed in the work dir, but the flow directory has that file (not staged)."""

    path: str
    category: Category
    pid: int
    process: str | None
    syscall: str
    missing: bool = False


@dataclass(frozen=True)
class Report:
    reads: tuple[Read, ...]  # in the order of first read

    def undeclared(self) -> tuple[Read, ...]:
        """The undeclared reads, sorted by path."""
        return tuple(sorted((r for r in self.reads if r.category == "undeclared"), key=_path))

    def counts(self) -> dict[Category, int]:
        return {c: sum(r.category == c for r in self.reads) for c in CATEGORIES}


def _path(read: Read) -> str:
    return read.path


def analyze(
    accesses: Iterable[Access],
    layout: Layout,
    *,
    exists: Callable[[str], bool] = os.path.isfile,
) -> Report:
    """Classify the file reads of a trace (failed, write-only and directory accesses are not
    reads); `exists` says whether the flow directory has a file (I/O, injected for tests)."""
    seen: dict[str, Read] = {}
    for a in accesses:
        if a.write or a.directory:
            continue
        canonical = layout.canonical(a.path)
        if a.ok:
            path, category, missing = canonical, classify(canonical, layout), False
        elif _under(canonical, layout.work) and canonical != layout.work:
            rel = canonical[len(layout.work) + 1 :]
            path = posixpath.join(layout.flow_dir, rel)
            if any(_under(rel, out) for out in layout.outputs) or not exists(path):
                # make stats its targets: a stale output in the checkout is not an input
                continue  # or a probe (make's implicit rules, PATH search): nothing is missing
            category, missing = "undeclared", True
        else:
            continue
        if path not in seen:
            seen[path] = Read(path, category, a.pid, a.process, a.syscall, missing)
    return Report(tuple(seen.values()))


# --- the proposed inputs --------------------------------------------------------------------


@dataclass(frozen=True)
class Proposal:
    """The step's new `inputs:` (plain YAML data, in the flow's order, `added` last), the
    literal inputs none of whose files were read (dropped), and the undeclared files read
    outside the flow directory (they cannot be a source input)."""

    inputs: dict[str, object]
    unused: tuple[str, ...] = ()
    added: str | None = None
    outside: tuple[str, ...] = ()


def _texts(value: object) -> list[str]:
    if isinstance(value, Mapping):
        value = value.get("from", ())
    if isinstance(value, str):
        return [value]
    if isinstance(value, Sequence):
        return [str(v) for v in value]
    return []


def _escape(name: str) -> str:
    return re.sub(r"([*?\[])", r"[\1]", name)


def collapse(paths: Iterable[str]) -> list[str]:
    """Relative file paths as globs, one per directory with several files: `dir/*.ext` when they
    share an extension, else `dir/*`; a lone file (or a hidden one, which `*` never matches)
    stays its own path. Sorted."""
    by_dir: dict[str, list[str]] = {}
    for p in sorted(set(paths)):
        by_dir.setdefault(posixpath.dirname(p), []).append(posixpath.basename(p))
    out: list[str] = []
    for d, names in by_dir.items():
        prefix = _escape(d) + "/" if d else ""
        visible = [n for n in names if not n.startswith(".")]
        out += [prefix + _escape(n) for n in names if n.startswith(".")]
        if len(visible) == 1:
            out.append(prefix + _escape(visible[0]))
        elif visible:
            exts = {posixpath.splitext(n)[1] for n in visible}
            ext = exts.pop() if len(exts) == 1 else ""
            out.append(f"{prefix}*{_escape(ext)}")
    return sorted(out)


def _value(original: object, patterns: list[str]) -> object:
    new: object = patterns[0] if len(patterns) == 1 else patterns
    if isinstance(original, Mapping):
        return {**original, "from": new}
    return new


def propose_inputs(
    raw: Mapping[str, object],
    sources: Mapping[str, str],
    report: Report,
    layout: Layout,
    *,
    partial: bool = False,
) -> Proposal:
    """The step's inputs tightened to what was read.

    `raw`: the step's `inputs:` as written (pattern, list or `{from, writable}`); `sources`:
    logical path -> input name of the action's source files (implicit ones, such as a make
    workdir, are not in `raw` and are left alone). An input with a `${...}` reference is kept as
    written; a literal one becomes the globs of the files read through it, or is dropped when
    none was; undeclared files read from the flow directory are added as `discovered`.
    `partial` (the tool failed or timed out, so the trace stops early): every declared input is
    kept as written, only `discovered` is added.
    """
    read: dict[str, set[str]] = {}
    added: set[str] = set()
    outside: list[str] = []
    for r in report.reads:
        if r.category == "input" and _under(r.path, layout.work):
            rel = r.path[len(layout.work) + 1 :]
            if rel in sources:
                read.setdefault(sources[rel], set()).add(rel)
        elif r.category == "undeclared":
            if _under(r.path, layout.flow_dir) and r.path != layout.flow_dir:
                added.add(r.path[len(layout.flow_dir) + 1 :])
            else:
                outside.append(r.path)
    inputs: dict[str, object] = {}
    unused: list[str] = []
    for name, value in raw.items():
        if partial or any("${" in t for t in _texts(value)):
            inputs[name] = value
        elif name in read:
            inputs[name] = _value(value, collapse(read[name]))
        else:
            unused.append(name)
    new_name = None
    if added:
        new_name, n = DISCOVERED, 2
        while new_name in raw:
            new_name, n = f"{DISCOVERED}_{n}", n + 1
        inputs[new_name] = _value(None, collapse(added))
    return Proposal(inputs, tuple(unused), new_name, tuple(sorted(outside)))


def yaml_dumper() -> YAML:
    yaml = YAML(typ="rt", pure=True)
    yaml.width = 1 << 16  # never re-wrap long lines
    yaml.indent(mapping=2, sequence=2, offset=0)
    return yaml


def render_patch(step: str, action_id: str, proposal: Proposal) -> str:
    """The proposal as a YAML fragment of the flow, with what it drops and cannot express in
    comments."""
    head = [
        f"# proposed by `ebs discover {step}` from the files action {action_id} read; review "
        "it, then",
        "# apply it by hand or rerun with --write",
    ]
    if proposal.unused:
        head.append(f"# no file of these inputs was read (dropped): {', '.join(proposal.unused)}")
    if proposal.outside:
        head.append("# read outside the flow directory (import them, or add a toolchain root):")
        head += [f"#   {p}" for p in proposal.outside]
    buf = StringIO()
    yaml_dumper().dump({"steps": {step: {"inputs": proposal.inputs}}}, buf)
    return "\n".join(head) + "\n" + buf.getvalue()
