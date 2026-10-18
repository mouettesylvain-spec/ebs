"""Docs that run (P0-17 R5, D-01 R1): every `console` block of every page of the site's
"start" and "tutorials" sections (site/src/content/docs/) is executed, one page per test.

A block line starting with `$ ` is a command; the lines after it, up to the next command, are
expected output. Each expected line must equal some line of the command's stdout, where `…`
matches any text (UUIDs, timings). `cd` changes the working directory, other commands run as
argv lists (never through a shell) from a fresh copy of the repository's `examples/`, against a
fresh site and database.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from sqlalchemy.engine import URL

from tests.helpers.e2e import EXAMPLES, REPO_ROOT, Site
from tests.helpers.pg import _pg_truncate_engine, pg_migrated, pg_server, pg_url

__all__ = ["_pg_truncate_engine", "pg_migrated", "pg_server", "pg_url"]

DOCS = REPO_ROOT / "site" / "src" / "content" / "docs"
EXECUTED_SECTIONS = ("start", "tutorials")
# A fence may be indented (a block inside a list item); its lines share that indentation.
_BLOCK = re.compile(r"^([ \t]*)```console\b[^\n]*\n(.*?)^\1```", re.MULTILINE | re.DOTALL)


@dataclass
class Command:
    argv: list[str]
    expected: list[str] = field(default_factory=list)


def console_commands(markdown: str) -> list[Command]:
    commands: list[Command] = []
    for indent, block in _BLOCK.findall(markdown):
        for raw in block.splitlines():
            line = raw.removeprefix(indent)
            if line.startswith("$ "):
                commands.append(Command(shlex.split(line[2:])))
            elif line.strip():
                assert commands, f"output line before any command: {line!r}"
                commands[-1].expected.append(line.rstrip())
    return commands


def _pattern(expected: str) -> re.Pattern[str]:
    return re.compile(".*".join(re.escape(part) for part in expected.split("…")))


def test_console_commands_parse() -> None:
    commands = console_commands(
        "text\n```console\n$ cd a\n$ ebs build\nbuild … passed\n\n```\n```toml\n$ no\n```\n"
    )
    assert [c.argv for c in commands] == [["cd", "a"], ["ebs", "build"]]
    assert commands[1].expected == ["build … passed"]
    indented = console_commands("1. x\n\n   ```console\n   $ cd a\n     out\n   ```\n")
    assert [c.argv for c in indented] == [["cd", "a"]]
    assert indented[0].expected == ["  out"]
    titled = console_commands('```console title="x"\n$ ebs plan\n```\n')
    assert [c.argv for c in titled] == [["ebs", "plan"]]
    assert _pattern("build … passed").fullmatch("build 1234 passed")
    assert not _pattern("build … passed").fullmatch("build 1234 failed")


def executed_pages() -> list[Path]:
    """Pages whose console blocks run: every .md/.mdx page of EXECUTED_SECTIONS with one."""
    pages = sorted(
        page
        for section in EXECUTED_SECTIONS
        for page in (DOCS / section).rglob("*.md*")
        if page.suffix in {".md", ".mdx"} and _BLOCK.search(page.read_text(encoding="utf-8"))
    )
    return pages


def test_executed_pages_include_quickstart() -> None:
    names = {page.relative_to(DOCS).as_posix() for page in executed_pages()}
    assert "start/quickstart.md" in names
    assert any(name.startswith("tutorials/") for name in names), names


# P0-17 R5, D-01 R1
@pytest.mark.parametrize(
    "page", executed_pages(), ids=lambda p: p.relative_to(DOCS).with_suffix("").as_posix()
)
def test_page_blocks(page: Path, tmp_path: Path, pg_url: URL) -> None:
    commands = console_commands(page.read_text(encoding="utf-8"))
    assert any(c.argv[0] == "ebs" for c in commands), f"{page.name} runs no ebs command"
    site = Site(tmp_path / "site", pg_url)
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    shutil.copytree(EXAMPLES, repo / "examples", ignore=shutil.ignore_patterns(".ebs"))
    cwd = repo
    for command in commands:
        shown = shlex.join(command.argv)
        if command.argv[0] == "cd":
            cwd = (cwd / command.argv[1]).resolve()
            assert cwd.is_dir(), shown
            continue
        if command.argv[0] == "ebs":
            proc = site.ebs(cwd, *command.argv[1:])
        else:
            proc = subprocess.run(
                command.argv, cwd=cwd, env=site.env(), capture_output=True, text=True,
                timeout=60, check=False,
            )  # fmt: skip
        assert proc.returncode == 0, f"$ {shown}\n{proc.stdout}{proc.stderr}"
        actual = proc.stdout.splitlines()
        for line in command.expected:
            if not any(_pattern(line).fullmatch(a.rstrip()) for a in actual):
                pytest.fail(f"$ {shown}: no output line matches {line!r}; got:\n{proc.stdout}")
