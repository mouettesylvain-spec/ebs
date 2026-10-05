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
4. The key is computed once every input id is known (`ebs.plan.keys`).
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
from ebs.core.errors import PlanError, RuleError
from ebs.flow.interp import Ref, SourceLocation, parse_template
from ebs.flow.matrix import StepInstance, expand_matrix, load_matrix_tables
from ebs.flow.model import Flow, OutputDef, Resources, StepDef, parse_memory
from ebs.plan.graph import StepGraph
from ebs.plan.keys import KEY_SCHEMA_VERSION, nondeterministic_output_id, with_key
from ebs.plan.types import (
    ActionOutputInput,
    ActionSpec,
    DebugSpec,
    FlowInfo,
    InputKind,
    InputRef,
    OutputSpec,
    OutputType,
    Plan,
    PlanToolchain,
    RuleRef,
    SourceInput,
    ToolchainRef,
)
from ebs.rules.api import ActionTemplate, ExpandContext, RulePlugin
from ebs.rules.registry import RuleRegistry
from ebs.sources.snapshot import SnapshotResult
from ebs.toolchain.model import Toolchain, ToolchainResolver

__all__ = ["SYSTEM_PATHS", "Planner", "Snapshotter"]

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

IMPORTS_UNSUPPORTED = "imports require flow.lock support (task P2-03)"

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
    ) -> Plan:
        """Expand `flow` (whose tables and sources live under `base`) into a plan.

        `targets` restricts the plan to those steps and their transitive dependencies.
        `rehash` asserts that the snapshotter ignores the stat cache (CI/release builds).
        """
        if rehash and not self._sources.rehash:
            raise PlanError(
                "plan(rehash=True) needs a source snapshotter created with rehash=True, so that "
                "no source id comes from the stat cache"
            )
        return _Run(self, flow, base).plan(info or FlowInfo(), targets)

    def refine(self, plan: Plan, produced: Mapping[tuple[str, str], Digest]) -> Plan:
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

    def __init__(self, planner: Planner, flow: Flow, base: Path) -> None:
        self._p = planner
        self._flow = flow
        self._base = base
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
                if ref.kind == "imports":
                    raise PlanError(f"{where}: {ref.text}: {IMPORTS_UNSUPPORTED}")
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
                name: PlanToolchain(tc.module, tc.id)
                for name, tc in sorted(self._toolchains.items())
            },
            actions=tuple(actions),
            edges=tuple(sorted(self._edges)),
            lock=None,
            key_schema=KEY_SCHEMA_VERSION,
        )

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
        # Only `steps` refs reach here: `imports` refs were rejected by `_static_refs`.
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
            licenses={**(tc_ref.licenses if tc_ref else {}), **step.licenses},
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
        for iname, value in step.inputs.items():
            texts = (value,) if isinstance(value, str) else value
            for i, text in enumerate(texts):
                field = f"inputs.{iname}" if isinstance(value, str) else f"inputs.{iname}[{i}]"
                parts = parse_template(text).parts
                if any(isinstance(p, Ref) and p.kind == "steps" for p in parts):
                    if len(parts) != 1:
                        raise PlanError(
                            f"{where}: {field} {text!r}: an output reference must be the whole "
                            "input value (outputs are staged as a unit)"
                        )
                    continue  # staged below, like every other output reference of the step
                rendered = inst.render(text, field=field, resolver=self)
                for pattern in rendered if isinstance(rendered, list) else [rendered]:
                    declared.append((iname, pattern))
        declared.extend(template.inputs.items())
        for iname, pattern in declared:
            pattern = _source_pattern(pattern, iname, where)
            for path, digest in self._snapshot(pattern):
                found.append(InputRef(path, "file", SourceInput(iname, pattern), digest))
        for static in refs:
            for spec, out in self._select(static.ref, f"steps.{inst.name}.{static.field}"):
                kind: InputKind = "tree" if out.type == "dir" else "file"
                source = ActionOutputInput(spec.action_id, out.name)
                found.append(
                    InputRef(_staged_path(out, spec.action_id), kind, source, _output_id(spec, out))
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
        return tuple(unique[p] for p in sorted(unique))

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
    values: dict[str, int | None] = {}
    for attr in ("cpus", "mem", "time"):
        value = getattr(step, attr)
        if value is None and toolchain is not None:
            value = getattr(toolchain, attr)
        if isinstance(value, str):
            raise PlanError(
                f"{where}: toolchain default resources.{attr} {value!r} must be a literal; "
                "references are resolved per step, so set it on the step instead"
            )
        values[attr] = value
    return Resources.model_construct(_fields_set={"cpus", "mem", "time"}, **values)
