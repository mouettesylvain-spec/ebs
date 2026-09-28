from __future__ import annotations

import pytest

from ebs.core import errors
from ebs.core.errors import EbsError, ExitCode, FlowError

SUBCLASSES = [
    "ConfigError",
    "FlowError",
    "DigestError",
    "CanonError",
    "PlanError",
    "CasError",
    "MetadataError",
    "ExecutorError",
    "RuleError",
    "SandboxError",
    "TreeError",
]


@pytest.mark.parametrize("name", SUBCLASSES)
def test_hierarchy_matches_interfaces(name: str) -> None:
    cls = getattr(errors, name)
    assert issubclass(cls, EbsError)
    assert cls.__bases__ == (EbsError,)


def test_ebs_error_is_an_exception_with_message() -> None:
    err = EbsError("could not read cas root /cas/x: permission denied; check group membership")
    assert isinstance(err, Exception)
    assert "permission denied" in str(err)


def test_flow_error_prefixes_location() -> None:
    err = FlowError("unknown step 'compil'", file="flow.yaml", line=12, col=5)
    assert (err.file, err.line, err.col) == ("flow.yaml", 12, 5)
    assert str(err) == "flow.yaml:12:5: unknown step 'compil'"
    assert err.message == "unknown step 'compil'"


def test_flow_error_without_column_or_line() -> None:
    assert str(FlowError("bad", file="flow.yaml", line=3)) == "flow.yaml:3: bad"
    assert str(FlowError("bad", file="flow.yaml")) == "flow.yaml: bad"
    assert str(FlowError("bad")) == "bad"


def test_exit_codes_match_interfaces() -> None:
    assert [(c.name, int(c)) for c in ExitCode] == [
        ("OK", 0),
        ("ACTIONS_FAILED", 1),
        ("USAGE", 2),
        ("INFRA", 3),
        ("INTERNAL", 4),
    ]


def test_flow_error_keeps_zero_line_and_column() -> None:
    # ruamel.yaml columns are 0-based, so 0 is a real position.
    assert str(FlowError("x", file="f.yaml", line=0, col=0)) == "f.yaml:0:0: x"
