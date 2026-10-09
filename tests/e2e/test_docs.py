"""Docs that run (P0-17 R5): every `console` block of docs/user/quickstart.md is executed.

A block line starting with `$ ` is a command; the lines after it, up to the next command, are
expected output. Each expected line must equal some line of the command's stdout, where `…`
matches any text (UUIDs, timings). `cd` changes the working directory, other commands run as
argv lists (never through a shell) from a copy of the repository's `examples/`.
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

QUICKSTART = REPO_ROOT / "docs" / "user" / "quickstart.md"
_BLOCK = re.compile(r"^```console\n(.*?)^```", re.MULTILINE | re.DOTALL)


@dataclass
class Command:
    argv: list[str]
    expected: list[str] = field(default_factory=list)


def console_commands(markdown: str) -> list[Command]:
    commands: list[Command] = []
    for block in _BLOCK.findall(markdown):
        for line in block.splitlines():
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
    assert _pattern("build … passed").fullmatch("build 1234 passed")
    assert not _pattern("build … passed").fullmatch("build 1234 failed")


# R5
def test_quickstart_blocks(tmp_path: Path, pg_url: URL) -> None:
    commands = console_commands(QUICKSTART.read_text(encoding="utf-8"))
    assert any(c.argv[0] == "ebs" for c in commands), "the quickstart runs no ebs command"
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
