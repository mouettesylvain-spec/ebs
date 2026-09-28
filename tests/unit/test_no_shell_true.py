"""Invariant I17: no subprocess is started through a shell (AST scan of src/)."""

from __future__ import annotations

import ast
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
ALLOW_MARKER = "# ebs: allow-shell"

# subprocess functions that accept shell=...; getoutput/getstatusoutput always use a shell.
_SHELL_KWARG_FUNCS = {"run", "Popen", "call", "check_call", "check_output"}
_ALWAYS_SHELL = {
    ("subprocess", "getoutput"),
    ("subprocess", "getstatusoutput"),
    ("asyncio", "create_subprocess_shell"),
    ("asyncio.subprocess", "create_subprocess_shell"),
    ("os", "system"),
    ("os", "popen"),
}
_SHELLS = {"sh", "bash", "dash", "zsh", "ksh", "csh", "tcsh"}
_WATCHED_MODULES = {"subprocess", "asyncio", "asyncio.subprocess", "os"}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    call: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.call}"


class _ShellCallVisitor(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.findings: list[Finding] = []
        # local name -> module ("sp" -> "subprocess")
        self.module_aliases: dict[str, str] = {}
        # local name -> (module, function) ("run" -> ("subprocess", "run"))
        self.func_aliases: dict[str, tuple[str, str]] = {}

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name.split(".")[0] not in _WATCHED_MODULES:
                continue
            if alias.asname:
                self.module_aliases[alias.asname] = alias.name
            else:  # `import asyncio.subprocess` binds `asyncio`
                root = alias.name.split(".")[0]
                self.module_aliases[root] = root
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module in _WATCHED_MODULES:  # `import *` is caught by the shell= keyword rule
            for alias in node.names:
                self.func_aliases[alias.asname or alias.name] = (node.module, alias.name)
        self.generic_visit(node)

    def _resolve(self, func: ast.expr) -> tuple[str, str] | None:
        """Map a call target like `sp.run` or `asyncio.subprocess.x` to (module, function)."""
        parts: list[str] = []
        node = func
        while isinstance(node, ast.Attribute):
            parts.insert(0, node.attr)
            node = node.value
        if not isinstance(node, ast.Name):
            return None
        root = node.id
        if root in self.module_aliases:
            prefix = self.module_aliases[root]
        elif root in self.func_aliases:  # `from asyncio import subprocess as asp`
            prefix = ".".join(self.func_aliases[root])
        else:
            return None
        if not parts:
            return self.func_aliases.get(root)
        return ".".join([prefix, *parts[:-1]]), parts[-1]

    def visit_Call(self, node: ast.Call) -> None:
        target = self._resolve(node.func)
        label = self._shell_reason(target, node)
        if label is not None:
            self.findings.append(Finding(self.path, node.lineno, label))
        self.generic_visit(node)

    @staticmethod
    def _shell_reason(target: tuple[str, str] | None, node: ast.Call) -> str | None:
        name = ".".join(target) if target else ast.unparse(node.func)
        if target in _ALWAYS_SHELL:
            return name
        # loop.subprocess_shell(...), aliased create_subprocess_shell, ...
        if isinstance(node.func, ast.Attribute) and node.func.attr.endswith("subprocess_shell"):
            return name
        # Any callee: injected runners (`runner=subprocess.run`), aliases and partials all
        # receive shell= as a keyword. Only a literal falsy value is provably safe.
        for kw in node.keywords:
            if kw.arg == "shell" and not (
                isinstance(kw.value, ast.Constant) and not kw.value.value
            ):
                return f"{name}(shell=...)"
        is_popen_like = target is not None and target[0] == "subprocess"
        is_popen_like = is_popen_like and target is not None and target[1] in _SHELL_KWARG_FUNCS
        if is_popen_like and any(kw.arg is None for kw in node.keywords):
            return f"{name}(**kwargs)"  # **kwargs could carry shell=True
        # argv that starts a shell interpreter: ["sh", "-c", ...]
        if node.args and isinstance(node.args[0], ast.List | ast.Tuple):
            elts = node.args[0].elts
            if (
                len(elts) >= 2
                and isinstance(elts[0], ast.Constant)
                and str(elts[0].value).rsplit("/", 1)[-1] in _SHELLS
                and isinstance(elts[1], ast.Constant)
                and elts[1].value == "-c"
            ):
                return f"{name}([shell, '-c', ...])"
        return None


def _allowed_lines(source: str) -> dict[int, str]:
    """Line number -> reason for every `# ebs: allow-shell <reason>` comment."""
    allowed: dict[int, str] = {}
    for lineno, line in enumerate(source.splitlines(), start=1):
        idx = line.find(ALLOW_MARKER)
        if idx >= 0:
            allowed[lineno] = line[idx + len(ALLOW_MARKER) :].strip()
    return allowed


def scan_source(source: str, path: str = "<string>") -> tuple[list[Finding], dict[int, str]]:
    """Return (shell-call findings not covered by an allow comment, allow comments)."""
    visitor = _ShellCallVisitor(path)
    visitor.visit(ast.parse(source, filename=path))
    allowed = _allowed_lines(source)
    remaining = [f for f in visitor.findings if not allowed.get(f.line)]
    return remaining, allowed


def scan_tree(root: Path) -> tuple[list[Finding], list[str]]:
    findings: list[Finding] = []
    allow_comments: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = str(path.relative_to(root))
        found, allowed = scan_source(path.read_text(encoding="utf-8"), rel)
        findings.extend(found)
        allow_comments.extend(f"{rel}:{line}: {reason!r}" for line, reason in allowed.items())
    return findings, allow_comments


# R5
def test_src_has_no_shell_true() -> None:
    findings, allow_comments = scan_tree(SRC)
    assert list(SRC.rglob("*.py")), "scanner found no sources; is SRC right?"
    assert not findings, "shell-based subprocess calls in src/ (invariant I17):\n" + "\n".join(
        map(str, findings)
    )
    # The allowlist mechanism exists, but nothing may use it today.
    assert not allow_comments, f"unexpected {ALLOW_MARKER} comments: {allow_comments}"


POSITIVE_SAMPLES = [
    "import subprocess\nsubprocess.run(cmd, shell=True)",
    "import subprocess\nsubprocess.Popen(cmd, shell=True)",
    "import subprocess\nsubprocess.call(cmd, shell=True)",
    "import subprocess\nsubprocess.check_call(cmd, shell=True)",
    "import subprocess\nsubprocess.check_output(cmd, shell=True)",
    "import subprocess\nsubprocess.run(cmd, shell=flag)",
    "import subprocess\nsubprocess.run(cmd, shell=1)",
    "import subprocess\nsubprocess.run(cmd, **opts)",
    "import subprocess\nsubprocess.getoutput(cmd)",
    "import subprocess\nsubprocess.getstatusoutput(cmd)",
    "import subprocess as sp\nsp.run(cmd, shell=True)",
    "from subprocess import run\nrun(cmd, shell=True)",
    "from subprocess import Popen as P\nP(cmd, shell=True)",
    "import asyncio\nasync def f():\n    await asyncio.create_subprocess_shell(cmd)",
    "from asyncio import create_subprocess_shell\ncreate_subprocess_shell(cmd)",
    "import asyncio\nasyncio.subprocess.create_subprocess_shell(cmd)",
    "import asyncio.subprocess\nasyncio.subprocess.create_subprocess_shell(cmd)",
    "from asyncio import subprocess as asp\nasp.create_subprocess_shell(cmd)",
    "import asyncio.subprocess as asp\nasp.create_subprocess_shell(cmd)",
    "import os\nos.system(cmd)",
    "import os\nos.popen(cmd)",
    (  # multi-line call inside a function
        "def f():\n    import subprocess\n"
        "    subprocess.run(\n        cmd,\n        shell=True,\n    )\n"
    ),
    # injected runners, aliases and partials: the shell= keyword is flagged whatever the callee
    "import subprocess\ndef f(cmd, runner=subprocess.run):\n    runner(cmd, shell=True)",
    "import subprocess\nclass A:\n    def g(self):\n        self._run(c, shell=True)",
    "import subprocess\nrun = subprocess.run\nrun(cmd, shell=True)",
    "import functools, subprocess\nfunctools.partial(subprocess.run, shell=True)(cmd)",
    "from subprocess import *\nrun(cmd, shell=True)",
    "def run(cmd, shell):\n    pass\nrun('x', shell=True)",
    "class S:\n    def run(self, shell): ...\nS().run(shell=True)",
    "async def f(loop):\n    await loop.subprocess_shell(P, cmd)",
    "import subprocess\nsubprocess.run(['sh', '-c', f'vlog {x}'])",
    "import subprocess\nsubprocess.run(('/bin/bash', '-c', cmd))",
    # an allow comment without a reason does not count
    "import subprocess\nsubprocess.run(cmd, shell=True)  # ebs: allow-shell",
]

NEGATIVE_SAMPLES = [
    "import subprocess\nsubprocess.run(['ls', '-l'])",
    "import subprocess\nsubprocess.run(argv, shell=False, check=True)",
    "import subprocess\nsubprocess.Popen(argv)",
    "import asyncio\nasync def f():\n    await asyncio.create_subprocess_exec(*argv)",
    "import subprocess\nsubprocess.run(argv, shell=0)",
    "import subprocess\nsubprocess.run(argv, shell=None)",
    "import subprocess\nsubprocess.run(['bash', 'script.sh'])",
    "import subprocess\nsubprocess.run([tool, '-c', cfg])",
    "import subprocess\nsubprocess.run(cmd, shell=True)  # ebs: allow-shell trusted constant",
]


# R5
@pytest.mark.parametrize("source", POSITIVE_SAMPLES)
def test_detector_catches_positive_samples(source: str) -> None:
    findings, _ = scan_source(textwrap.dedent(source))
    assert len(findings) == 1, source


# R5
@pytest.mark.parametrize("source", NEGATIVE_SAMPLES)
def test_detector_ignores_safe_calls(source: str) -> None:
    findings, _ = scan_source(textwrap.dedent(source))
    assert findings == [], source


# R5
def test_scan_tree_reports_file_and_line(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "ok.py").write_text("x = 1\n")
    (pkg / "bad.py").write_text("import subprocess\n\nsubprocess.run('ls', shell=True)\n")
    (pkg / "allowed.py").write_text(
        "import os\nos.system('true')  # ebs: allow-shell fixed string, no user data\n"
    )
    findings, allow_comments = scan_tree(tmp_path)
    assert [str(f) for f in findings] == ["pkg/bad.py:3: subprocess.run(shell=...)"]
    assert allow_comments == ["pkg/allowed.py:2: 'fixed string, no user data'"]
