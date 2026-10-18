"""The planner: flow -> static DAG of fully explicit, keyed actions (interfaces.md § 4).

Planning is pure apart from the injected collaborators: the source snapshotter (globs -> CAS
trees), the CAS (to read those trees back), the toolchain resolver and the rule registry.

How a flow becomes actions:
1. Every `${steps.S.outputs.O[sel]}` reference in a step makes the step depend on S. The step
   graph is ordered topologically (cycles are reported with their full path) and restricted to
   the targets plus their transitive dependencies.
2. Each step is expanded over its matrix rows; its rule turns every instance into an
   `ActionTemplate`; the planner renders outputs, config files and debug patterns, checks that
   every path stays inside the scratch work dir (sandbox v1) and builds the `ActionSpec`.
3. Inputs: each file a source glob matches is one file input at its flow-relative path, with
   the snapshot digest as id. An output reference stages the producer's output at
   `<output>/<producer instance id>` (a tree, or a file `<output>/<instance id>/<file name>`),
   and renders to that path. Its id is `nondeterministic_output_id(producer key, output)` for
   `deterministic: false` outputs, else unknown (None) until the producer ran: `refine`.
4. Imports (P2-03): `${imports.A}` renders `imports/A`. Planning needs the `flow.lock` pins
   (`PlanLock`) and checks every import against them (the planner never resolves a channel).
   An input `${imports.A}` stages every output of the pinned release whole, at
   `imports/A/<output>`; `${imports.A}/<output>` one of them; any other `${imports.A}/<glob>`
   is matched (`match_paths`) against the release's files, read from the tree manifests in this
   domain's CAS: one file input per match. Ids are the ones the release recorded, never
   re-hashed; a whole output's id is the release's (for `deterministic: false`, not its bytes).
5. The key is computed once every input id is known (`ebs.plan.keys`).
"""

from __future__ import annotations

import dataclasses
import difflib
import posixpath
import re
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Protocol

import ebs
from ebs.cas.api import CAS
from ebs.core.digest import Digest
from ebs.core.errors import CasError, PlanError, RuleError
from ebs.flow.interp import Ref, SourceLocation, parse_template
from ebs.flow.matrix import StepInstance, expand_matrix, load_matrix_tables
from ebs.flow.model import (
    Flow,
    InputDef,
    OutputDef,
    Resources,
    StepDef,
    and_constraints,
    input_texts,
    merge_licenses,
    parse_memory,
)
from ebs.plan.graph import StepGraph
from ebs.plan.keys import KEY_SCHEMA_VERSION, nondeterministic_output_id, with_key
from ebs.plan.types import (
    ActionOutputInput,
    ActionSpec,
    DebugSpec,
    FlowInfo,
    ImportInput,
    ImportPin,
    InputKind,
    InputRef,
    OutputSpec,
    OutputType,
    Plan,
    PlanLock,
    PlanToolchain,
    RuleRef,
    SourceInput,
    ToolchainRef,
)
from ebs.rules.api import ActionTemplate, ExpandContext, RulePlugin
from ebs.rules.registry import RuleRegistry
from ebs.sources.globs import match_paths
from ebs.sources.snapshot import SnapshotResult
from ebs.toolchain.model import Toolchain, ToolchainResolver

__all__ = ["SYSTEM_PATHS", "Planner", "Snapshotter", "refine_plan"]

SYSTEM_PATHS: tuple[str, ...] = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/dev/null",
    "/dev/stdin",
    "/dev/stdout",
    "/dev/stderr",
)
"""Absolute paths an argv may name besides the toolchain's install roots (OS image, read-only)."""

IMPORTS_DIR = "imports"
"""Imported releases are staged at `imports/<alias>/<release output>`."""

_ARG_SPLIT = re.compile(r"[=+,:]")
_URL = re.compile(r"(?:https?|ftp)://[^\s,]*")  # remote URLs only; file:// is a path
_OPTION_PREFIX = re.compile(r"-{1,2}[A-Za-z][A-Za-z0-9_-]*?(?=[/~.])")


class Snapshotter(Protocol):
    """What the planner needs of `ebs.sources.SourceSnapshotter` (fakes implement it in tests)."""

    @property
    def rehash(self) -> bool: ...

    def snapshot(self, base: Path, pattern: str, *, optional: bool = False) -> SnapshotResult: ...


def _suggest(name: str, choices: Sequence[str]) -> str:
    close = difflib.get_close_matches(name, choices, n=1, cutoff=0.6)
    if close:
        return f"; did you mean {close[0]!r}?"
    return f"; known: {', '.join(sorted(choices)) or 'none'}"


def _strings(value: object, path: str) -> Iterator[tuple[str, str]]:
    """Every string (dict keys included) in a dumped step model, with its field path."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _strings(key, f"{path}.{key}" if path else str(key))
            yield from _strings(child, f"{path}.{key}" if path else str(key))
    elif isinstance(value, (list, tuple)):
        for i, child in enumerate(value):
            yield from _strings(child, f"{path}[{i}]")


def _staged_path(out: OutputSpec, producer: str) -> str:
    if out.type == "dir":
        return f"{out.name}/{producer}"
    return f"{out.name}/{producer}/{posixpath.basename(out.path)}"


def _under(path: str, root: str) -> bool:
    root = root.rstrip("/") or "/"
    return path == root or path.startswith(root + "/") or root == "/"


@dataclasses.dataclass(frozen=True)
class _StaticRef:
    ref: Ref
    field: str


@dataclasses.dataclass
class _Planned:
    step: StepDef
    instances: list[tuple[StepInstance, ActionSpec]]


class Planner:
    """Builds plans; one instance can plan several flows sharing its collaborators."""

    def __init__(
        self, cas: CAS, sources: Snapshotter, toolchains: ToolchainResolver, rules: RuleRegistry
    ) -> None:
        self._cas = cas
        self._sources = sources
        self._toolchains = toolchains
        self._rules = rules

    def plan(
        self,
        flow: Flow,
        *,
        base: Path,
        info: FlowInfo | None = None,
        targets: Sequence[str] = (),
        rehash: bool = False,
        lock: PlanLock | None = None,
    ) -> Plan:
        """Expand `flow` (whose tables and sources live under `base`) into a plan.

        `targets` restricts the plan to those steps and their transitive dependencies.
        `rehash` asserts that the snapshotter ignores the stat cache (CI/release builds).
        `lock` holds the `flow.lock` pins; a flow with imports cannot be planned without them.
        """
        if rehash and not self._sources.rehash:
            raise PlanError(
                "plan(rehash=True) needs a source snapshotter created with rehash=True, so that "
                "no source id comes from the stat cache"
            )
        return _Run(self, flow, base, lock).plan(info or FlowInfo(), targets)

    def refine(self, plan: Plan, produced: Mapping[tuple[str, str], Digest]) -> Plan:
        """`refine_plan` (needs nothing of the planner: a resumed driver refines without one)."""
        return refine_plan(plan, produced)


def refine_plan(plan: Plan, produced: Mapping[tuple[str, str], Digest]) -> Plan:
    """Fill in input ids and keys that `produced` ((action_id, output) -> id) makes known.

    Idempotent; producers are refined before their consumers, so one call propagates
    through chains of nondeterministic outputs.
    """
    index = {a.action_id: a for a in plan.actions}
    for action_id, output in produced:
        if action_id not in index:
            hint = _suggest(action_id, list(index))
            raise PlanError(f"refine: no action {action_id!r} in this plan{hint}")
        if output not in {o.name for o in index[action_id].outputs}:
            raise PlanError(f"refine: action {action_id!r} has no output {output!r}")
    updated: dict[str, ActionSpec] = {}
    actions = []
    for spec in plan.actions:
        inputs = []
        for ref in spec.inputs:
            source = ref.source
            if isinstance(source, ActionOutputInput):
                producer = updated.get(source.action_id, index[source.action_id])
                out = producer.output(source.output)
                if out.deterministic:  # content digest: the latest produced one wins
                    new_id = produced.get((source.action_id, source.output), ref.id)
                else:  # never from `produced`: only the producer's key (I14)
                    new_id = _output_id(producer, out)
                if new_id != ref.id:
                    ref = dataclasses.replace(ref, id=new_id)
            inputs.append(ref)
        refined = with_key(dataclasses.replace(spec, inputs=tuple(inputs)))
        updated[spec.action_id] = refined
        actions.append(refined)
    return dataclasses.replace(plan, actions=tuple(actions))


def _output_id(producer: ActionSpec, out: OutputSpec) -> Digest | None:
    if out.deterministic or producer.key is None:
        return None
    return nondeterministic_output_id(producer.key, out.name)


class _Run:
    """State of one `Planner.plan` call; also the resolver for `${steps.…}` references."""

    def __init__(self, planner: Planner, flow: Flow, base: Path, lock: PlanLock | None) -> None:
        self._p = planner
        self._flow = flow
        self._base = base
        self._lock = lock
        self._planned: dict[str, _Planned] = {}
        self._toolchains: dict[str, Toolchain] = {}
        self._snapshots: dict[str, list[tuple[str, Digest]]] = {}
        self._trees: dict[Digest, list[tuple[str, Digest | None]]] = {}
        self._edges: set[tuple[str, str, str]] = set()

    # -- graph ------------------------------------------------------------------------------

    def _static_refs(self, name: str, step: StepDef) -> list[_StaticRef]:
        refs: dict[Ref, _StaticRef] = {}
        for field, text in _strings(step.model_dump(by_alias=True), ""):
            if "${" not in text:
                continue
            where = SourceLocation(step=name, field=field)
            for ref in parse_template(text, where=where).refs:
                if ref.kind == "imports" and ref.name not in self._flow.imports:
                    raise PlanError(
                        f"{where}: {ref.text} refers to unknown import {ref.name!r}"
                        f"{_suggest(ref.name, list(self._flow.imports))}"
                    )
                if ref.kind != "steps" or ref in refs:
                    continue
                if ref.name not in self._flow.steps:
                    raise PlanError(
                        f"{where}: {ref.text} refers to unknown step {ref.name!r}"
                        f"{_suggest(ref.name, list(self._flow.steps))}"
                    )
                refs[ref] = _StaticRef(ref, field)
        return list(refs.values())

    def plan(self, info: FlowInfo, targets: Sequence[str]) -> Plan:
        steps = self._flow.steps
        for target in targets:
            if target not in steps:
                raise PlanError(f"unknown target {target!r}{_suggest(target, list(steps))}")
        pins = self._check_imports()
        graph = StepGraph(steps)
        refs = {name: self._static_refs(name, step) for name, step in steps.items()}
        for name, step_refs in refs.items():
            for r in step_refs:
                graph.add_edge(r.ref.name, name)
        order = graph.topo_order()
        wanted = graph.closure(targets)
        actions: list[ActionSpec] = []
        for name in order:
            if name in wanted:
                actions.extend(self._plan_step(name, steps[name], refs[name]))
        return Plan(
            ebs_version=ebs.__version__,
            domain=self._flow.domain,
            project=self._flow.project,
            flow=info,
            toolchains={
                name: PlanToolchain(tc.module, tc.id, tc.env)
                for name, tc in sorted(self._toolchains.items())
            },
            actions=tuple(actions),
            edges=tuple(sorted(self._edges)),
            lock=None if self._lock is None or not pins else self._lock.digest,
            key_schema=KEY_SCHEMA_VERSION,
        )

    def _check_imports(self) -> dict[str, ImportPin]:
        """The pin of every import; PlanError if one is missing or stale (R2)."""
        pins: dict[str, ImportPin] = {}
        for alias, imp in self._flow.imports.items():
            fix = f"run `ebs lock update {alias}` and review the flow.lock change"
            where = f"imports.{alias}"
            if self._lock is None:
                raise PlanError(
                    f"{where}: no flow.lock next to the flow pins the release it imports; {fix}"
                )
            pin = self._lock.imports.get(alias)
            if pin is None:
                raise PlanError(f"{where}: flow.lock has no entry for import {alias!r}; {fix}")
            pinned = f"{pin.name} {pin.version}"
            if pin.name != imp.from_:
                raise PlanError(
                    f"{where}: flow.lock pins release {pinned}, but the flow imports "
                    f"{imp.from_}; {fix}"
                )
            if pin.domain != self._flow.domain:
                raise PlanError(
                    f"{where}: flow.lock pins a release of domain {pin.domain!r}, but the flow is "
                    f"in domain {self._flow.domain!r}; {fix}"
                )
            origin = f"channel {pin.channel!r}" if pin.channel else f"version {pin.version}"
            if imp.channel is not None and pin.channel != imp.channel:
                raise PlanError(
                    f"{where}: flow.lock pins {pinned} from {origin}, but the flow asks for "
                    f"channel {imp.channel!r}; {fix}"
                )
            if imp.version is not None and (pin.version != imp.version or pin.channel):
                raise PlanError(
                    f"{where}: flow.lock pins {pinned}, but the flow asks for version "
                    f"{imp.version}; {fix}"
                )
            pins[alias] = pin
        return pins

    # -- steps ------------------------------------------------------------------------------

    def _rule(self, name: str, step: StepDef) -> RulePlugin:
        try:
            rule = self._p._rules.get(step.kind)
            rule.validate(step)
        except RuleError as exc:
            raise PlanError(f"steps.{name}: {exc}") from exc
        return rule

    def _toolchain(self, name: str | None) -> Toolchain | None:
        if name is None:
            return None
        if name not in self._toolchains:
            ref = self._flow.toolchains[name]  # the flow model checked the name exists
            self._toolchains[name] = self._p._toolchains.resolve(name, ref.module)
        return self._toolchains[name]

    def _plan_step(self, name: str, step: StepDef, refs: list[_StaticRef]) -> list[ActionSpec]:
        rule = self._rule(name, step)
        toolchain = self._toolchain(step.toolchain)
        tables = load_matrix_tables(step.matrix, self._base) if step.matrix is not None else {}
        instances = expand_matrix(step, tables, name=name, resolver=self)
        planned = _Planned(step, [])
        for instance in instances:
            spec = self._action(name, step, rule, instance, toolchain, refs)
            planned.instances.append((instance, spec))
        self._planned[name] = planned
        return [spec for _, spec in planned.instances]

    # -- resolver ---------------------------------------------------------------------------

    def resolve(self, ref: Ref, *, where: SourceLocation) -> str | list[str]:
        if ref.kind == "imports":  # `_static_refs` checked the alias
            return posixpath.join(IMPORTS_DIR, ref.name, *([ref.glob] if ref.glob else []))
        paths = [_staged_path(out, spec.action_id) for spec, out in self._select(ref, str(where))]
        return paths if ref.selector == "*" else paths[0]

    def _select(self, ref: Ref, where: str) -> list[tuple[ActionSpec, OutputSpec]]:
        assert ref.output is not None
        producer = self._planned[ref.name]  # planned earlier: the graph orders producers first
        instances = producer.instances
        if ref.selector is None:
            if producer.step.matrix is not None:
                raise PlanError(
                    f"{where}: {ref.text}: step {ref.name!r} has a matrix ({len(instances)} "
                    f"instances); use {ref.text[:-1]}[*]}} for all of them or "
                    f"{ref.text[:-1]}[column=value]}} to pick one"
                )
            chosen = [spec for _, spec in instances]
        elif ref.selector == "*":
            chosen = [spec for _, spec in instances]
        else:
            wanted = dict(ref.selector)
            chosen = [
                spec
                for inst, spec in instances
                if all(inst.row.get(k) == v for k, v in wanted.items())
            ]
            if len(chosen) != 1:
                ids = ", ".join(spec.action_id for _, spec in instances)
                found = "no instance" if not chosen else f"{len(chosen)} instances"
                raise PlanError(
                    f"{where}: {ref.text}: selector matches {found} of step {ref.name!r}; it must "
                    f"match exactly one. Instances: {ids}"
                )
        selected = []
        for spec in chosen:
            names = [o.name for o in spec.outputs]
            if ref.output not in names:
                raise PlanError(
                    f"{where}: {ref.text}: step {ref.name!r} has no output {ref.output!r}"
                    f"{_suggest(ref.output, names)}"
                )
            selected.append((spec, spec.output(ref.output)))
        return selected

    # -- actions ----------------------------------------------------------------------------

    def _action(
        self,
        name: str,
        step: StepDef,
        rule: RulePlugin,
        inst: StepInstance,
        toolchain: Toolchain | None,
        refs: list[_StaticRef],
    ) -> ActionSpec:
        where = f"steps.{name} ({inst.instance_id})" if step.matrix is not None else f"steps.{name}"
        try:
            template = rule.expand(step, ExpandContext(inst, self))
        except RuleError as exc:
            raise PlanError(f"{where}: {exc}") from exc
        roots = [str(r) for r in toolchain.install_roots] if toolchain is not None else []
        _check_argv(template.argv, roots, where)
        outputs = self._outputs(step, template, inst, where)
        config_files = self._config_files(step, template, inst, where)
        inputs = self._inputs(step, template, inst, refs, where)
        _check_overlaps(inputs, config_files, outputs, where)
        tc_ref = self._flow.toolchains[step.toolchain] if step.toolchain is not None else None
        spec = ActionSpec(
            action_id=inst.instance_id,
            step=name,
            rule=RuleRef(rule.kind, rule.version),
            argv=template.argv,
            params=dict(inst.params),
            env=dict(template.env),
            toolchain=None
            if toolchain is None
            else ToolchainRef(toolchain.name, toolchain.module, toolchain.id),
            inputs=inputs,
            outputs=outputs,
            config_files=config_files,
            resources=_merge_resources(inst.resources, tc_ref.resources if tc_ref else None, where),
            licenses=merge_licenses(tc_ref.licenses if tc_ref else {}, step.licenses),
            debug=self._debug(step, inst, where),
            domain=self._flow.domain,
            key=None,
            runtime_env=dict(template.runtime_env),
        )
        return with_key(spec)

    def _render_str(self, inst: StepInstance, text: str, field: str, where: str) -> str:
        value = inst.render(text, field=field, resolver=self)
        if isinstance(value, list):
            raise PlanError(
                f"{where}: {field} {text!r} expands to a list; a single value is needed"
            )
        return value

    def _outputs(
        self, step: StepDef, template: ActionTemplate, inst: StepInstance, where: str
    ) -> tuple[OutputSpec, ...]:
        clash = sorted(step.outputs.keys() & template.outputs.keys())
        if clash:
            raise PlanError(
                f"{where}: output {clash[0]!r} is declared in the flow and also generated by the "
                f"{step.kind!r} rule; rename the declared output"
            )
        specs = []
        defs: dict[str, OutputDef] = {**step.outputs, **template.outputs}
        for oname, odef in sorted(defs.items()):
            raw = odef.file if odef.file is not None else odef.dir
            assert raw is not None  # the model requires exactly one of file/dir
            path = _logical(
                self._render_str(inst, raw, f"outputs.{oname}", where), f"output {oname!r}", where
            )
            kind: OutputType = "file" if odef.file is not None else "dir"
            specs.append(OutputSpec(oname, path, kind, odef.deterministic, odef.optional))
        return tuple(specs)

    def _config_files(
        self, step: StepDef, template: ActionTemplate, inst: StepInstance, where: str
    ) -> dict[str, str]:
        files = {
            _logical(path, "generated config file", where): content
            for path, content in template.config_files.items()
        }
        for raw_path, raw_content in step.config_files.items():
            path = _logical(
                self._render_str(inst, raw_path, f"config_files[{raw_path!r}]", where),
                "config file",
                where,
            )
            if path in files:
                raise PlanError(
                    f"{where}: config file {path!r} is declared in the flow and also generated by "
                    f"the {step.kind!r} rule; choose another path"
                )
            files[path] = self._render_str(inst, raw_content, f"config_files[{raw_path!r}]", where)
        return dict(sorted(files.items()))

    def _debug(self, step: StepDef, inst: StepInstance, where: str) -> DebugSpec:
        if step.debug is None:
            return DebugSpec()
        collect: list[str] = []
        for i, text in enumerate(step.debug.collect):
            value = inst.render(text, field=f"debug.collect[{i}]", resolver=self)
            for pattern in value if isinstance(value, list) else [value]:
                collect.append(_logical(pattern, "debug pattern", where))
        max_size = step.debug.max_size
        if isinstance(max_size, str):
            try:
                max_size = parse_memory(self._render_str(inst, max_size, "debug.max_size", where))
            except ValueError as exc:
                raise PlanError(f"{where}: debug.max_size: {exc}") from None
        return DebugSpec(tuple(collect), max_size, step.debug.on_success)

    def _inputs(
        self,
        step: StepDef,
        template: ActionTemplate,
        inst: StepInstance,
        refs: list[_StaticRef],
        where: str,
    ) -> tuple[InputRef, ...]:
        clash = sorted(step.inputs.keys() & template.inputs.keys())
        if clash:
            raise PlanError(
                f"{where}: input {clash[0]!r} is declared in the flow and also generated by the "
                f"{step.kind!r} rule; rename the declared input"
            )
        found: list[InputRef] = []
        declared: list[tuple[str, str]] = []
        imported: list[tuple[str, str, str]] = []  # (input name, alias, rendered pattern)
        writable_names: set[str] = set()
        writable_refs: set[Ref] = set()  # output references staged through a writable input
        for iname, value in step.inputs.items():
            writable = isinstance(value, InputDef) and value.writable
            if writable:
                writable_names.add(iname)
            for field, text in input_texts(iname, value):
                parts = parse_template(text).parts
                if any(isinstance(p, Ref) and p.kind == "steps" for p in parts):
                    if len(parts) != 1:
                        raise PlanError(
                            f"{where}: {field} {text!r}: an output reference must be the whole "
                            "input value (outputs are staged as a unit)"
                        )
                    if writable:
                        writable_refs.update(p for p in parts if isinstance(p, Ref))
                    continue  # staged below, like every other output reference of the step
                imports = [p for p in parts if isinstance(p, Ref) and p.kind == "imports"]
                if imports:
                    if len(imports) > 1 or parts[0] is not imports[0]:
                        raise PlanError(
                            f"{where}: {field} {text!r}: an import reference must start the "
                            "input value: ${imports.A}, ${imports.A}/<output> or "
                            "${imports.A}/<output>/<glob>"
                        )
                    patterns = inst.render(text, field=field, resolver=self)
                    for pattern in patterns if isinstance(patterns, list) else [patterns]:
                        imported.append((iname, imports[0].name, pattern))
                    continue
                rendered = inst.render(text, field=field, resolver=self)
                for pattern in rendered if isinstance(rendered, list) else [rendered]:
                    declared.append((iname, pattern))
        declared.extend(template.inputs.items())  # rule inputs ("ebs.*") are never writable
        for iname, pattern in declared:
            pattern = _source_pattern(pattern, iname, where)
            writable = iname in writable_names
            for path, digest in self._snapshot(pattern):
                found.append(
                    InputRef(path, "file", SourceInput(iname, pattern), digest, writable=writable)
                )
        for iname, alias, pattern in imported:
            found.extend(
                self._import_inputs(alias, pattern, writable=iname in writable_names, where=where)
            )
        for static in refs:
            for spec, out in self._select(static.ref, f"steps.{inst.name}.{static.field}"):
                kind: InputKind = "tree" if out.type == "dir" else "file"
                source = ActionOutputInput(spec.action_id, out.name)
                found.append(
                    InputRef(
                        _staged_path(out, spec.action_id),
                        kind,
                        source,
                        _output_id(spec, out),
                        writable=static.ref in writable_refs,
                    )
                )
                self._edges.add((spec.action_id, out.name, inst.instance_id))
        unique: dict[str, InputRef] = {}
        for ref in found:
            seen = unique.setdefault(ref.logical_path, ref)
            if (seen.kind, seen.id, type(seen.source)) != (ref.kind, ref.id, type(ref.source)):
                raise PlanError(
                    f"{where}: two different inputs are staged at {ref.logical_path!r} "
                    f"({seen.source} and {ref.source}); rename one of them"
                )
            if ref.writable and not seen.writable:  # one writable declaration is enough
                unique[ref.logical_path] = dataclasses.replace(seen, writable=True)
        return tuple(unique[p] for p in sorted(unique))

    def _import_inputs(
        self, alias: str, pattern: str, *, writable: bool, where: str
    ) -> list[InputRef]:
        """The inputs one import pattern (`imports/<alias>[/…]`, rendered) stands for."""
        assert self._lock is not None  # `_check_imports` passed
        pin = self._lock.imports[alias]
        release = f"release {pin.name} {pin.version} (import {alias!r})"
        root = posixpath.join(IMPORTS_DIR, alias)
        rest = pattern[len(root) :]
        if not rest:
            return [self._whole(alias, pin, name, writable) for name in sorted(pin.outputs)]
        sub = rest[1:]
        norm = posixpath.normpath(sub) if sub else ""
        if not rest.startswith("/") or norm in ("", ".", "..") or norm.startswith(("../", "/")):
            raise PlanError(
                f"{where}: input pattern {pattern!r} leaves the release or names no file in it; "
                f"write ${{imports.{alias}}}/<output>[/<glob>] (outputs of {release}: "
                f"{', '.join(sorted(pin.outputs))})"
            )
        if norm in pin.outputs:
            return [self._whole(alias, pin, norm, writable)]
        # A pattern starting with an output's exact name globs inside that output only, so names
        # with glob characters (`compile[lib=a].worklib`) are literal there.
        scope = max((n for n in pin.outputs if norm.startswith(f"{n}/")), key=len, default=None)
        files: dict[str, tuple[Digest, Digest | None]] = {}  # path -> (id, content | symlink)
        for name, out in sorted(pin.outputs.items()):
            if scope is not None and name != scope:
                continue
            if out.type == "file":
                files[name] = (out.id, out.digest)
                continue
            try:
                tree = self._tree_files(out.digest)
            except CasError as exc:
                raise PlanError(
                    f"{where}: output {name!r} of {release} is not in the CAS of domain "
                    f"{self._flow.domain!r} ({exc}); a release of another domain must be "
                    f"copied here first: `ebs release export {pin.name} {pin.version} --to "
                    f"{self._flow.domain}`"
                ) from exc
            for path, digest in tree:
                files[f"{name}/{path}"] = (digest or out.id, digest)
        if scope is None:
            matched = match_paths(files, norm)
        else:
            inner = {p[len(scope) + 1 :]: p for p in files}
            matched = [inner[p] for p in match_paths(inner, norm[len(scope) + 1 :])]
        if not matched:
            first = norm.split("/", 1)[0]
            if scope is None and not any(c in first for c in "*?[") and first not in pin.outputs:
                raise PlanError(
                    f"{where}: input pattern {pattern!r}: {release} has no output {first!r}"
                    f"{_suggest(first, list(pin.outputs))}; outputs: "
                    f"{', '.join(sorted(pin.outputs))}"
                )
            raise PlanError(
                f"{where}: input pattern {pattern!r} matches nothing in {release}; fix the "
                "pattern (hidden files only match a pattern that names them)"
            )
        links = [p for p in matched if files[p][1] is None]
        if links:
            raise PlanError(
                f"{where}: input pattern {pattern!r} matches the symlink {links[0]!r} in "
                f"{release}; a symlink can only be staged with its whole tree: import the "
                "output itself or match the file it points to"
            )
        refs = []
        for path in matched:
            oid, content = files[path]
            assert content is not None  # symlinks were refused above
            source = ImportInput(alias, path, content)
            refs.append(
                InputRef(posixpath.join(root, path), "file", source, oid, writable=writable)
            )
        return refs

    def _whole(self, alias: str, pin: ImportPin, name: str, writable: bool) -> InputRef:
        out = pin.outputs[name]
        path = posixpath.join(IMPORTS_DIR, alias, name)
        return InputRef(path, out.type, ImportInput(alias, name, out.digest), out.id, writable)

    def _snapshot(self, pattern: str) -> list[tuple[str, Digest]]:
        files = self._snapshots.get(pattern)
        if files is None:
            result = self._p._sources.snapshot(self._base, pattern)
            files = []
            for path, digest in self._tree_files(result.digest):
                if digest is None:
                    raise PlanError(
                        f"source pattern {pattern!r} matches the symlink {path!r}; symlinks "
                        "cannot be staged as action inputs yet: declare the file it points to"
                    )
                files.append((path, digest))
            self._snapshots[pattern] = files
        return files

    def _tree_files(self, digest: Digest) -> list[tuple[str, Digest | None]]:
        """(path, file digest) below a stored tree; symlinks have no digest."""
        files = self._trees.get(digest)
        if files is None:
            files = []
            for entry in self._p._cas.get_tree(digest).entries:
                if entry.type == "dir":
                    assert entry.digest is not None
                    files.extend(
                        (f"{entry.name}/{p}", d) for p, d in self._tree_files(entry.digest)
                    )
                else:
                    files.append((entry.name, entry.digest))
            self._trees[digest] = files
        return files


def _logical(path: str, what: str, where: str) -> str:
    """Normalize a work-dir path; PlanError if it is absolute or leaves the work dir (R4)."""
    normalized = posixpath.normpath(path) if path else ""
    if path.startswith(("/", "~")) or normalized in ("", ".", "..") or normalized.startswith("../"):
        raise PlanError(
            f"{where}: {what} path {path!r} must be a relative path inside the action's work dir "
            "(no leading '/' or '~', no '..' leaving it); actions run in a scratch directory"
        )
    return normalized


def _source_pattern(pattern: str, name: str, where: str) -> str:
    normalized = posixpath.normpath(pattern) if pattern else ""
    if pattern.startswith(("/", "~")) or normalized in ("", "..") or normalized.startswith("../"):
        raise PlanError(
            f"{where}: input {name!r} pattern {pattern!r} must be relative to the flow directory "
            "and stay inside it; absolute paths into homes or project areas are not visible to "
            "actions (sandbox v1): copy or link the files into the repository, or import a release"
        )
    return pattern


def _check_argv(argv: Sequence[str], roots: Sequence[str], where: str) -> None:
    allowed = (*SYSTEM_PATHS, *roots)
    for i, element in enumerate(argv):
        text = _URL.sub("", element)  # remote URLs are not paths
        for token in (text, *_ARG_SPLIT.split(text)):
            candidate = _OPTION_PREFIX.sub("", token, count=1) if token.startswith("-") else token
            normalized = posixpath.normpath(candidate) if candidate else ""
            if candidate.startswith("~"):
                what = "the absolute path"
            elif candidate.startswith("/"):
                if any(_under(normalized, root) for root in allowed):
                    continue
                what = "the absolute path"
            elif normalized == ".." or normalized.startswith("../"):
                what = "a path leaving the work dir:"
            else:
                continue
            raise PlanError(
                f"{where}: argv[{i}] {element!r} names {what} {candidate!r}; actions run in "
                "a scratch directory with only their declared inputs, so use a path relative to "
                "the work dir and declare the file as an input (absolute paths are allowed only "
                f"under the toolchain's install roots and {', '.join(SYSTEM_PATHS)})"
            )


def _check_overlaps(
    inputs: Sequence[InputRef],
    config_files: Mapping[str, str],
    outputs: Sequence[OutputSpec],
    where: str,
) -> None:
    owners: dict[str, str] = {}
    entries = [(r.logical_path, "input") for r in inputs]
    entries += [(p, "config file") for p in config_files]
    entries += [(o.path, f"output {o.name!r}") for o in outputs]
    for path, what in entries:
        if path in owners:
            raise PlanError(f"{where}: {what} and {owners[path]} both use the path {path!r}")
        owners[path] = what
    for path, what in entries:
        parent = path
        while "/" in parent:
            parent = parent.rsplit("/", 1)[0]
            if parent in owners:
                raise PlanError(
                    f"{where}: {what} {path!r} lies inside {owners[parent]} {parent!r}; "
                    "paths in the work dir must not nest"
                )


def _merge_resources(step: Resources, toolchain: Resources | None, where: str) -> Resources:
    # Constraints add up (both are needed on the node); the other resources are defaults.
    values: dict[str, int | str | bool | None] = {
        "constraint": and_constraints(toolchain.constraint if toolchain else None, step.constraint)
    }
    for attr in ("cpus", "mem", "time", "max_parallel", "retry_on_timeout"):
        value = getattr(step, attr)
        if value is None and toolchain is not None:
            value = getattr(toolchain, attr)
        if isinstance(value, str):
            raise PlanError(
                f"{where}: toolchain default resources.{attr} {value!r} must be a literal; "
                "references are resolved per step, so set it on the step instead"
            )
        values[attr] = value
    return Resources.model_construct(_fields_set=set(values), **values)
