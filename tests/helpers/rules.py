"""Shared helpers for rule plugin tests (tests/unit/rules)."""

from __future__ import annotations

import signal
from collections.abc import Mapping
from typing import Any

from ebs.flow.matrix import StepInstance, expand_matrix
from ebs.flow.model import StepDef
from ebs.rules.api import FAILED, INFRA, PASSED, Classification, ExpandContext

LICENSE_LOG = "Error: license checkout failed for feature 'x'\n"

# (exit code, log tail, expected classification) shared by every rule's `test_classify` (R5).
CLASSIFY_CASES: list[tuple[int, str, Classification]] = [
    (0, "", PASSED),
    (0, LICENSE_LOG, PASSED),  # the tool recovered: exit status wins
    (1, "", FAILED),
    (2, "make: *** [lint] Error 1\n", FAILED),
    (-signal.SIGTERM, "", FAILED),
    (-signal.SIGKILL, "", INFRA("tool_crash")),
    (-signal.SIGSEGV, "", INFRA("tool_crash")),
    (-signal.SIGBUS, "", INFRA("tool_crash")),
    (128 + signal.SIGSEGV, "Segmentation fault\n", INFRA("tool_crash")),  # via bash/make
    (-signal.SIGSEGV, LICENSE_LOG, INFRA("tool_crash")),
    (1, LICENSE_LOG, INFRA("license")),
    (3, "noise\nUnable to checkout license\nmore noise\n", INFRA("license")),
    (1, "Cannot connect to license server system.\n", INFRA("license")),
]


def step(**fields: Any) -> StepDef:
    return StepDef.model_validate(fields)


def instance(step_def: StepDef, name: str = "s") -> StepInstance:
    (only,) = expand_matrix(step_def, {}, name=name)
    return only


def context(step_def: StepDef, name: str = "s") -> ExpandContext:
    return ExpandContext(instance(step_def, name))


def as_dict(mapping: Mapping[str, str]) -> dict[str, str]:
    return dict(mapping)
