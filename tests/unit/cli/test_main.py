from __future__ import annotations

from importlib.metadata import version

from typer.testing import CliRunner

import ebs
from ebs.cli.main import app

runner = CliRunner()


# R1
def test_version_option_prints_package_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == f"ebs {version('ebs')}"


# R1
def test_dunder_version_comes_from_metadata() -> None:
    assert ebs.__version__ == version("ebs")


def test_no_arguments_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "Usage" in result.output
