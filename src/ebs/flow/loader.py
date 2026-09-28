"""Load `flow.yaml` into a validated `Flow` with `file:line:col` errors (task P0-04).

The YAML is only *composed* by ruamel (parsed into a node graph); this module builds plain Python
data from the nodes itself, so no YAML tag can ever construct an object. While doing so it
enforces the flow dialect of YAML:

- only the core-schema tags (`!!str`, `!!int`, `!!bool`, `!!null`, `!!map`, `!!seq`) and merge
  keys (`<<`) are accepted; floats and unquoted timestamps are rejected with a hint to quote them;
- duplicate keys are errors (ruamel's own check only runs in its constructor, which is not used);
- mapping keys must be strings; anchors and aliases work, recursive aliases are rejected.

It records the position of every key and value, and maps Pydantic validation errors back to them.
"""

from __future__ import annotations

import difflib
import re
import types
import typing
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Union

from pydantic import BaseModel, ValidationError
from pydantic_core import ErrorDetails
from ruamel.yaml import YAML
from ruamel.yaml.error import MarkedYAMLError, YAMLError
from ruamel.yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from ebs.core.errors import FlowError
from ebs.flow.model import Flow

__all__ = ["load_flow"]

_YAML_NS = "tag:yaml.org,2002:"
_STR, _INT, _BOOL, _NULL = (_YAML_NS + t for t in ("str", "int", "bool", "null"))
_FLOAT, _TIMESTAMP, _MERGE = (_YAML_NS + t for t in ("float", "timestamp", "merge"))
_MAP, _SEQ = _YAML_NS + "map", _YAML_NS + "seq"
_YAML_DIRECTIVE_RE = re.compile(r"%YAML[ \t]+(\S+)")

MAX_NODES = 200_000
"""Upper bound on YAML nodes built, counting each alias expansion (real flows are far smaller)."""

DataPath = tuple[str | int, ...]
Position = tuple[int, int]  # 1-based line, column


@dataclass
class _Positions:
    values: dict[DataPath, Position] = field(default_factory=dict)
    keys: dict[DataPath, Position] = field(default_factory=dict)


def load_flow(path: Path, *, lock: Path | None = None) -> Flow:
    """Load and validate `path`; raise `FlowError` pointing at the offending YAML node.

    `lock` (the `flow.lock` next to the flow) is reserved for imports (task P2-03) and is not
    read yet.
    """
    del lock
    file = str(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise FlowError(
            f"cannot read flow file: {exc}; check the path and that it is UTF-8", file=file
        ) from exc
    root = _compose(text, file)
    builder = _Builder(file)
    data = builder.build(root, ())
    if not isinstance(data, dict):
        raise FlowError(
            "a flow file must be a mapping with 'version', 'project', 'domain' and 'steps'",
            file=file,
            line=root.start_mark.line + 1,
            col=root.start_mark.column + 1,
        )
    try:
        return Flow.model_validate(data)
    except ValidationError as exc:
        raise _flow_error(exc, data, builder.positions, file) from None


# --- YAML -> plain data -----------------------------------------------------------------------


def _mark(node: Node) -> Position:
    return node.start_mark.line + 1, node.start_mark.column + 1


def _check_directives(text: str, file: str) -> None:
    # A `%YAML 1.1` directive would switch the resolver to YAML 1.1, where `yes`/`no`/`on`
    # are booleans; flows are always read as YAML 1.2.
    for number, line in enumerate(text.splitlines(), start=1):
        m = _YAML_DIRECTIVE_RE.match(line)
        if m is not None and m[1] != "1.2":
            raise FlowError(
                f"unsupported '%YAML {m[1]}' directive: flows are YAML 1.2; remove the directive",
                file=file,
                line=number,
                col=1,
            )


def _compose(text: str, file: str) -> Node:
    _check_directives(text, file)
    yaml = YAML(typ="safe", pure=True)
    try:
        documents: list[Node] = list(yaml.compose_all(text))
    except MarkedYAMLError as exc:
        mark = exc.problem_mark or exc.context_mark
        context = exc.context
        if context and exc.context_mark is not None and mark is not exc.context_mark:
            where = exc.context_mark
            context += f" (started at {where.line + 1}:{where.column + 1})"
        detail = "; ".join(str(p) for p in (context, exc.problem) if p)
        raise FlowError(
            f"YAML syntax error: {detail}",
            file=file,
            line=mark.line + 1 if mark is not None else None,
            col=mark.column + 1 if mark is not None else None,
        ) from None
    except YAMLError as exc:  # pragma: no cover - ruamel raises marked errors for bad input
        raise FlowError(f"YAML error: {exc}", file=file) from None
    documents = [doc for doc in documents if doc is not None]
    if not documents:
        raise FlowError(
            "flow file is empty: expected a mapping with 'version: 1', 'project' and 'domain'",
            file=file,
            line=1,
            col=1,
        )
    if len(documents) > 1:
        line, col = _mark(documents[1])
        raise FlowError(
            "a flow file must contain a single YAML document; remove the '---' separator",
            file=file,
            line=line,
            col=col,
        )
    return documents[0]


def _display_tag(tag: str | None) -> str:
    if tag is None:  # pragma: no cover - the composer always resolves a tag
        return "?"
    if tag.startswith(_YAML_NS):
        return "!!" + tag.removeprefix(_YAML_NS)
    return tag


def _dotted(path: Sequence[str | int]) -> str:
    return ".".join(str(p) for p in path)


class _Builder:
    def __init__(self, file: str) -> None:
        self.file = file
        self.positions = _Positions()
        self._active: set[int] = set()
        self._count = 0

    def _error(self, message: str, node: Node, path: DataPath) -> FlowError:
        line, col = _mark(node)
        prefix = f"{_dotted(path)}: " if path else ""
        return FlowError(prefix + message, file=self.file, line=line, col=col)

    def build(self, node: Node, path: DataPath) -> Any:
        self._count += 1
        if self._count > MAX_NODES:  # aliases expand in full: bound "billion laughs" inputs
            raise self._error(
                f"flow expands to more than {MAX_NODES} YAML nodes through aliases; "
                "move large data into parameter tables",
                node,
                path,
            )
        self.positions.values.setdefault(path, _mark(node))
        if isinstance(node, ScalarNode):
            return self._scalar(node, path)
        if id(node) in self._active:
            raise self._error("recursive YAML alias: an anchor may not contain itself", node, path)
        self._active.add(id(node))
        try:
            if isinstance(node, SequenceNode):
                self._check_tag(node, path, _SEQ)
                return [self.build(item, (*path, i)) for i, item in enumerate(node.value)]
            assert isinstance(node, MappingNode)  # the composer makes only these 3 node kinds
            self._check_tag(node, path, _MAP)
            return self._mapping(node, path)
        finally:
            self._active.discard(id(node))

    def _check_tag(self, node: Node, path: DataPath, expected: str) -> None:
        if node.tag != expected:
            raise self._unsafe_tag(node, path)

    def _unsafe_tag(self, node: Node, path: DataPath) -> FlowError:
        return self._error(
            f"YAML tag {_display_tag(node.tag)!r} is not allowed; flows only use plain strings, "
            "integers, booleans, null, mappings, lists, anchors and '<<' merge keys",
            node,
            path,
        )

    def _scalar(self, node: ScalarNode, path: DataPath) -> Any:
        tag, value = node.tag, str(node.value)
        if tag == _STR:
            return value
        if tag == _BOOL:
            return value.lower() == "true"
        if tag == _NULL:
            return None
        if tag == _INT:
            try:
                return _parse_int(value)
            except ValueError:
                raise self._error(f"invalid integer {value!r}", node, path) from None
        if tag == _FLOAT:
            raise self._error(
                f"floats are not allowed ({value}); quote the value to pass it as a string",
                node,
                path,
            )
        if tag == _TIMESTAMP:
            raise self._error(
                f"unquoted date/time {value!r} is not allowed; quote it to pass it as a string",
                node,
                path,
            )
        raise self._unsafe_tag(node, path)

    def _mapping(self, node: MappingNode, path: DataPath) -> dict[str, Any]:
        # Positions are recorded first-writer-wins, so explicit keys are built before merge
        # sources, and earlier sources before later ones: the recorded node is the one whose
        # value ends up in the data.
        own: dict[str, Any] = {}
        first_seen: dict[str, Node] = {}
        merge_key: Node | None = None
        merge_value: Node | None = None
        for key_node, value_node in node.value:
            if isinstance(key_node, ScalarNode) and key_node.tag == _MERGE:
                if merge_key is not None:
                    line, _ = _mark(merge_key)
                    raise self._error(
                        f"duplicate '<<' merge key (first defined on line {line}); "
                        "merge several mappings with '<<: [*a, *b]'",
                        key_node,
                        path,
                    )
                merge_key, merge_value = key_node, value_node
                continue
            key = self._key(key_node, path)
            if key in first_seen:
                line, _ = _mark(first_seen[key])
                raise self._error(
                    f"duplicate key {key!r} (first defined on line {line}); "
                    "YAML keys must be unique within a mapping",
                    key_node,
                    path,
                )
            first_seen[key] = key_node
            self.positions.keys.setdefault((*path, key), _mark(key_node))
            self.positions.values.setdefault((*path, key), _mark(value_node))
            own[key] = self.build(value_node, (*path, key))
        merged = [] if merge_value is None else self._merge_sources(merge_value, path)
        # Merge semantics (yaml.org/type/merge): explicit keys win, then earlier sources.
        result: dict[str, Any] = {}
        for source in reversed(merged):
            result.update(source)
        result.update(own)
        return result

    def _merge_sources(self, node: Node, path: DataPath) -> list[dict[str, Any]]:
        nodes = node.value if isinstance(node, SequenceNode) else [node]
        sources: list[dict[str, Any]] = []
        for source in nodes:
            if not isinstance(source, MappingNode):
                raise self._error(
                    "a '<<' merge key needs a mapping or a list of mappings", source, path
                )
            built = self.build(source, path)
            assert isinstance(built, dict)
            sources.append(built)
        return sources

    def _key(self, node: Node, path: DataPath) -> str:
        if isinstance(node, ScalarNode):
            key = self._scalar(node, path)
            if isinstance(key, str):
                return key
            kind = "null" if key is None else type(key).__name__
            raise self._error(f"mapping keys must be strings, got {kind} {node.value}", node, path)
        raise self._error("mapping keys must be strings, got a collection", node, path)


def _parse_int(text: str) -> int:
    body = text.replace("_", "")
    sign = -1 if body.startswith("-") else 1
    body = body.lstrip("+-")
    if body[:2] in ("0x", "0X"):
        return sign * int(body[2:], 16)
    if body[:2] in ("0o", "0O"):
        return sign * int(body[2:], 8)
    if body[:2] in ("0b", "0B"):
        return sign * int(body[2:], 2)
    return sign * int(body, 10)


# --- validation errors -> FlowError -----------------------------------------------------------


@dataclass(frozen=True, order=True)
class _Located:
    line: int
    col: int
    text: str


def _flow_error(
    exc: ValidationError, data: dict[str, Any], positions: _Positions, file: str
) -> FlowError:
    located: dict[str, _Located] = {}
    for error in exc.errors():
        item = _locate(error, data, positions)
        located.setdefault(item.text.split(": ", 1)[0], item)  # one error per field path
    ordered = sorted(located.values())
    first, rest = ordered[0], ordered[1:]
    message = first.text + "".join(f"\n{e.line}:{e.col}: {e.text}" for e in rest)
    return FlowError(message, file=file, line=first.line, col=first.col)


def _locate(error: ErrorDetails, data: dict[str, Any], positions: _Positions) -> _Located:
    ctx = error.get("ctx") or {}
    loc: Sequence[str | int] = ctx.get("path") or error["loc"]
    kind = error["type"]
    path, missing_or_extra, is_key = _data_path(loc, data, kind)

    if kind == "missing":
        shown = [*path, missing_or_extra] if missing_or_extra is not None else path
        text = f"{_dotted(shown)}: missing required field"
        pos = positions.values.get(path, (1, 1))
    elif kind == "extra_forbidden":
        text = f"{_dotted(path)}: {_unknown_key(path)}"
        pos = positions.keys.get(path) or positions.values.get(path, (1, 1))
    else:
        if "detail" in ctx:
            message = str(ctx["detail"])
        elif kind in _EXPECTED:
            message = f"expected {_EXPECTED[kind]}, got {_describe(error['input'])}"
        else:
            message = error["msg"]
        text = f"{_dotted(path)}: {message}" if path else message
        # Every node the builder visits records its position, so these lookups succeed.
        pos = (positions.keys if is_key else positions.values).get(path, (1, 1))
    return _Located(pos[0], pos[1], text)


# Pydantic type errors, phrased in YAML terms (users write lists and mappings, not tuples/dicts).
_EXPECTED = {
    "string_type": "a string",
    "int_type": "an integer",
    "bool_type": "a boolean (true or false)",
    "dict_type": "a mapping",
    "model_type": "a mapping",
    "list_type": "a list",
    "tuple_type": "a list",
}


def _describe(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return f"boolean {str(value).lower()}"
    if isinstance(value, int):
        return f"integer {value}"
    if isinstance(value, str):
        return f"string {value!r}"
    return "a list" if isinstance(value, list) else "a mapping"


def _data_path(loc: Sequence[str | int], data: Any, kind: str) -> tuple[DataPath, str | None, bool]:
    """Keep the `loc` parts that address YAML nodes, dropping Pydantic union/validator tags.

    Returns the data path, the missing field name (for `missing` errors) and whether the error
    is about a mapping key rather than its value.
    """
    path: list[str | int] = []
    current = data
    for index, part in enumerate(loc):
        if part == "[key]":
            return tuple(path), None, True
        in_dict = isinstance(current, dict) and isinstance(part, str) and part in current
        in_list = isinstance(current, list) and type(part) is int and 0 <= part < len(current)
        if in_dict or in_list:
            current = current[part]
            path.append(part)
        elif kind == "missing" and index == len(loc) - 1 and isinstance(part, str):
            return tuple(path), part, False
    return tuple(path), None, False


def _unknown_key(path: DataPath) -> str:
    key = str(path[-1])
    allowed = _allowed_keys(path[:-1])
    message = f"unknown key {key!r}"
    if not allowed:
        return message
    close = difflib.get_close_matches(key, allowed, n=1, cutoff=0.75)
    if close:
        return f"{message}; did you mean {close[0]!r}?"
    return f"{message}; allowed keys: {', '.join(allowed)}"


def _allowed_keys(path: DataPath) -> list[str]:
    """Field names (YAML spelling) of the model found at `path`, walking the model annotations."""
    tp: Any = Flow
    for part in path:
        model = _model_in(tp)
        if model is not None:
            fields = {(f.alias or name): f.annotation for name, f in model.model_fields.items()}
            if not isinstance(part, str) or part not in fields:
                return []
            tp = fields[part]
        else:
            tp = _item_type(tp)
            if tp is None:
                return []
    model = _model_in(tp)
    if model is None:
        return []
    return [(f.alias or name) for name, f in model.model_fields.items()]


def _members(tp: Any) -> list[Any]:
    origin = typing.get_origin(tp)
    if origin is Annotated:
        return _members(typing.get_args(tp)[0])
    if origin in (Union, types.UnionType):
        return [m for arg in typing.get_args(tp) for m in _members(arg)]
    return [tp]


def _model_in(tp: Any) -> type[BaseModel] | None:
    for member in _members(tp):
        if isinstance(member, type) and issubclass(member, BaseModel):
            return member
    return None


def _item_type(tp: Any) -> Any:
    for member in _members(tp):
        origin = typing.get_origin(member)
        if origin is not None and issubclass(origin, Mapping):
            return typing.get_args(member)[1]
        if origin in (tuple, list):
            return typing.get_args(member)[0]
    return None
