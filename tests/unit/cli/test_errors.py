from __future__ import annotations

import json

import pytest

from ebs.cli._output import UsageError, exit_code_for
from ebs.core.errors import (
    CanonError,
    CasError,
    ConfigError,
    DigestError,
    ExecutorError,
    ExitCode,
    FlowError,
    MetadataError,
    PlanError,
    RuleError,
    SandboxError,
    SourceError,
    ToolchainError,
    TreeError,
)
from tests.helpers.cli import Site

BAD_FLOW = (
    "version: 1\nproject: demo\ndomain: test\nsteps:\n  gen:\n    kind: shell\n    bogus: 1\n"
)


# R5
def test_flow_error_rendering(site: Site) -> None:
    (site.proj / "flow.yaml").write_text(BAD_FLOW)
    result = site.invoke(["plan"])
    assert result.exit_code == ExitCode.USAGE
    lines = result.stderr.splitlines()
    assert len(lines) == 1, result.stderr  # one line, no traceback
    assert lines[0].startswith("ebs: error: ")
    assert "flow.yaml:7:5" in lines[0]  # file:line:col of the offending key
    assert "bogus" in lines[0]
    assert "Traceback" not in result.output


# R5
def test_debug_shows_traceback(site: Site) -> None:
    (site.proj / "flow.yaml").write_text(BAD_FLOW)
    result = site.invoke(["--debug", "plan"])
    assert result.exit_code == ExitCode.USAGE
    assert "Traceback" in result.stderr
    assert "ebs: error: " in result.stderr


# R5
def test_missing_flow_file_names_it_and_hints(site: Site) -> None:
    result = site.invoke(["plan", "-f", "nope.yaml"])
    assert result.exit_code == ExitCode.USAGE
    assert "nope.yaml" in result.stderr
    assert "-f" in result.stderr  # the hint says how to point at another flow


# R5
def test_internal_error_asks_for_report(site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise ZeroDivisionError("division by zero")

    monkeypatch.setattr("ebs.cli.plan.load_flow", boom)
    result = site.invoke(["plan"])
    assert result.exit_code == ExitCode.INTERNAL
    assert "please report" in result.stderr
    assert "--debug" in result.stderr
    assert "Traceback" not in result.stderr


# R5 R6
def test_json_error_document(site: Site) -> None:
    (site.proj / "flow.yaml").write_text(BAD_FLOW)
    result = site.invoke(["plan", "--json"])
    assert result.exit_code == ExitCode.USAGE
    doc = json.loads(result.stdout)
    assert doc["error"]["type"] == "FlowError"
    assert doc["error"]["file"].endswith("flow.yaml")
    assert (doc["error"]["line"], doc["error"]["col"]) == (7, 5)
    assert doc["exit_code"] == 2


# R5
@pytest.mark.parametrize(
    ("error", "code"),
    [
        (FlowError("x"), ExitCode.USAGE),
        (ConfigError("x"), ExitCode.USAGE),
        (PlanError("x"), ExitCode.USAGE),
        (SourceError("x"), ExitCode.USAGE),
        (MetadataError("x"), ExitCode.INFRA),
        (CasError("x"), ExitCode.INFRA),
        (ExecutorError("x"), ExitCode.INFRA),
        (KeyError("x"), ExitCode.INTERNAL),
        (RuleError("x"), ExitCode.USAGE),
        (ToolchainError("x"), ExitCode.USAGE),
        (DigestError("x"), ExitCode.USAGE),
        (CanonError("x"), ExitCode.USAGE),
        (TreeError("x"), ExitCode.USAGE),
        (UsageError("x"), ExitCode.USAGE),
        (SandboxError("x"), ExitCode.INFRA),
        (KeyboardInterrupt(), ExitCode.CANCELLED),
    ],
)
def test_exit_code_mapping(error: BaseException, code: ExitCode) -> None:
    assert exit_code_for(error) == code
