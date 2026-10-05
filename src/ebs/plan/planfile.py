"""plan.json: canonical JSON encoding of a `Plan` (docs/design/interfaces.md § 4).

`encode` produces canonical JSON (RFC 8785 subset, `ebs.core.canon`), so the bytes, and the
plan digest, depend only on the plan's content. `decode(encode(p)) == p` for every plan, and
`decode` validates the document field by field, naming the JSON path of anything malformed.

Action layout (one object per action, in plan order):

    {"action_id", "step", "rule": {"kind", "version"}, "argv": [...], "params": {...},
     "env": {...}, "runtime_env": {...}, "toolchain": {"name", "module", "id"} | null,
     "inputs": [{"path", "kind", "id", "source": {"type": "source", "input", "pattern"}
                                                | {"type": "output", "action", "output"}
                                                | {"type": "import", "name", "path"}}],
     "outputs": [{"name", "path", "type", "deterministic", "optional"}],
     "config_files": {...}, "resources": {"cpus", "mem", "time"}, "licenses": {...},
     "debug": {"collect", "max_size", "on_success"}, "domain", "key"}

Resources are integers (count, bytes, seconds) or null.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TypeVar

from ebs.core.canon import canonical_json
from ebs.core.digest import Digest, hash_bytes
from ebs.core.errors import DigestError, PlanError
from ebs.core.types import JsonValue
from ebs.flow.model import Resources
from ebs.plan.keys import KEY_SCHEMA_VERSION, with_key
from ebs.plan.types import (
    PLAN_FORMAT_VERSION,
    ActionOutputInput,
    ActionSpec,
    DebugSpec,
    FlowInfo,
    GitInfo,
    ImportInput,
    InputKind,
    InputRef,
    OutputSpec,
    OutputType,
    ParamValue,
    Plan,
    PlanToolchain,
    RuleRef,
    SourceInput,
    ToolchainRef,
)

__all__ = [
    "decode",
    "encode",
    "from_json",
    "plan_digest",
    "spec_from_json",
    "spec_to_json",
    "to_json",
]

T = TypeVar("T")


# --- encoding ----------------------------------------------------------------------------------


def _source_json(source: SourceInput | ActionOutputInput | ImportInput) -> JsonValue:
    if isinstance(source, SourceInput):
        return {"type": "source", "input": source.input, "pattern": source.pattern}
    if isinstance(source, ActionOutputInput):
        return {"type": "output", "action": source.action_id, "output": source.output}
    return {"type": "import", "name": source.name, "path": source.path}


def _opt(d: Digest | None) -> JsonValue:
    return None if d is None else str(d)


def spec_to_json(spec: ActionSpec) -> JsonValue:
    """One action as a JSON object (the plan.json layout in the module docstring)."""
    res = spec.resources
    return {
        "action_id": spec.action_id,
        "step": spec.step,
        "rule": {"kind": spec.rule.kind, "version": spec.rule.version},
        "argv": list(spec.argv),
        "params": {k: list(v) if isinstance(v, tuple) else v for k, v in spec.params.items()},
        "env": dict(spec.env),
        "runtime_env": dict(spec.runtime_env),
        "toolchain": None
        if spec.toolchain is None
        else {
            "name": spec.toolchain.name,
            "module": spec.toolchain.module,
            "id": str(spec.toolchain.id),
        },
        "inputs": [
            {
                "path": r.logical_path,
                "kind": r.kind,
                "source": _source_json(r.source),
                "id": _opt(r.id),
            }
            for r in spec.inputs
        ],
        "outputs": [
            {
                "name": o.name,
                "path": o.path,
                "type": o.type,
                "deterministic": o.deterministic,
                "optional": o.optional,
            }
            for o in spec.outputs
        ],
        "config_files": dict(spec.config_files),
        "resources": {"cpus": res.cpus, "mem": res.mem, "time": res.time},
        "licenses": dict(spec.licenses),
        "debug": {
            "collect": list(spec.debug.collect),
            "max_size": spec.debug.max_size,
            "on_success": spec.debug.on_success,
        },
        "domain": spec.domain,
        "key": _opt(spec.key),
    }


def to_json(plan: Plan) -> JsonValue:
    git = plan.flow.git
    return {
        "v": plan.v,
        "ebs_version": plan.ebs_version,
        "key_schema": plan.key_schema,
        "flow": {
            "path": plan.flow.path,
            "git": None
            if git is None
            else {"repo": git.repo, "commit": git.commit, "dirty": git.dirty},
        },
        "lock": _opt(plan.lock),
        "domain": plan.domain,
        "project": plan.project,
        "toolchains": {
            name: {"module": tc.module, "id": str(tc.id)} for name, tc in plan.toolchains.items()
        },
        "actions": [spec_to_json(spec) for spec in plan.actions],
        "edges": [list(edge) for edge in plan.edges],
    }


def encode(plan: Plan) -> bytes:
    """Canonical plan.json bytes."""
    return canonical_json(to_json(plan))


def plan_digest(plan: Plan) -> Digest:
    """The build's plan digest: the digest of `encode(plan)`."""
    return hash_bytes(encode(plan))


# --- decoding ----------------------------------------------------------------------------------


def _fail(path: str, expected: str, value: object) -> PlanError:
    shown = json.dumps(value)[:80] if not isinstance(value, _Missing) else "nothing"
    return PlanError(f"invalid plan.json: {path} must be {expected}, got {shown}")


class _Missing:
    pass


_MISSING = _Missing()


def _obj(value: object, path: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise _fail(path, "an object", value)
    return value


def _get(doc: dict[str, JsonValue], name: str, path: str, check: Callable[[object, str], T]) -> T:
    return check(doc.get(name, _MISSING), f"{path}.{name}")


def _str(value: object, path: str) -> str:
    if not isinstance(value, str):
        raise _fail(path, "a string", value)
    return value


def _bool(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise _fail(path, "true or false", value)
    return value


def _int(value: object, path: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise _fail(path, "an integer", value)
    return value


def _opt_int(value: object, path: str) -> int | None:
    return None if value is None else _int(value, path)


def _digest(value: object, path: str) -> Digest:
    try:
        return Digest.parse(_str(value, path))
    except DigestError:
        raise _fail(path, "a digest like 'sha256:<64 hex>'", value) from None


def _opt_digest(value: object, path: str) -> Digest | None:
    return None if value is None else _digest(value, path)


def _list(value: object, path: str) -> list[JsonValue]:
    if not isinstance(value, list):
        raise _fail(path, "an array", value)
    return value


def _strs(value: object, path: str) -> tuple[str, ...]:
    return tuple(_str(v, f"{path}[{i}]") for i, v in enumerate(_list(value, path)))


def _str_map(value: object, path: str) -> dict[str, str]:
    return {k: _str(v, f"{path}.{k}") for k, v in _obj(value, path).items()}


def _int_map(value: object, path: str) -> dict[str, int]:
    return {k: _int(v, f"{path}.{k}") for k, v in _obj(value, path).items()}


def _param(value: object, path: str) -> ParamValue:
    if isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, list):
        return _strs(value, path)
    raise _fail(path, "a string, integer, boolean or array of strings", value)


def _source(value: object, path: str) -> SourceInput | ActionOutputInput | ImportInput:
    doc = _obj(value, path)
    kind = doc.get("type")
    if kind == "source":
        return SourceInput(_get(doc, "input", path, _str), _get(doc, "pattern", path, _str))
    if kind == "output":
        return ActionOutputInput(_get(doc, "action", path, _str), _get(doc, "output", path, _str))
    if kind == "import":
        return ImportInput(_get(doc, "name", path, _str), _get(doc, "path", path, _str))
    raise _fail(f"{path}.type", "'source', 'output' or 'import'", kind)


_INPUT_KINDS: dict[str, InputKind] = {"file": "file", "tree": "tree"}
_OUTPUT_TYPES: dict[str, OutputType] = {"file": "file", "dir": "dir"}


def _input(value: object, path: str) -> InputRef:
    doc = _obj(value, path)
    raw = doc.get("kind")
    kind = _INPUT_KINDS.get(raw) if isinstance(raw, str) else None
    if kind is None:
        raise _fail(f"{path}.kind", "'file' or 'tree'", raw)
    return InputRef(
        logical_path=_get(doc, "path", path, _str),
        kind=kind,
        source=_get(doc, "source", path, _source),
        id=_get(doc, "id", path, _opt_digest),
    )


def _output(value: object, path: str) -> OutputSpec:
    doc = _obj(value, path)
    raw = doc.get("type")
    kind = _OUTPUT_TYPES.get(raw) if isinstance(raw, str) else None
    if kind is None:
        raise _fail(f"{path}.type", "'file' or 'dir'", raw)
    return OutputSpec(
        name=_get(doc, "name", path, _str),
        path=_get(doc, "path", path, _str),
        type=kind,
        deterministic=_get(doc, "deterministic", path, _bool),
        optional=_get(doc, "optional", path, _bool),
    )


def _toolchain(value: object, path: str) -> ToolchainRef | None:
    if value is None:
        return None
    doc = _obj(value, path)
    return ToolchainRef(
        _get(doc, "name", path, _str),
        _get(doc, "module", path, _str),
        _get(doc, "id", path, _digest),
    )


def _resources(value: object, path: str) -> Resources:
    doc = _obj(value, path)
    return Resources.model_construct(
        _fields_set={"cpus", "mem", "time"},
        cpus=_get(doc, "cpus", path, _opt_int),
        mem=_get(doc, "mem", path, _opt_int),
        time=_get(doc, "time", path, _opt_int),
    )


def _debug(value: object, path: str) -> DebugSpec:
    doc = _obj(value, path)
    return DebugSpec(
        collect=_get(doc, "collect", path, _strs),
        max_size=_get(doc, "max_size", path, _opt_int),
        on_success=_get(doc, "on_success", path, _bool),
    )


def _spec(value: object, path: str) -> ActionSpec:
    spec = _spec_fields(value, path)
    if spec.key is not None and with_key(spec).key != spec.key:
        raise PlanError(
            f"invalid plan.json: {path}.key does not match the action's fields (the plan was "
            "edited or written by another key schema); re-plan the flow"
        )
    return spec


def _spec_fields(value: object, path: str) -> ActionSpec:
    doc = _obj(value, path)
    rule = _get(doc, "rule", path, _obj)
    params = _get(doc, "params", path, _obj)
    return ActionSpec(
        action_id=_get(doc, "action_id", path, _str),
        step=_get(doc, "step", path, _str),
        rule=RuleRef(
            _get(rule, "kind", f"{path}.rule", _str), _get(rule, "version", f"{path}.rule", _str)
        ),
        argv=_get(doc, "argv", path, _strs),
        params={k: _param(v, f"{path}.params.{k}") for k, v in params.items()},
        env=_get(doc, "env", path, _str_map),
        toolchain=_get(doc, "toolchain", path, _toolchain),
        inputs=tuple(
            _input(v, f"{path}.inputs[{i}]") for i, v in enumerate(_get(doc, "inputs", path, _list))
        ),
        outputs=tuple(
            _output(v, f"{path}.outputs[{i}]")
            for i, v in enumerate(_get(doc, "outputs", path, _list))
        ),
        config_files=_get(doc, "config_files", path, _str_map),
        resources=_get(doc, "resources", path, _resources),
        licenses=_get(doc, "licenses", path, _int_map),
        debug=_get(doc, "debug", path, _debug),
        domain=_get(doc, "domain", path, _str),
        key=_get(doc, "key", path, _opt_digest),
        runtime_env=_get(doc, "runtime_env", path, _str_map),
    )


def spec_from_json(doc: JsonValue) -> ActionSpec:
    """Inverse of `spec_to_json`; PlanError naming the JSON path of a malformed field."""
    return _spec(doc, "action")


def _edge(value: object, path: str) -> tuple[str, str, str]:
    items = _strs(value, path)
    if len(items) != 3:
        raise _fail(path, "[producer, output, consumer]", value)
    return items[0], items[1], items[2]


def _flow(value: object, path: str) -> FlowInfo:
    doc = _obj(value, path)
    git_doc = doc.get("git")
    git = None
    if git_doc is not None:
        g = _obj(git_doc, f"{path}.git")
        git = GitInfo(
            _get(g, "repo", f"{path}.git", _str),
            _get(g, "commit", f"{path}.git", _str),
            _get(g, "dirty", f"{path}.git", _bool),
        )
    return FlowInfo(_get(doc, "path", path, _str), git)


def from_json(doc: JsonValue) -> Plan:
    """Inverse of `to_json`; PlanError for another format or key schema, or malformed fields."""
    root = _obj(doc, "$")
    version = root.get("v")
    if version != PLAN_FORMAT_VERSION:
        raise PlanError(
            f"unsupported plan format version {json.dumps(version)} (this ebs reads plan format "
            f"version {PLAN_FORMAT_VERSION}); re-plan the flow with this ebs version"
        )
    schema = root.get("key_schema")
    if schema != KEY_SCHEMA_VERSION:
        raise PlanError(
            f"plan.json uses key schema {json.dumps(schema)} but this ebs computes keys with "
            f"schema {KEY_SCHEMA_VERSION}; re-plan the flow so keys match the cache"
        )
    toolchains = {
        name: PlanToolchain(
            _get(_obj(tc, f"$.toolchains.{name}"), "module", f"$.toolchains.{name}", _str),
            _get(_obj(tc, f"$.toolchains.{name}"), "id", f"$.toolchains.{name}", _digest),
        )
        for name, tc in _get(root, "toolchains", "$", _obj).items()
    }
    return Plan(
        ebs_version=_get(root, "ebs_version", "$", _str),
        domain=_get(root, "domain", "$", _str),
        project=_get(root, "project", "$", _str),
        flow=_get(root, "flow", "$", _flow),
        toolchains=toolchains,
        actions=tuple(
            _spec(v, f"$.actions[{i}]") for i, v in enumerate(_get(root, "actions", "$", _list))
        ),
        edges=tuple(
            _edge(v, f"$.edges[{i}]") for i, v in enumerate(_get(root, "edges", "$", _list))
        ),
        lock=_get(root, "lock", "$", _opt_digest),
        key_schema=schema,
        v=version,
    )


def decode(data: bytes) -> Plan:
    """Parse plan.json bytes; PlanError if they are not a valid plan."""
    try:
        doc = json.loads(data)
    except (ValueError, UnicodeDecodeError) as exc:
        raise PlanError(f"plan.json is not valid JSON: {exc}") from exc
    return from_json(doc)
