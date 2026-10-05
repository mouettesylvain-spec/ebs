"""Rule plugin contract (docs/design/interfaces.md § 7).

A rule turns one step instance into an `ActionTemplate` (argv, generated config files, implicit
inputs and outputs) and later classifies the tool's exit into PASSED / FAILED / INFRA(reason).
Plugins register under the entry point group `ebs.rules` (see `ebs.rules.registry`); each entry
point is a factory called with the site's `RuleSettings`.

Conventions every rule follows:
- argv and config file paths are logical: relative POSIX paths inside the action's work dir.
  Files a rule generates live under `.ebs/`.
- `ActionTemplate.env` is the declared env of the action and is part of the action key;
  `ActionTemplate.runtime_env` is set by the runner, is NOT part of the key, and is expanded with
  `expand_runtime_env` against the variables the runner sets (`EBS_CPUS`, `EBS_ACTION_ID`, …).
- `version` must change whenever the generated argv, config files, inputs or outputs change,
  because the planner puts `(kind, version)` in every action key.
"""

from __future__ import annotations

import re
import signal
import string
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Protocol

from ebs.core.errors import ConfigError, RuleError
from ebs.flow.interp import ParamValue, Resolver
from ebs.flow.matrix import StepInstance
from ebs.flow.model import OutputDef, StepDef

__all__ = [
    "CRASH_SIGNALS",
    "DEFAULT_LICENSE_ERROR_PATTERNS",
    "ENTRY_POINT_GROUP",
    "FAILED",
    "GENERATED_DIR",
    "INFRA",
    "PASSED",
    "ActionTemplate",
    "BaseRule",
    "Classification",
    "ExpandContext",
    "RuleFactory",
    "RulePlugin",
    "RuleSettings",
    "compile_license_patterns",
    "default_classify",
    "expand_runtime_env",
    "param_text",
    "render_command",
]

ENTRY_POINT_GROUP = "ebs.rules"

GENERATED_DIR = ".ebs"
"""Work-dir subdirectory holding the files rules generate (scripts, params, wrappers)."""

CRASH_SIGNALS = frozenset({signal.SIGKILL, signal.SIGSEGV, signal.SIGBUS})

DEFAULT_LICENSE_ERROR_PATTERNS: tuple[str, ...] = (
    r"(?i)unable to checkout",
    r"(?i)license checkout failed",
    r"(?i)cannot connect to license server",
    r"(?i)licensed number of users already reached",
    r"(?i)flexnet licensing error",
    r"(?i)flexlm error",
)
"""Generic FlexNet/FlexLM failure shapes; `[rules].license_error_patterns` replaces them."""


# --- classification ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Classification:
    """Outcome of a tool run: `passed`, `failed` (cacheable) or `infra` (retried, never cached)."""

    status: Literal["passed", "failed", "infra"]
    reason: str | None = None

    def __post_init__(self) -> None:
        if (self.status == "infra") != (self.reason is not None):
            raise ValueError("an infra classification needs a reason; passed/failed take none")


PASSED = Classification("passed")
FAILED = Classification("failed")


def INFRA(reason: str) -> Classification:
    """An infrastructure failure, e.g. `INFRA("license")` or `INFRA("tool_crash")`."""
    return Classification("infra", reason)


def _signal_of(exit_code: int) -> int | None:
    # Python reports a signal death as -N; shells and make report 128+N for a child's signal.
    if exit_code < 0:
        return -exit_code
    if 128 < exit_code < 128 + 65:
        return exit_code - 128
    return None


def default_classify(
    exit_code: int, log_tail: str, license_patterns: tuple[re.Pattern[str], ...]
) -> Classification:
    """Exit 0 ⇒ PASSED; SIGKILL/SIGSEGV/SIGBUS ⇒ INFRA("tool_crash"); a license error in the
    log tail ⇒ INFRA("license"); any other non-zero exit ⇒ FAILED.
    """
    if exit_code == 0:
        return PASSED
    if _signal_of(exit_code) in CRASH_SIGNALS:
        return INFRA("tool_crash")
    if any(p.search(log_tail) for p in license_patterns):
        return INFRA("license")
    return FAILED


# --- settings, context, template ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuleSettings:
    """Site settings passed to every rule factory (config section `[rules]`)."""

    license_error_patterns: tuple[str, ...] = DEFAULT_LICENSE_ERROR_PATTERNS


def compile_license_patterns(patterns: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    compiled = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern, re.MULTILINE))
        except re.error as exc:
            raise ConfigError(
                f"invalid regular expression {pattern!r} in [rules].license_error_patterns: "
                f"{exc}; fix or remove it in ebs.toml"
            ) from exc
    return tuple(compiled)


@dataclass(frozen=True)
class ExpandContext:
    """What a rule sees of one step instance: resolved row/params/env/resources, plus the
    planner's resolver for `${imports.…}` / `${steps.…}` references (None: such refs are errors).
    """

    instance: StepInstance
    resolver: Resolver | None = None

    def render(self, text: str, *, field: str) -> str | list[str]:
        return self.instance.render(text, field=field, resolver=self.resolver)

    def render_str(self, text: str, *, field: str) -> str:
        value = self.render(text, field=field)
        if isinstance(value, list):
            raise RuleError(
                f"step {self.instance.instance_id!r}: {field} {text!r} expands to a list of "
                f"{len(value)} paths, but a single value is needed here"
            )
        return value


def _frozen(mapping: Mapping[str, str] | None) -> Mapping[str, str]:
    return MappingProxyType(dict(mapping or {}))


@dataclass(frozen=True)
class ActionTemplate:
    """What a rule generates for one action; the planner turns it into an `ActionSpec`.

    - `argv`: the command, with logical paths (relative to the work dir).
    - `env`: declared env for the tool (in the action key).
    - `inputs`: implicit inputs, input name -> source glob relative to the flow directory, like
      `StepDef.inputs`; names start with `ebs.` so they never collide with declared inputs.
    - `outputs`: implicit outputs added to the declared ones.
    - `config_files`: logical path -> content, written into the work dir and hashed into the key.
    - `runtime_env`: set by the runner after its own variables, NOT in the key (e.g. make `-j`).
    """

    argv: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    inputs: Mapping[str, str] = field(default_factory=dict)
    outputs: Mapping[str, OutputDef] = field(default_factory=dict)
    config_files: Mapping[str, str] = field(default_factory=dict)
    runtime_env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.argv or not self.argv[0]:
            raise RuleError("a rule produced an empty command line (argv[0] is missing)")
        for name in ("env", "inputs", "config_files", "runtime_env"):
            object.__setattr__(self, name, _frozen(getattr(self, name)))
        object.__setattr__(self, "outputs", MappingProxyType(dict(self.outputs)))


def expand_runtime_env(runtime_env: Mapping[str, str], values: Mapping[str, str]) -> dict[str, str]:
    """Expand `$NAME` / `${NAME}` in runtime env values (runner side); `$$` is a literal `$`."""
    expanded = {}
    for name, value in runtime_env.items():
        try:
            expanded[name] = string.Template(value).substitute(values)
        except (KeyError, ValueError) as exc:
            raise RuleError(
                f"runtime env {name}={value!r} refers to {exc}, which the runner does not set; "
                f"available: {', '.join(sorted(values)) or 'none'}"
            ) from exc
    return expanded


def render_command(command: tuple[str, ...], ctx: ExpandContext, *, start: int = 0) -> list[str]:
    """Render each argv element; an element that is a sole list reference is spliced in.

    `start` is the index of `command[0]` in the step's `command` (for error locations).
    """
    argv: list[str] = []
    for i, element in enumerate(command, start):
        value = ctx.render(element, field=f"command[{i}]")
        argv.extend(value if isinstance(value, list) else [value])
    return argv


def param_text(value: ParamValue) -> str:
    """A resolved param as text: ints in decimal, bools as `true`/`false`, lists space-joined."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, tuple):
        return " ".join(value)
    return str(value)


# --- plugin protocol ---------------------------------------------------------------------------


class RulePlugin(Protocol):
    kind: str
    version: str

    def validate(self, step: StepDef) -> None: ...

    def expand(self, step: StepDef, ctx: ExpandContext) -> ActionTemplate: ...

    def classify(
        self, exit_code: int, log_tail: str, outputs: Mapping[str, Path]
    ) -> Classification: ...

    def summarize(self, outputs: Mapping[str, Path], log: Path) -> dict[str, str | int]: ...


RuleFactory = Callable[[RuleSettings], RulePlugin]
"""What an `ebs.rules` entry point loads: a callable (usually the plugin class) taking settings."""


class BaseRule:
    """Default `validate` / `classify` / `summarize`; subclasses set `kind`, `version`, `expand`."""

    kind: str = ""
    version: str = ""

    def __init__(self, settings: RuleSettings | None = None) -> None:
        settings = settings or RuleSettings()
        self._license_patterns = compile_license_patterns(settings.license_error_patterns)

    def validate(self, step: StepDef) -> None:
        return None

    def expand(self, step: StepDef, ctx: ExpandContext) -> ActionTemplate:
        raise NotImplementedError

    def classify(
        self, exit_code: int, log_tail: str, outputs: Mapping[str, Path]
    ) -> Classification:
        return default_classify(exit_code, log_tail, self._license_patterns)

    def summarize(self, outputs: Mapping[str, Path], log: Path) -> dict[str, str | int]:
        return {}

    def _reject_fields(self, step: StepDef, names: tuple[str, ...], hint: str) -> None:
        for name in names:
            if getattr(step, name) is not None:
                raise RuleError(f"a {self.kind!r} step does not take {name!r}: {hint}")
