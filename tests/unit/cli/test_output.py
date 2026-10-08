from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ebs.core.errors import ExitCode
from ebs.core.log import get_logger
from tests.helpers.cli import Site, TestServices, write_site

_SGR = re.compile(r"\x1b\[([0-9;]*)m")


def _has_color(text: str) -> bool:
    """True if an SGR sequence sets a foreground or background color (bold etc. is no color)."""
    for match in _SGR.finditer(text):
        params = [p for p in match.group(1).split(";") if p]
        if any(
            p in {"38", "48"} or (len(p) == 2 and p[0] in "349" and p[1] != "9") for p in params
        ):
            return True
    return False


def _tty_site(tmp_path: Path, **environ: str) -> Site:
    write_site(tmp_path)
    services = TestServices(tmp_path, tty=True)
    services.environ.update(environ)
    return Site(tmp_path, services)


# R6
def test_colors_on_a_terminal(tmp_path: Path) -> None:
    result = _tty_site(tmp_path).invoke(["plan"])
    assert result.exit_code == ExitCode.OK, result.output
    assert _has_color(result.stdout)


# R6
def test_no_color_is_honoured(tmp_path: Path) -> None:
    result = _tty_site(tmp_path, NO_COLOR="1").invoke(["plan"])
    assert result.exit_code == ExitCode.OK, result.output
    assert not _has_color(result.stdout)


# R6
def test_no_color_on_a_tty_build(tmp_path: Path) -> None:
    site = _tty_site(tmp_path, NO_COLOR="1")
    result = site.invoke(["build"])
    assert result.exit_code == ExitCode.OK, result.output
    assert not _has_color(result.stdout)
    assert "build passed" in result.stdout
    # the live view: one row per step with its state counts (review nit)
    assert re.search(r"gen\s+done=1", result.stdout), result.stdout
    assert re.search(r"sim\s+done=2", result.stdout), result.stdout


# R6
def test_rules_list(site: Site) -> None:
    result = site.invoke(["rules", "list"])
    assert result.exit_code == ExitCode.OK, result.output
    for kind in ("make", "shell", "tcl"):
        assert kind in result.stdout
    doc = json.loads(site.invoke(["rules", "list", "--json"]).stdout)
    kinds = [r["kind"] for r in doc["rules"]]
    assert kinds == sorted(kinds)
    assert {"make", "shell", "tcl"} <= set(kinds)
    assert all(isinstance(r["version"], str) and r["version"] for r in doc["rules"])


# R6
def test_every_command_has_json(site: Site) -> None:
    for command in (["plan"], ["build"], ["status"], ["logs"], ["rules", "list"]):
        result = site.invoke([*command, "--help"])
        assert result.exit_code == 0, command
        # Typer colors help when FORCE_COLOR/PY_COLORS is set at import; ANSI codes split "--json".
        assert "--json" in _SGR.sub("", result.stdout), command


# R5 (review: logging was bound to CliRunner's stderr, closed after the invocation)
def test_logging_outlives_the_invocation(site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    assert site.invoke(["rules", "list"]).exit_code == ExitCode.OK
    get_logger("tests.cli").warning("after.the.command")  # must not write to a closed stream
    assert "after.the.command" in capsys.readouterr().err
