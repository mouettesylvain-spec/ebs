"""Invariant I3: every key-document field changes the key; no excluded field does (P0-08 R5).

Hypothesis builds random action specs, then mutates one field at a time.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Container
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ebs.core.digest import hash_bytes
from ebs.plan.keys import compute_key
from ebs.plan.types import (
    ActionSpec,
    DebugSpec,
    InputRef,
    OutputSpec,
    RuleRef,
    SourceInput,
    ToolchainRef,
)
from tests.helpers.plan import resources
from tests.helpers.plan_strategies import (
    ALPHABET,
    digests,
    names,
    param_values,
    paths,
    sources,
    specs,
    texts,
)

Mutation = Callable[[ActionSpec, st.DataObject], ActionSpec]


def _replace(spec: ActionSpec, **changes: Any) -> ActionSpec:
    return dataclasses.replace(spec, **changes)


def _fresh(existing: Container[str], data: st.DataObject, strategy: st.SearchStrategy[str]) -> str:
    return data.draw(strategy.filter(lambda v: v not in existing))


def _new_input(spec: ActionSpec, data: st.DataObject) -> ActionSpec:
    path = _fresh({i.logical_path for i in spec.inputs}, data, paths)
    ref = InputRef(path, "file", SourceInput("x", "x"), data.draw(digests))
    return _replace(spec, inputs=tuple(sorted((*spec.inputs, ref), key=lambda r: r.logical_path)))


def _input_id(spec: ActionSpec, data: st.DataObject) -> ActionSpec:
    if not spec.inputs:
        return _new_input(spec, data)
    first = spec.inputs[0]
    new = data.draw(digests.filter(lambda d: d != first.id))
    return _replace(spec, inputs=(dataclasses.replace(first, id=new), *spec.inputs[1:]))


def _input_path(spec: ActionSpec, data: st.DataObject) -> ActionSpec:
    if not spec.inputs:
        return _new_input(spec, data)
    path = _fresh({i.logical_path for i in spec.inputs}, data, paths)
    moved = dataclasses.replace(spec.inputs[0], logical_path=path)
    inputs = sorted((moved, *spec.inputs[1:]), key=lambda r: r.logical_path)
    return _replace(spec, inputs=tuple(inputs))


def _param(spec: ActionSpec, data: st.DataObject) -> ActionSpec:
    name = data.draw(names)
    old = spec.params.get(name, object())
    value = data.draw(param_values.filter(lambda v: v != old or type(v) is not type(old)))
    return _replace(spec, params={**spec.params, name: value})


def _param_type(spec: ActionSpec, data: st.DataObject) -> ActionSpec:
    number = data.draw(st.integers(0, 99))
    as_int = _replace(spec, params={**spec.params, "n": number})
    as_str = _replace(spec, params={**spec.params, "n": str(number)})
    # Return whichever differs from `spec`; the two differ from each other only by type.
    return as_str if spec.params.get("n") == number and type(spec.params["n"]) is int else as_int


def _env(spec: ActionSpec, data: st.DataObject) -> ActionSpec:
    name = data.draw(names.map(str.upper))
    value = data.draw(texts.filter(lambda v: v != spec.env.get(name)))
    return _replace(spec, env={**spec.env, name: value})


def _toolchain(spec: ActionSpec, data: st.DataObject) -> ActionSpec:
    if spec.toolchain is None:
        return _replace(spec, toolchain=ToolchainRef("t", "t/1", data.draw(digests)))
    old = spec.toolchain.id
    return _replace(
        spec,
        toolchain=dataclasses.replace(
            spec.toolchain, id=data.draw(digests.filter(lambda d: d != old))
        ),
    )


def _config_content(spec: ActionSpec, data: st.DataObject) -> ActionSpec:
    path = data.draw(paths)
    old = spec.config_files.get(path)
    content = data.draw(st.text(ALPHABET).filter(lambda c: c != old))
    return _replace(spec, config_files={**spec.config_files, path: content})


def _config_path(spec: ActionSpec, data: st.DataObject) -> ActionSpec:
    if not spec.config_files:
        return _config_content(spec, data)
    (_first, content), *rest = spec.config_files.items()
    path = _fresh(spec.config_files, data, paths)
    return _replace(spec, config_files={path: content, **dict(rest)})


def _output(spec: ActionSpec, data: st.DataObject, field: str) -> ActionSpec:
    if not spec.outputs:
        name = data.draw(names)
        return _replace(spec, outputs=(OutputSpec(name, "o", "file"),))
    first = spec.outputs[0]
    if field == "type":
        changed = dataclasses.replace(first, type="dir" if first.type == "file" else "file")
    elif field == "path":
        changed = dataclasses.replace(
            first, path=data.draw(paths.filter(lambda p: p != first.path))
        )
    else:
        changed = dataclasses.replace(
            first, name=_fresh({o.name for o in spec.outputs}, data, names)
        )
    outputs = sorted((changed, *spec.outputs[1:]), key=lambda o: o.name)
    return _replace(spec, outputs=tuple(outputs))


INCLUDED: dict[str, Mutation] = {
    "rule.kind": lambda s, d: _replace(s, rule=RuleRef(s.rule.kind + "x", s.rule.version)),
    "rule.version": lambda s, d: _replace(s, rule=RuleRef(s.rule.kind, s.rule.version + "1")),
    "argv.append": lambda s, d: _replace(s, argv=(*s.argv, d.draw(texts))),
    "argv.element": lambda s, d: _replace(s, argv=(s.argv[0] + "x", *s.argv[1:])),
    "params": _param,
    "params.type": _param_type,
    "env": _env,
    "toolchain": _toolchain,
    "config_files.content": _config_content,
    "config_files.path": _config_path,
    "inputs.id": _input_id,
    "inputs.path": _input_path,
    "inputs.added": _new_input,
    "outputs.path": lambda s, d: _output(s, d, "path"),
    "outputs.type": lambda s, d: _output(s, d, "type"),
    "outputs.name": lambda s, d: _output(s, d, "name"),
}


def _flip_outputs(spec: ActionSpec, data: st.DataObject) -> ActionSpec:
    flipped = tuple(
        dataclasses.replace(o, deterministic=not o.deterministic, optional=not o.optional)
        for o in spec.outputs
    )
    return _replace(spec, outputs=flipped)


def _input_sources(spec: ActionSpec, data: st.DataObject) -> ActionSpec:
    inputs = tuple(dataclasses.replace(i, source=data.draw(sources)) for i in spec.inputs)
    return _replace(spec, inputs=inputs)


EXCLUDED: dict[str, Mutation] = {
    "action_id": lambda s, d: _replace(s, action_id=s.action_id + "[x=1]"),
    "step": lambda s, d: _replace(s, step=s.step + "x"),
    "resources": lambda s, d: _replace(
        s, resources=resources(d.draw(st.integers(1, 64)), d.draw(st.integers(1, 1 << 40)), 7)
    ),
    "licenses": lambda s, d: _replace(s, licenses={**s.licenses, "feature": 9}),
    "debug": lambda s, d: _replace(s, debug=DebugSpec(("*.log",), 1 << 20, True)),
    "domain": lambda s, d: _replace(s, domain="other-nda"),
    "runtime_env": lambda s, d: _replace(s, runtime_env={"MAKEFLAGS": "-j8"}),
    "toolchain.name_module": lambda s, d: _replace(
        s,
        toolchain=None
        if s.toolchain is None
        else dataclasses.replace(s.toolchain, name="renamed", module="other/9"),
    ),
    "inputs.source": _input_sources,
    "outputs.deterministic_optional": _flip_outputs,
    "key": lambda s, d: _replace(s, key=hash_bytes(b"stale")),
}


# R5 (I3)
@pytest.mark.parametrize("field", sorted(INCLUDED))
@given(spec=specs(), data=st.data())
def test_included_fields_change_key(field: str, spec: ActionSpec, data: st.DataObject) -> None:
    mutated = INCLUDED[field](spec, data)
    assert compute_key(mutated) != compute_key(spec), field


# R5 (I3)
@pytest.mark.parametrize("field", sorted(EXCLUDED))
@given(spec=specs(), data=st.data())
def test_excluded_fields_do_not(field: str, spec: ActionSpec, data: st.DataObject) -> None:
    mutated = EXCLUDED[field](spec, data)
    assert compute_key(mutated) == compute_key(spec), field


@given(spec=specs())
def test_key_is_deterministic(spec: ActionSpec) -> None:
    rebuilt = dataclasses.replace(
        spec,
        params=dict(reversed(list(spec.params.items()))),
        env=dict(reversed(list(spec.env.items()))),
    )
    assert compute_key(rebuilt) == compute_key(spec)


@given(spec=specs())
def test_argv_order_matters(spec: ActionSpec) -> None:
    pq = dataclasses.replace(spec, argv=(*spec.argv, "p", "q"))
    qp = dataclasses.replace(spec, argv=(*spec.argv, "q", "p"))
    assert compute_key(pq) != compute_key(qp)
