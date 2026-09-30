"""Parameter tables: CSV (RFC 4180) and YAML lists of flat maps (task P0-05).

Every value stays a string exactly as written: no int, float or boolean guessing (`007` and `1.10`
survive). The table digest is the digest of the file bytes, recorded later in plan provenance.

CSV dialect: UTF-8 (a leading BOM is dropped), comma separator, `"` quoting with `""` escapes,
`\\n`, `\\r\\n` or `\\r` line ends, a header row. Outside quoted values, lines starting with `#`
and blank (or whitespace-only) lines are ignored.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from ruamel.yaml import YAML
from ruamel.yaml.error import MarkedYAMLError, YAMLError
from ruamel.yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from ebs.core.digest import Digest, hash_bytes
from ebs.core.errors import FlowError

__all__ = ["Table", "load_table"]

_YAML_NS = "tag:yaml.org,2002:"
_SCALAR_TAGS = frozenset(_YAML_NS + t for t in ("str", "int", "float", "bool", "timestamp"))
_NULL_TAG = _YAML_NS + "null"
_STR_TAG = _YAML_NS + "str"
_NEWLINE_RE = re.compile(r"\r\n|\r|\n")


@dataclass(frozen=True)
class Table:
    """A loaded table. Rows are read-only mappings with keys in `columns` order.

    `lines` holds the 1-based source line where each row starts (for error messages).
    """

    columns: tuple[str, ...]
    rows: tuple[Mapping[str, str], ...]
    source: Path
    digest: Digest
    lines: tuple[int, ...] = ()

    def origin(self, index: int) -> str:
        """`name.csv:12`: where row `index` (0-based) comes from."""
        if index < len(self.lines):
            return f"{self.source.name}:{self.lines[index]}"
        return f"{self.source.name} row {index + 1}"


def load_table(path: Path) -> Table:
    """Load a `.csv` or `.yaml`/`.yml` table; raises FlowError with file and line on bad input."""
    file = str(path)
    suffix = path.suffix.lower()
    if suffix not in (".csv", ".yaml", ".yml"):
        raise FlowError(
            f"unsupported table format {path.suffix!r}: use a .csv file (header row) or a "
            ".yaml/.yml file (list of maps)",
            file=file,
        )
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise FlowError(f"cannot read table: {exc.strerror or exc}", file=file) from None
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        line = data.count(b"\n", 0, exc.start) + 1
        raise FlowError(
            f"table is not valid UTF-8 (byte offset {exc.start}); save it as UTF-8",
            file=file,
            line=line,
        ) from None
    if suffix == ".csv":
        columns, rows, lines = _parse_csv(text, file)
    else:
        columns, rows, lines = _parse_yaml(text, file)
    return Table(
        columns=columns,
        rows=tuple(MappingProxyType(r) for r in rows),
        source=path,
        digest=hash_bytes(data),
        lines=lines,
    )


_Parsed = tuple[tuple[str, ...], list[dict[str, str]], tuple[int, ...]]


# --- CSV -----------------------------------------------------------------------------------------


def _csv_records(text: str, file: str) -> Iterator[tuple[int, list[str]]]:
    """Yield `(start line, fields)` per record, skipping comment and blank lines."""
    i, n, line = 0, len(text), 1
    while i < n:
        m = _NEWLINE_RE.search(text, i)
        eol, nxt = (m.start(), m.end()) if m else (n, n)
        if text.startswith("#", i) or not text[i:eol].strip():
            i, line = nxt, line + 1
            continue
        start, fields = line, []
        while True:
            if text.startswith('"', i):
                value: list[str] = []
                i += 1
                while True:
                    q = text.find('"', i)
                    if q < 0:
                        raise FlowError(
                            f"unterminated quoted value starting on line {start}: "
                            'close it with \'"\' (write "" for a quote inside a value)',
                            file=file,
                            line=start,
                        )
                    chunk = text[i:q]
                    line += len(_NEWLINE_RE.findall(chunk))
                    value.append(chunk)
                    if text.startswith('""', q):
                        value.append('"')
                        i = q + 2
                        continue
                    i = q + 1
                    break
                if i < n and text[i] not in ",\r\n":
                    raise FlowError(
                        f"unexpected {text[i]!r} after a closing quote: a quoted value must be "
                        "followed by ',' or the end of the line",
                        file=file,
                        line=line,
                    )
                fields.append("".join(value))
            else:
                j = i
                while j < n and text[j] not in ",\r\n":
                    j += 1
                raw = text[i:j]
                if '"' in raw:
                    raise FlowError(
                        f"quote inside unquoted value {raw!r}: enclose the value in double "
                        'quotes and double the inner quote ("a""b")',
                        file=file,
                        line=line,
                    )
                fields.append(raw)
                i = j
            if i < n and text[i] == ",":
                i += 1
                continue
            m = _NEWLINE_RE.match(text, i)
            i = m.end() if m else n
            line += 1
            break
        yield start, fields


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def _parse_csv(text: str, file: str) -> _Parsed:
    records = _csv_records(text, file)
    header = next(records, None)
    if header is None:
        raise FlowError("table has no header row: the first line must name the columns", file=file)
    header_line, names = header
    seen: set[str] = set()
    for name in names:
        if name == "":
            raise FlowError(
                "empty column name in the header row; name every column",
                file=file,
                line=header_line,
            )
        if name in seen:
            raise FlowError(
                f"duplicate column {name!r} in the header row; column names must be unique",
                file=file,
                line=header_line,
            )
        seen.add(name)
    columns = tuple(names)
    rows: list[dict[str, str]] = []
    lines: list[int] = []
    for line, fields in records:
        if len(fields) != len(columns):
            raise FlowError(
                f"row has {_plural(len(fields), 'value')}, expected {len(columns)} "
                f"({', '.join(columns)}); quote values that contain ','",
                file=file,
                line=line,
            )
        rows.append(dict(zip(columns, fields, strict=True)))
        lines.append(line)
    return columns, rows, tuple(lines)


# --- YAML ----------------------------------------------------------------------------------------


def _yaml_error(message: str, file: str, node: Node | None = None) -> FlowError:
    if node is None:
        return FlowError(message, file=file)
    mark = node.start_mark
    return FlowError(message, file=file, line=mark.line + 1, col=mark.column + 1)


def _compose(text: str, file: str) -> Node:
    yaml = YAML(typ="safe", pure=True)
    try:
        documents: list[Node] = [doc for doc in yaml.compose_all(text) if doc is not None]
    except MarkedYAMLError as exc:
        mark = exc.problem_mark or exc.context_mark
        detail = "; ".join(str(p) for p in (exc.context, exc.problem) if p)
        raise FlowError(
            f"YAML syntax error: {detail}",
            file=file,
            line=mark.line + 1 if mark is not None else None,
            col=mark.column + 1 if mark is not None else None,
        ) from None
    except YAMLError as exc:  # pragma: no cover - ruamel raises marked errors for bad input
        raise FlowError(f"YAML error: {exc}", file=file) from None
    if not documents:
        raise FlowError(
            "table file is empty: expected a list of maps, e.g. '- {test: smoke}'", file=file
        )
    if len(documents) > 1:
        raise _yaml_error(
            "a table file must contain a single YAML document; remove the '---' separator",
            file,
            documents[1],
        )
    return documents[0]


def _parse_yaml(text: str, file: str) -> _Parsed:
    root = _compose(text, file)
    if not isinstance(root, SequenceNode):
        raise _yaml_error("a YAML table must be a list of maps, e.g. '- {test: smoke}'", file, root)
    columns: tuple[str, ...] = ()
    rows: list[dict[str, str]] = []
    lines: list[int] = []
    for index, item in enumerate(root.value, start=1):
        if not isinstance(item, MappingNode):
            raise _yaml_error(f"row {index} must be a map of column: value", file, item)
        row: dict[str, str] = {}
        for key_node, value_node in item.value:
            if not isinstance(key_node, ScalarNode) or key_node.tag != _STR_TAG:
                raise _yaml_error("column names (map keys) must be strings", file, key_node)
            key = key_node.value
            if key in row:
                raise _yaml_error(f"duplicate key {key!r} in row {index}", file, key_node)
            row[key] = _scalar(key, value_node, file)
        if not row:
            raise _yaml_error(
                f"row {index} has no columns; write at least one column: value", file, item
            )
        if index == 1:
            columns = tuple(row)
        elif set(row) != set(columns):
            raise _yaml_error(
                f"row {index} has keys {sorted(row)}, expected {sorted(columns)}: "
                "all rows must have the same keys",
                file,
                item,
            )
        rows.append({c: row[c] for c in columns})
        lines.append(item.start_mark.line + 1)
    return columns, rows, tuple(lines)


def _scalar(key: str, node: Node, file: str) -> str:
    if not isinstance(node, ScalarNode):
        raise _yaml_error(
            f"value of {key!r} must be a scalar; tables are flat (no lists or maps)", file, node
        )
    if node.tag == _NULL_TAG:
        raise _yaml_error(
            f"value of {key!r} is null; write '' for an empty value or quote it ('null')",
            file,
            node,
        )
    if node.tag not in _SCALAR_TAGS:
        raise _yaml_error(
            f"unsupported tag {node.tag!r} on {key!r}: table values are plain strings", file, node
        )
    value: str = node.value
    return value
