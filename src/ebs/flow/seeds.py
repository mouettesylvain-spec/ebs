"""`ebs seeds gen`: write explicit seeds into a parameter table (task P1-11).

Seeds are table values, never generated at build time (architecture.md § Explicitness rules),
so this command writes them into the table, where they get committed and reviewed.

Algorithm (deterministic: same table and same options give the same bytes). One stream
`random.Random(base_seed)` yields seeds with `randint(1, 2**31 - 1)` (31-bit positive ints):

1. With `replace`, every row whose seed cell is empty gets a seed, in row order.
2. With `per_test=N`, N rows are appended per distinct test (in order of first appearance); each
   copies every other column of that test's first row.

A drawn seed whose `(test, seed)` pair is already in the table (or was just generated) is
dropped and the next one drawn, so the command never writes a duplicate pair. Seeds made only
of ASCII digits compare as numbers (`017` = `17`). A table that already holds a duplicate pair is
refused. Existing rows never change, apart from the empty seed cells `replace` fills.

Formatting is kept: a CSV file is edited as text, so its existing bytes (comments, blank lines,
quoting, line ends, BOM) stay as they are; an appended row repeats the raw cells of its template
row and quotes the seed only if the template's seed cell is quoted. A YAML table is round-tripped
with ruamel: comments, quotes, the sequence indentation (guessed from the first row), line ends,
BOM and `---`/`...` markers are kept, but ruamel may normalise spacing inside rows (e.g. inside
flow maps, or the column of an end-of-line comment on a row that grew). A new row takes the flow
or block style of its template and its seed cell's quoting. New rows go at the end of the file,
so a comment on the last lines ends up above them.

The result is written to a temporary file next to the table, loaded back with `load_table` as a
check, then renamed over the table.
"""

from __future__ import annotations

import io
import os
import random
import re
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import ScalarString
from ruamel.yaml.util import load_yaml_guess_indent

from ebs.core.errors import FlowError
from ebs.flow.tables import Table, load_table

__all__ = ["MAX_SEED", "SeedOptions", "SeedResult", "write_seeds"]

MAX_SEED: Final = 2**31 - 1
_NEWLINE_RE: Final = re.compile(r"\r\n|\r|\n")
_BOM: Final = "﻿"


@dataclass(frozen=True)
class SeedOptions:
    """What `ebs seeds gen` does; at least one of `per_test` and `replace` must be set."""

    per_test: int | None = None
    column: str = "seed"
    test_column: str = "test"
    base_seed: int = 0
    replace: bool = False


@dataclass(frozen=True)
class SeedResult:
    appended: int
    filled: int


_Pair = tuple[str, int | str]


def _seed_key(value: str) -> int | str:
    return int(value) if value.isascii() and value.isdigit() else value


class _Generator:
    """The seed stream, skipping pairs already used."""

    def __init__(self, base_seed: int, used: set[_Pair]) -> None:
        self._rng = random.Random(base_seed)
        self._used = used

    def next(self, test: str) -> int:
        while True:
            seed = self._rng.randint(1, MAX_SEED)
            if (test, seed) not in self._used:
                self._used.add((test, seed))
                return seed


@dataclass(frozen=True)
class _Edit:
    fills: list[tuple[int, int]]  # (row index, seed)
    appends: list[tuple[int, int]]  # (template row index, seed)


def _plan(table: Table, opts: SeedOptions) -> _Edit:
    file = str(table.source)
    if opts.per_test is None and not opts.replace:
        raise FlowError(
            "nothing to do: give --per-test N to append rows, or --replace to fill empty seeds",
            file=file,
        )
    if opts.per_test is not None and opts.per_test < 1:
        raise FlowError(f"--per-test must be at least 1, got {opts.per_test}", file=file)
    if opts.column == opts.test_column:
        raise FlowError(
            f"the seed column and the test column must be different (both {opts.column!r})",
            file=file,
        )
    for name, flag in ((opts.test_column, "--test-column"), (opts.column, "--column")):
        if name not in table.columns:
            raise FlowError(
                f"table has no column {name!r} (columns: {', '.join(table.columns)}); add it "
                f"to the header or choose another with {flag}",
                file=file,
            )
    seen: dict[_Pair, int] = {}
    templates: dict[str, int] = {}
    empty: list[int] = []
    for index, row in enumerate(table.rows):
        test, seed = row[opts.test_column], row[opts.column]
        templates.setdefault(test, index)
        if seed == "":
            empty.append(index)
            continue
        pair = (test, _seed_key(seed))
        if pair in seen:
            first = table.lines[seen[pair]] if seen[pair] < len(table.lines) else seen[pair] + 1
            line = table.lines[index] if index < len(table.lines) else None
            raise FlowError(
                f"duplicate (test, seed) pair ({test!r}, {seed!r}) on lines {first} and {line}; "
                "remove one of the rows before generating seeds",
                file=file,
                line=line,
            )
        seen[pair] = index
    gen = _Generator(opts.base_seed, set(seen))
    fills = [(i, gen.next(table.rows[i][opts.test_column])) for i in empty] if opts.replace else []
    appends = [
        (template, gen.next(test))
        for test, template in templates.items()
        for _ in range(opts.per_test or 0)
    ]
    return _Edit(fills, appends)


# --- CSV -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _RawRecord:
    spans: list[tuple[int, int]]  # [start, end) of each field's raw text, quotes included


def _raw_records(text: str) -> Iterator[_RawRecord]:
    """Field spans of each record; same dialect as `tables._csv_records` (text already valid)."""
    i, n = 0, len(text)
    while i < n:
        m = _NEWLINE_RE.search(text, i)
        eol, nxt = (m.start(), m.end()) if m else (n, n)
        if text.startswith("#", i) or not text[i:eol].strip():
            i = nxt
            continue
        spans: list[tuple[int, int]] = []
        while True:
            start = i
            if text.startswith('"', i):
                i += 1
                while True:
                    q = text.index('"', i)
                    if text.startswith('""', q):
                        i = q + 2
                        continue
                    i = q + 1
                    break
            else:
                while i < n and text[i] not in ",\r\n":
                    i += 1
            spans.append((start, i))
            if i < n and text[i] == ",":
                i += 1
                continue
            m = _NEWLINE_RE.match(text, i)
            i = m.end() if m else n
            break
        yield _RawRecord(spans)


def _csv_seed(seed: int, quoted: bool) -> str:
    return f'"{seed}"' if quoted else str(seed)


def _edit_csv(data: bytes, table: Table, opts: SeedOptions, edit: _Edit) -> str:
    text = data.decode("utf-8-sig")
    bom = _BOM if data.startswith(_BOM.encode("utf-8")) else ""
    records = list(_raw_records(text))[1:]  # drop the header
    col = table.columns.index(opts.column)
    out = text
    for index, seed in sorted(edit.fills, reverse=True):  # splice from the end: offsets stay valid
        start, end = records[index].spans[col]
        out = out[:start] + _csv_seed(seed, text.startswith('"', start)) + out[end:]
    if edit.appends:
        m = _NEWLINE_RE.search(text)
        newline = m.group() if m else "\n"
        if out and not out.endswith(("\n", "\r")):
            out += newline
        for template, seed in edit.appends:
            cells = [text[s:e] for s, e in records[template].spans]
            cells[col] = _csv_seed(seed, cells[col].startswith('"'))
            out += ",".join(cells) + newline
    return bom + out


# --- YAML ----------------------------------------------------------------------------------------


def _yaml_seed(old: object, seed: int) -> object:
    if isinstance(old, ScalarString):
        return type(old)(str(seed))
    return seed


_DOC_START_RE: Final = re.compile(r"^---(?=[ \t\r\n]|\Z)", re.MULTILINE)
_DOC_END_RE: Final = re.compile(r"^\.\.\.[ \t]*\Z", re.MULTILINE)


def _edit_yaml(data: bytes, opts: SeedOptions, edit: _Edit) -> str:
    text = data.decode("utf-8-sig")
    bom = _BOM if data.startswith(_BOM.encode("utf-8")) else ""
    m = _NEWLINE_RE.search(text)
    newline = m.group() if m else "\n"
    yaml = YAML(typ="rt", pure=True)
    yaml.preserve_quotes = True
    yaml.width = 1 << 16  # never re-wrap long lines
    yaml.explicit_start = _DOC_START_RE.search(text) is not None
    yaml.explicit_end = _DOC_END_RE.search(text.rstrip()) is not None
    root, indent, block_seq_indent = load_yaml_guess_indent(text, yaml=yaml)
    if indent is not None:
        offset = block_seq_indent or 0
        yaml.indent(mapping=indent, sequence=max(indent, offset + 2), offset=offset)
    assert isinstance(root, CommentedSeq)  # load_table accepted it as a list of maps
    for index, seed in edit.fills:
        row = root[index]
        row[opts.column] = _yaml_seed(row[opts.column], seed)
    for template_index, seed in edit.appends:
        template: CommentedMap = root[template_index]
        new = CommentedMap()
        for key, value in template.items():
            new[key] = _yaml_seed(value, seed) if key == opts.column else value
        if template.fa.flow_style():
            new.fa.set_flow_style()
        root.append(new)
    buffer = io.StringIO()
    yaml.dump(root, buffer)
    out = buffer.getvalue()
    return bom + (out.replace("\n", newline) if newline != "\n" else out)


# --- entry point ---------------------------------------------------------------------------------


def write_seeds(path: Path, opts: SeedOptions) -> SeedResult:
    """Add seeds to the table at `path` in place (see the module docstring).

    A symlinked table is edited at its target; the link stays."""
    path = path.resolve()
    table = load_table(path)
    edit = _plan(table, opts)
    result = SeedResult(appended=len(edit.appends), filled=len(edit.fills))
    if not edit.fills and not edit.appends:
        return result
    data = path.read_bytes()
    if path.suffix.lower() == ".csv":
        text = _edit_csv(data, table, opts, edit)
    else:
        text = _edit_yaml(data, opts, edit)
    expected = [dict(r) for r in table.rows]
    for index, seed in edit.fills:
        expected[index][opts.column] = str(seed)
    _replace_checked(path, text.encode("utf-8"), expected, result.appended)
    return result


def _replace_checked(
    path: Path, data: bytes, expected: Sequence[Mapping[str, str]], appended: int
) -> None:
    """Write `data` over `path` atomically, after checking it reads back as the old rows (with
    filled seeds) followed by `appended` new rows."""
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=path.suffix, dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        rows = [dict(r) for r in load_table(tmp).rows]
        if rows[: len(expected)] != list(expected) or len(rows) != len(expected) + appended:
            raise FlowError(  # guards a bug in the editors above
                "internal check failed: the edited table does not read back as the old rows plus "
                "the new ones; the table was left unchanged, please report this",
                file=str(path),
            )
        os.chmod(tmp, path.stat().st_mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
