"""Tests for `ebs.rules.registry` and the shared API (task P0-12 R1, R3 runtime env)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from ebs.core.errors import ConfigError, RuleError
from ebs.flow.model import StepDef
from ebs.rules.api import (
    ENTRY_POINT_GROUP,
    FAILED,
    INFRA,
    PASSED,
    ActionTemplate,
    BaseRule,
    Classification,
    ExpandContext,
    RuleSettings,
    expand_runtime_env,
)
from ebs.rules.make import MakeRule
from ebs.rules.registry import RuleRegistry
from ebs.rules.shell import ShellRule
from ebs.rules.tcl import TclRule


class _Rule(BaseRule):
    kind = "fake.sim"
    version = "1"

    def expand(self, step: StepDef, ctx: ExpandContext) -> ActionTemplate:
        return ActionTemplate(argv=("true",))


@dataclass
class _EntryPoint:
    """Duck-typed `importlib.metadata.EntryPoint`: name, value, load()."""

    name: str
    target: Any
    value: str = "tests.fake:factory"

    def load(self) -> Any:
        return self.target


def _rule(kind: str = "fake.sim", version: Any = "1") -> type[BaseRule]:
    return type("R", (_Rule,), {"kind": kind, "version": version})


# R1
def test_entry_points() -> None:
    # The built-ins are registered in pyproject.toml and found through installed metadata.
    registry = RuleRegistry.from_entry_points()
    assert {"shell", "make", "tcl"} <= set(registry.kinds())
    assert isinstance(registry.get("shell"), ShellRule)
    assert isinstance(registry.get("make"), MakeRule)
    assert isinstance(registry.get("tcl"), TclRule)

    # Injected entry points: factories are called with the settings; listing is sorted by kind.
    settings = RuleSettings(license_error_patterns=("LICENSE GONE",))
    registry = RuleRegistry.from_entry_points(
        settings=settings,
        entry_points=[_EntryPoint("zeta.a", _rule("zeta.a")), _EntryPoint("alpha", _rule("alpha"))],
    )
    assert registry.kinds() == ("alpha", "zeta.a")
    assert [p.kind for p in registry.plugins()] == ["alpha", "zeta.a"]
    assert registry.get("alpha").classify(1, "LICENSE GONE", {}) == INFRA("license")
    assert ENTRY_POINT_GROUP == "ebs.rules"


# R1
def test_unknown_kind_suggests() -> None:
    registry = RuleRegistry([ShellRule(), MakeRule()])
    with pytest.raises(RuleError, match=r"unknown step kind 'shel'.*did you mean 'shell'"):
        registry.get("shel")
    with pytest.raises(RuleError, match="available kinds: make, shell"):
        registry.get("questa.sim")


# R1
def test_duplicate_kind() -> None:
    registry = RuleRegistry([ShellRule()])
    with pytest.raises(RuleError, match="duplicate rule kind 'shell'"):
        registry.register(ShellRule())
    with pytest.raises(RuleError, match=r"duplicate rule kind 'fake\.sim'.*tests\.fake:other"):
        RuleRegistry.from_entry_points(
            entry_points=[
                _EntryPoint("fake.sim", _rule()),
                _EntryPoint("fake.sim", _rule(), value="tests.fake:other"),
            ]
        )


# R1
@pytest.mark.parametrize("version", ["", "   ", None, 1, b"1"])
def test_bad_version(version: Any) -> None:
    with pytest.raises(RuleError, match=r"rule 'fake\.sim'.*version.*non-empty string"):
        RuleRegistry([_rule(version=version)()])
    with pytest.raises(RuleError, match="version"):
        RuleRegistry.from_entry_points(
            entry_points=[_EntryPoint("fake.sim", _rule(version=version))]
        )


# R1
def test_bad_plugins_rejected() -> None:
    with pytest.raises(RuleError, match="kind"):
        RuleRegistry([_rule(kind="Bad Kind")()])
    with pytest.raises(RuleError, match=r"entry point 'other' .* kind 'fake\.sim'"):
        RuleRegistry.from_entry_points(entry_points=[_EntryPoint("other", _rule())])

    def boom() -> None:
        raise ImportError("no module named vendor_pack")

    class _Broken(_EntryPoint):
        def load(self) -> Any:
            boom()

    with pytest.raises(RuleError, match=r"could not load rule plugin 'x'.*vendor_pack"):
        RuleRegistry.from_entry_points(entry_points=[_Broken("x", None)])
    with pytest.raises(RuleError, match="expand"):
        RuleRegistry.from_entry_points(
            entry_points=[
                _EntryPoint("x", lambda _s: type("P", (), {"kind": "x", "version": "1"})())
            ]
        )


def test_classification_values() -> None:
    assert Classification("passed") == PASSED
    assert Classification("failed") == FAILED
    assert INFRA("license") == Classification("infra", "license")
    with pytest.raises(ValueError, match="reason"):
        Classification("infra")
    with pytest.raises(ValueError, match="reason"):
        Classification("passed", "x")


def test_bad_license_pattern_is_config_error() -> None:
    with pytest.raises(ConfigError, match=r"'\('.*\[rules\]\.license_error_patterns"):
        ShellRule(RuleSettings(license_error_patterns=("(",)))
    # Through the registry it stays a ConfigError (not a "could not load plugin" RuleError).
    with pytest.raises(ConfigError, match=r"\[rules\]\.license_error_patterns"):
        RuleRegistry.from_entry_points(settings=RuleSettings(license_error_patterns=("(",)))


# R3 (the runner expands rule runtime env, which is not part of the key)
def test_expand_runtime_env() -> None:
    env: Mapping[str, str] = {"MAKEFLAGS": "-j$EBS_CPUS", "LIT": "$$x ${EBS_ACTION_ID}"}
    got = expand_runtime_env(env, {"EBS_CPUS": "8", "EBS_ACTION_ID": "lint"})
    assert got == {"MAKEFLAGS": "-j8", "LIT": "$x lint"}
    with pytest.raises(RuleError, match="EBS_NOPE"):
        expand_runtime_env({"X": "$EBS_NOPE"}, {})


def test_default_summarize_is_empty(tmp_path: Path) -> None:
    assert ShellRule().summarize({}, tmp_path / "log") == {}
