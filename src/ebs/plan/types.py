"""Plan data types (docs/design/interfaces.md § 4): action specs, input refs and the plan itself.

Everything here is immutable data. `ebs.plan.keys` hashes an `ActionSpec` into its action key,
`ebs.plan.planfile` encodes a `Plan` as canonical `plan.json`, `ebs.plan.planner` builds plans.
Mappings are stored read-only (`MappingProxyType`) and compare equal to plain dicts.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, TypeAlias

from ebs.core.digest import Digest
from ebs.flow.model import Resources

__all__ = [
    "ActionDiff",
    "ActionOutputInput",
    "ActionSpec",
    "DebugSpec",
    "DiffStatus",
    "FlowInfo",
    "GitInfo",
    "ImportInput",
    "InputKind",
    "InputRef",
    "InputSource",
    "OutputSpec",
    "OutputType",
    "ParamValue",
    "Plan",
    "PlanToolchain",
    "RuleRef",
    "SourceInput",
    "ToolchainRef",
]

InputKind: TypeAlias = Literal["file", "tree"]
OutputType: TypeAlias = Literal["file", "dir"]
ParamValue: TypeAlias = "str | int | bool | tuple[str, ...]"
"""A resolved param as the flow wrote it: str, int, bool, or a tuple from a list reference."""

PLAN_FORMAT_VERSION = 1


def _ro(mapping: Mapping[str, object]) -> MappingProxyType[str, object]:
    return MappingProxyType(dict(mapping))


@dataclass(frozen=True, slots=True)
class RuleRef:
    """The rule that expanded an action: step kind + rule implementation version (in the key)."""

    kind: str
    version: str


@dataclass(frozen=True, slots=True)
class ToolchainRef:
    """The toolchain an action runs with: flow-level name, module and immutable toolchain id.

    Only `id` is in the action key; the name and module are for humans.
    """

    name: str
    module: str
    id: Digest


@dataclass(frozen=True, slots=True)
class SourceInput:
    """A source file snapshotted at plan time: declared input name and the glob that matched it."""

    input: str
    pattern: str


@dataclass(frozen=True, slots=True)
class ActionOutputInput:
    """An output of another action of the same plan (a DAG edge)."""

    action_id: str
    output: str


@dataclass(frozen=True, slots=True)
class ImportInput:
    """An imported release (task P2-03); the planner does not produce these yet."""

    name: str
    path: str


InputSource: TypeAlias = "SourceInput | ActionOutputInput | ImportInput"


@dataclass(frozen=True, slots=True)
class InputRef:
    """One staged input: where the runner puts it and the content id it must have.

    `id` is None only for a deterministic output of an action that has not run yet (I14).
    """

    logical_path: str  # normalized, relative, POSIX; where the runner stages it
    kind: InputKind
    source: SourceInput | ActionOutputInput | ImportInput
    id: Digest | None


@dataclass(frozen=True, slots=True)
class OutputSpec:
    """A declared or rule-generated output of one action."""

    name: str
    path: str  # logical, relative to the work dir
    type: OutputType
    deterministic: bool = True
    optional: bool = False


@dataclass(frozen=True, slots=True)
class DebugSpec:
    """Files collected into the debug area (not in the key)."""

    collect: tuple[str, ...] = ()
    max_size: int | None = None  # bytes
    on_success: bool = False


@dataclass(frozen=True)
class ActionSpec:
    """One fully explicit action. Fields marked "not in key" never change the action key (I3)."""

    action_id: str  # "compile[lib=core]"; stable across runs
    step: str
    rule: RuleRef
    argv: tuple[str, ...]  # fully expanded; paths are logical (relative to the work dir)
    params: Mapping[str, ParamValue]
    env: Mapping[str, str]  # declared env only
    toolchain: ToolchainRef | None
    inputs: tuple[InputRef, ...]  # sorted by logical_path
    outputs: tuple[OutputSpec, ...]  # sorted by name
    config_files: Mapping[str, str]  # logical path -> content (hashed into the key)
    resources: Resources  # NOT in key
    licenses: Mapping[str, int]  # NOT in key
    debug: DebugSpec  # NOT in key
    domain: str  # NOT in key: the action cache is keyed by (domain, key)
    key: Digest | None  # None while any input id is None
    runtime_env: Mapping[str, str] = field(default_factory=dict)  # NOT in key; runner expands

    def __post_init__(self) -> None:
        for name in ("params", "env", "config_files", "licenses", "runtime_env"):
            object.__setattr__(self, name, _ro(getattr(self, name)))

    def input(self, logical_path: str) -> InputRef:
        """The input staged at `logical_path`; KeyError if there is none."""
        for ref in self.inputs:
            if ref.logical_path == logical_path:
                return ref
        raise KeyError(logical_path)

    def output(self, name: str) -> OutputSpec:
        """The output called `name`; KeyError if there is none."""
        for out in self.outputs:
            if out.name == name:
                return out
        raise KeyError(name)


@dataclass(frozen=True, slots=True)
class GitInfo:
    """Where the flow came from in version control (recorded, never hashed into keys)."""

    repo: str
    commit: str
    dirty: bool


@dataclass(frozen=True, slots=True)
class FlowInfo:
    """The flow file as the caller names it (usually relative to the repository root)."""

    path: str = "flow.yaml"
    git: GitInfo | None = None


@dataclass(frozen=True, slots=True)
class PlanToolchain:
    """A toolchain used by the plan: module and immutable id."""

    module: str
    id: Digest


@dataclass(frozen=True)
class Plan:
    """A fully expanded build plan; `ebs.plan.planfile` encodes it as `plan.json`.

    `actions` are in a topological order (producers before consumers), deterministic for a given
    flow; `edges` are `(producer action_id, output name, consumer action_id)`, sorted.
    """

    ebs_version: str
    domain: str
    project: str
    flow: FlowInfo
    toolchains: Mapping[str, PlanToolchain]
    actions: tuple[ActionSpec, ...]
    edges: tuple[tuple[str, str, str], ...]
    lock: Digest | None = None
    key_schema: int = 1
    v: int = PLAN_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "toolchains", _ro(self.toolchains))

    def action(self, action_id: str) -> ActionSpec:
        """The action with this id; KeyError if there is none."""
        for spec in self.actions:
            if spec.action_id == action_id:
                return spec
        raise KeyError(action_id)


DiffStatus: TypeAlias = Literal["added", "removed", "unchanged", "changed", "unknown"]


@dataclass(frozen=True, slots=True)
class ActionDiff:
    """Why one action will (or will not) rerun compared with an older plan.

    - `changed`: key fields that differ, e.g. `inputs["src/alu.sv"]`, `params.seed`, `toolchain`.
    - `pending`: for `unknown` (new key not computable yet), the producers it waits for, as
      `depends on <action_id>`.
    """

    action_id: str
    status: DiffStatus
    changed: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
