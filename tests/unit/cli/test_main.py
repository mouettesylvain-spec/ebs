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


# D-01 (site: Integrations > Shell and Make)
def test_completion_options_are_offered() -> None:
    result = runner.invoke(app, ["--help"], env={"NO_COLOR": "1", "COLUMNS": "200"})
    assert result.exit_code == 0
    assert "--install-completion" in result.output
    assert "--show-completion" in result.output
