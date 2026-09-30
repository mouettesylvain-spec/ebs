"""Tests for `ebs.flow.tables` (task P0-05 R1, R2, R9)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ebs.core.digest import hash_bytes
from ebs.core.errors import FlowError
from ebs.flow.tables import load_table

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "tables"


def _write(tmp_path: Path, name: str, data: str | bytes) -> Path:
    path = tmp_path / name
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)
    return path


# R1
def test_csv_fixture() -> None:
    table = load_table(FIXTURES / "regression.csv")
    assert table.columns == ("test", "seed", "plusargs", "timeout")
    assert [dict(r) for r in table.rows] == [
        {"test": "smoke", "seed": "1", "plusargs": "", "timeout": "10m"},
        {"test": "smoke", "seed": "2", "plusargs": "+verbose", "timeout": "10m"},
        {"test": "alu_rand", "seed": "17", "plusargs": "+a=1,+b=2", "timeout": "1h"},
    ]
    assert table.lines == (3, 5, 6)
    assert table.source == FIXTURES / "regression.csv"


# R1
@pytest.mark.parametrize(
    ("text", "message", "line"),
    [
        pytest.param("", "no header row", None, id="empty"),
        pytest.param("# only a comment\n\n", "no header row", None, id="comments-only"),
        pytest.param("a,b,a\n1,2,3\n", "duplicate column 'a'", 1, id="duplicate-column"),
        pytest.param("a,,c\n1,2,3\n", "empty column name", 1, id="empty-column"),
        pytest.param("a,b\n1,2\n1,2,3\n", "row has 3 values, expected 2", 3, id="ragged-long"),
        pytest.param("a,b\n1,2\n\n1\n", "row has 1 value, expected 2", 4, id="ragged-short"),
        pytest.param('a,b\n1,"2\n', "unterminated quoted value", 2, id="open-quote"),
        pytest.param('a,b\n"x\r\ny",1\n2\n', "row has 1 value", 4, id="line-after-multiline"),
        pytest.param('a,b\n"x"y,1\n', "after a closing quote", 2, id="text-after-quote"),
        pytest.param('a,b\nx"y,1\n', "quote inside unquoted value", 2, id="bare-quote"),
    ],
)
def test_csv_rules(tmp_path: Path, text: str, message: str, line: int | None) -> None:
    path = _write(tmp_path, "t.csv", text)
    with pytest.raises(FlowError) as exc:
        load_table(path)
    assert message in str(exc.value)
    assert exc.value.file == str(path)
    assert exc.value.line == line


# R1
def test_csv_values_stay_strings(tmp_path: Path) -> None:
    path = _write(tmp_path, "t.csv", "seed,flag,ratio,empty,pad\n007,true,1.50,, x \n")
    (row,) = load_table(path).rows
    assert dict(row) == {"seed": "007", "flag": "true", "ratio": "1.50", "empty": "", "pad": " x "}


# R1
def test_bom_and_comments(tmp_path: Path) -> None:
    text = (
        '﻿lib,filelist\r\n# comment\r\n\r\n   \r\ncore,core.f\r\nalu,"alu\n# kept.f"\r\nlast,l.f\r\n'
    )
    table = load_table(_write(tmp_path, "t.csv", text))
    assert table.columns == ("lib", "filelist")  # BOM stripped from the first column name
    assert [dict(r) for r in table.rows] == [
        {"lib": "core", "filelist": "core.f"},
        {"lib": "alu", "filelist": "alu\n# kept.f"},  # '#' inside a quoted value is data
        {"lib": "last", "filelist": "l.f"},
    ]
    assert table.lines == (5, 6, 8)


# R1
def test_csv_comment_before_header(tmp_path: Path) -> None:
    table = load_table(_write(tmp_path, "t.csv", "# header follows\n\na\n1\n"))
    assert table.columns == ("a",)
    assert table.lines == (4,)


# R1
def test_csv_invalid_utf8(tmp_path: Path) -> None:
    path = _write(tmp_path, "t.csv", b"a\n\xff\n")
    with pytest.raises(FlowError, match="not valid UTF-8"):
        load_table(path)


# R1
def test_unknown_extension(tmp_path: Path) -> None:
    path = _write(tmp_path, "t.tsv", "a\n1\n")
    with pytest.raises(FlowError, match=r"\.csv.*\.yaml"):
        load_table(path)


# R1
def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FlowError, match="cannot read table"):
        load_table(tmp_path / "missing.csv")


# R1
def test_header_only_is_empty_table(tmp_path: Path) -> None:
    table = load_table(_write(tmp_path, "t.csv", "a,b\n"))
    assert table.columns == ("a", "b")
    assert table.rows == ()


# R1
def test_rows_are_immutable() -> None:
    table = load_table(FIXTURES / "libs.csv")
    with pytest.raises(TypeError):
        table.rows[0]["lib"] = "x"  # type: ignore[index]


# R2
def test_yaml_fixture() -> None:
    table = load_table(FIXTURES / "modes.yaml")
    assert table.columns == ("mode", "opt", "level")
    # Scalars keep their source text: no int/float guessing (007 and 1.10 survive).
    assert [dict(r) for r in table.rows] == [
        {"mode": "fast", "opt": "-O1", "level": "007"},
        {"mode": "full", "opt": "-O3", "level": "1.10"},
    ]
    assert table.lines == (2, 5)


# R2
def test_yaml_scalars(tmp_path: Path) -> None:
    text = "- {a: true, b: '', c: 'x: y', d: \"q\\\"\", e: -1}\n"
    (row,) = load_table(_write(tmp_path, "t.yml", text)).rows
    assert dict(row) == {"a": "true", "b": "", "c": "x: y", "d": 'q"', "e": "-1"}


# R2
@pytest.mark.parametrize(
    ("text", "message", "line"),
    [
        pytest.param("", "empty", None, id="empty"),
        pytest.param("a: 1\n", "must be a list of maps", 1, id="not-a-list"),
        pytest.param("- 1\n", "row 1 must be a map", 1, id="row-not-map"),
        pytest.param("- {a: [1]}\n", "value of 'a' must be a scalar", 1, id="nested-list"),
        pytest.param("- a: {b: 1}\n", "value of 'a' must be a scalar", 1, id="nested-map"),
        pytest.param("- {a: 1}\n- {b: 1}\n", "row 2 has keys ['b'], expected ['a']", 2, id="keys"),
        pytest.param("- {a: 1}\n- {a: 1, b: 2}\n", "expected ['a']", 2, id="extra-key"),
        pytest.param("- {a: 1, a: 2}\n", "duplicate key 'a'", 1, id="duplicate-key"),
        pytest.param("- {a: }\n", "value of 'a' is null", 1, id="null"),
        pytest.param("- {a: ~}\n", "value of 'a' is null", 1, id="tilde"),
        pytest.param("- {a: !!binary aGk=}\n", "unsupported tag", 1, id="tag"),
        pytest.param("- {}\n", "row 1 has no columns", 1, id="empty-map"),
        pytest.param("- {1: x}\n", "map keys) must be strings", 1, id="int-key"),
        pytest.param("- {a: 1}\n---\n- {a: 2}\n", "single YAML document", None, id="multi-doc"),
        pytest.param("- {a: [1\n", "YAML syntax error", 2, id="syntax"),
    ],
)
def test_yaml_table_rules(tmp_path: Path, text: str, message: str, line: int | None) -> None:
    path = _write(tmp_path, "t.yaml", text)
    with pytest.raises(FlowError) as exc:
        load_table(path)
    assert message in str(exc.value)
    assert exc.value.file == str(path)
    if line is not None:
        assert exc.value.line == line


# R2
def test_yaml_aliases_and_empty_list(tmp_path: Path) -> None:
    table = load_table(_write(tmp_path, "t.yaml", "- &r {a: x}\n- *r\n"))
    assert [dict(r) for r in table.rows] == [{"a": "x"}, {"a": "x"}]
    empty = load_table(_write(tmp_path, "e.yaml", "[]\n"))
    assert empty.columns == ()
    assert empty.rows == ()


# R9
@pytest.mark.parametrize("name", ["regression.csv", "modes.yaml"])
def test_digest_is_file_bytes(name: str) -> None:
    path = FIXTURES / name
    assert load_table(path).digest == hash_bytes(path.read_bytes())


# R9
def test_digest_changes_with_comment_only_edit(tmp_path: Path) -> None:
    a = load_table(_write(tmp_path, "a.csv", "x\n1\n"))
    b = load_table(_write(tmp_path, "b.csv", "x\n# note\n1\n"))
    assert a.rows == b.rows
    assert a.digest != b.digest  # bytes, not parsed content
