"""Tests for `ebs.flow.seeds` (task P1-11 R1-R4)."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from ebs.core.errors import FlowError
from ebs.flow import seeds
from ebs.flow.seeds import MAX_SEED, SeedOptions, SeedResult, write_seeds
from ebs.flow.tables import load_table

CSV = '''\
# regression list, reviewed by verif
test,seed,plusargs,timeout
smoke,1,"+a,b",10m
"smoke",2,,10m

# long tests
long,7,"say ""hi""",2h
'''

YAML = """\
# regression list
- {test: smoke, seed: '1', plusargs: '+a'}   # first
- test: long    # block style row
  seed: 7
  plusargs: ''
# trailing comment
"""


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_bytes(text.encode("utf-8"))
    return path


def _rows(path: Path) -> list[dict[str, str]]:
    return [dict(r) for r in load_table(path).rows]


def _draws(base: int, tests: list[str], used: set[tuple[str, int]]) -> list[int]:
    """The documented algorithm: one random.Random(base) stream, one draw per entry of `tests`,
    redrawn while the (test, seed) pair is already used."""
    rng = random.Random(base)
    out: list[int] = []
    for test in tests:
        seed = rng.randint(1, MAX_SEED)
        while (test, seed) in used:
            seed = rng.randint(1, MAX_SEED)
        used.add((test, seed))
        out.append(seed)
    return out


# R1
def test_deterministic(tmp_path: Path) -> None:
    a = _write(tmp_path, "a.csv", CSV)
    b = _write(tmp_path, "b.csv", CSV)
    write_seeds(a, SeedOptions(per_test=3, base_seed=42))
    write_seeds(b, SeedOptions(per_test=3, base_seed=42))
    assert a.read_bytes() == b.read_bytes()
    rows = _rows(a)
    assert len(rows) == 3 + 2 * 3
    new = rows[3:]
    # N rows per distinct test, in first-appearance order, other columns copied from its first row
    assert [r["test"] for r in new] == ["smoke"] * 3 + ["long"] * 3
    assert all(r["plusargs"] == "+a,b" and r["timeout"] == "10m" for r in new[:3])
    assert all(r["plusargs"] == 'say "hi"' and r["timeout"] == "2h" for r in new[3:])
    used = {("smoke", 1), ("smoke", 2), ("long", 7)}
    assert [int(r["seed"]) for r in new] == _draws(42, ["smoke"] * 3 + ["long"] * 3, used)

    c = _write(tmp_path, "c.csv", CSV)
    write_seeds(c, SeedOptions(per_test=3, base_seed=43))
    assert c.read_bytes() != a.read_bytes()


# R1
def test_range(tmp_path: Path) -> None:
    path = _write(tmp_path, "t.csv", "test,seed\n" + "".join(f"t{i},\n" for i in range(50)))
    result = write_seeds(path, SeedOptions(per_test=20, base_seed=0))
    assert result.appended == 1000
    seeds = [r["seed"] for r in _rows(path)[50:]]
    assert all(s.isdigit() and 1 <= int(s) <= 2**31 - 1 for s in seeds)
    assert MAX_SEED == 2**31 - 1


# R1
@pytest.mark.parametrize("per_test", [0, -1])
def test_per_test_must_be_positive(tmp_path: Path, per_test: int) -> None:
    path = _write(tmp_path, "t.csv", CSV)
    with pytest.raises(FlowError, match="per-test"):
        write_seeds(path, SeedOptions(per_test=per_test))


def test_nothing_to_do_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, "t.csv", CSV)
    with pytest.raises(FlowError, match="--per-test"):
        write_seeds(path, SeedOptions())
    assert path.read_text() == CSV


@pytest.mark.parametrize(
    ("opts", "match"),
    [
        (SeedOptions(per_test=1, column="sd"), "no column 'sd'"),
        (SeedOptions(per_test=1, test_column="name"), "no column 'name'"),
        (SeedOptions(per_test=1, column="test"), "different"),
    ],
)
def test_missing_columns(tmp_path: Path, opts: SeedOptions, match: str) -> None:
    path = _write(tmp_path, "t.csv", CSV)
    with pytest.raises(FlowError, match=match):
        write_seeds(path, opts)
    assert path.read_text() == CSV


# R2
@pytest.mark.parametrize(("name", "text"), [("t.csv", CSV), ("t.yaml", YAML)])
def test_existing_rows_untouched(tmp_path: Path, name: str, text: str) -> None:
    path = _write(tmp_path, name, text)
    before = _rows(path)
    write_seeds(path, SeedOptions(per_test=2, base_seed=5))
    after = _rows(path)
    assert after[: len(before)] == before
    assert len(after) == len(before) + 2 * 2


# R2
def test_replace_empty_only(tmp_path: Path) -> None:
    text = 'test,seed,x\nsmoke,,a\nsmoke,5,b\nlong,"",c\nlong,9,d\n'
    path = _write(tmp_path, "t.csv", text)
    result = write_seeds(path, SeedOptions(replace=True, base_seed=1))
    assert (result.filled, result.appended) == (2, 0)
    rows = _rows(path)
    assert [(r["test"], r["x"]) for r in rows] == [
        ("smoke", "a"), ("smoke", "b"), ("long", "c"), ("long", "d"),
    ]  # fmt: skip
    assert rows[1]["seed"] == "5"
    assert rows[3]["seed"] == "9"
    assert [int(rows[0]["seed"]), int(rows[2]["seed"])] == _draws(
        1, ["smoke", "long"], {("smoke", 5), ("long", 9)}
    )
    # exact bytes: only the empty cells changed, quoting of the "" cell kept
    s0, s2 = rows[0]["seed"], rows[2]["seed"]
    assert path.read_text() == f'test,seed,x\nsmoke,{s0},a\nsmoke,5,b\nlong,"{s2}",c\nlong,9,d\n'


# R2
def test_replace_yaml_and_then_append(tmp_path: Path) -> None:
    text = "- {test: a, seed: ''}  # fill me\n- {test: a, seed: '3'}\n"
    path = _write(tmp_path, "t.yaml", text)
    result = write_seeds(path, SeedOptions(replace=True, per_test=1, base_seed=2))
    assert (result.filled, result.appended) == (1, 1)
    rows = _rows(path)
    assert rows[1] == {"test": "a", "seed": "3"}
    assert rows[0]["seed"] != ""
    assert len(rows) == 3
    lines = path.read_text().splitlines()
    # the comment stays on its row (ruamel keeps its column, or one space when the row grew)
    assert lines[0].startswith(f"- {{test: a, seed: '{rows[0]['seed']}'}} ")
    assert lines[0].endswith(" # fill me")
    assert lines[1] == "- {test: a, seed: '3'}"


# R2: without --replace an empty seed cell stays empty
def test_append_leaves_empty_cells(tmp_path: Path) -> None:
    path = _write(tmp_path, "t.csv", "test,seed\nsmoke,\n")
    write_seeds(path, SeedOptions(per_test=1))
    rows = _rows(path)
    assert rows[0]["seed"] == ""
    assert rows[1]["seed"] != ""


# R3
@pytest.mark.parametrize("fmt", ["csv", "yaml"])
def test_format_preserved(tmp_path: Path, fmt: str) -> None:
    text = CSV if fmt == "csv" else YAML
    path = _write(tmp_path, f"t.{fmt}", text)
    write_seeds(path, SeedOptions(per_test=1, base_seed=3))
    out = path.read_text()
    rows = _rows(path)
    s_smoke, s_long = rows[-2]["seed"], rows[-1]["seed"]
    if fmt == "csv":
        # existing bytes unchanged (comments, blank line, quoting); new rows copy the raw cells of
        # the test's first row, so its quoting style carries over
        assert out == text + f'smoke,{s_smoke},"+a,b",10m\nlong,{s_long},"say ""hi""",2h\n'
    else:
        assert out.startswith(text.split("# trailing comment")[0])
        assert "# regression list" in out
        assert "# first" in out
        assert "# block style row" in out
        assert "# trailing comment" in out
        # new rows follow the style of the test's first row: flow map with quoted seed, block map
        # with a plain int seed
        assert f"- {{test: smoke, seed: '{s_smoke}', plusargs: '+a'}}" in out
        assert f"- test: long\n  seed: {s_long}\n  plusargs: ''\n" in out
        assert out.count("# first") == 1
        assert out.count("# block style row") == 1


# R3
def test_csv_crlf_bom_and_no_final_newline(tmp_path: Path) -> None:
    raw = "﻿test,seed\r\nsmoke,1"
    path = tmp_path / "t.csv"
    path.write_bytes(raw.encode("utf-8"))
    write_seeds(path, SeedOptions(per_test=1))
    out = path.read_bytes().decode("utf-8")
    seed = _rows(path)[1]["seed"]
    assert out == f"{raw}\r\nsmoke,{seed}\r\n"


# R3
def test_yaml_indentation_kept(tmp_path: Path) -> None:
    text = "-   test: a\n    seed: 1\n"
    path = _write(tmp_path, "t.yml", text)
    write_seeds(path, SeedOptions(per_test=1))
    seed = _rows(path)[1]["seed"]
    assert path.read_text() == text + f"-   test: a\n    seed: {seed}\n"


# R4
def test_no_duplicates(tmp_path: Path) -> None:
    # an existing duplicate pair (017 and 17 are the same seed) is refused, nothing is written
    text = "test,seed\nsmoke,17\nlong,17\nsmoke,017\n"
    path = _write(tmp_path, "t.csv", text)
    with pytest.raises(FlowError, match=r"duplicate \(test, seed\).*smoke.*lines 2 and 4") as info:
        write_seeds(path, SeedOptions(per_test=1))
    assert info.value.line == 4
    assert path.read_text() == text


# R4: generated seeds that clash with existing pairs are redrawn, never written
def test_generated_clash_is_redrawn(tmp_path: Path) -> None:
    first = random.Random(9).randint(1, MAX_SEED)
    path = _write(tmp_path, "t.csv", f"test,seed\nsmoke,{first}\n")
    write_seeds(path, SeedOptions(per_test=5, base_seed=9))
    pairs = [(r["test"], int(r["seed"])) for r in _rows(path)]
    assert len(pairs) == len(set(pairs)) == 6
    # running twice with the same arguments adds fresh seeds, still no duplicates
    write_seeds(path, SeedOptions(per_test=5, base_seed=9))
    pairs = [(r["test"], int(r["seed"])) for r in _rows(path)]
    assert len(pairs) == len(set(pairs)) == 11


def test_yaml_duplicates_refused(tmp_path: Path) -> None:
    text = "- {test: a, seed: '4'}\n- {test: a, seed: 4}\n"
    path = _write(tmp_path, "t.yaml", text)
    with pytest.raises(FlowError, match="duplicate"):
        write_seeds(path, SeedOptions(per_test=1))
    assert path.read_text() == text


def test_invalid_table_is_rejected_unchanged(tmp_path: Path) -> None:
    path = _write(tmp_path, "t.csv", "test,seed\nsmoke\n")
    with pytest.raises(FlowError, match="expected 2"):
        write_seeds(path, SeedOptions(per_test=1))
    assert path.read_text() == "test,seed\nsmoke\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["t.csv"]


# R1: a non-default seed column receives the seeds; other columns are copied
@pytest.mark.parametrize(
    ("name", "text"),
    [("t.csv", "test,sv_seed,x\nsmoke,1,a\n"), ("t.yaml", "- {test: smoke, sv_seed: '1', x: a}\n")],
)
def test_custom_seed_column(tmp_path: Path, name: str, text: str) -> None:
    path = _write(tmp_path, name, text)
    write_seeds(path, SeedOptions(per_test=1, column="sv_seed", base_seed=4))
    new = _rows(path)[1]
    assert new["x"] == "a"
    assert int(new["sv_seed"]) == _draws(4, ["smoke"], {("smoke", 1)})[0]


# R1: the draw order is part of the output: empty cells first (row order), then appended rows
def test_replace_then_append_order(tmp_path: Path) -> None:
    path = _write(tmp_path, "t.csv", "test,seed\nsmoke,\nsmoke,5\n")
    write_seeds(path, SeedOptions(replace=True, per_test=2, base_seed=7))
    rows = _rows(path)
    got = [int(rows[i]["seed"]) for i in (0, 2, 3)]
    assert got == _draws(7, ["smoke"] * 3, {("smoke", 5)})


# R4: seeds drawn in the same run are also never repeated for a test
def test_generated_seeds_unique_within_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    draws = iter([5, 5, 1, 6])
    monkeypatch.setattr(random.Random, "randint", lambda self, a, b: next(draws))
    path = _write(tmp_path, "t.csv", "test,seed\nsmoke,1\n")
    write_seeds(path, SeedOptions(per_test=2))
    assert [r["seed"] for r in _rows(path)] == ["1", "5", "6"]


# R4: only ASCII digit strings compare as numbers
def test_non_ascii_digits_are_strings(tmp_path: Path) -> None:
    arabic_17, superscript_2 = chr(0x661) + chr(0x667), chr(0xB2)
    text = f"test,seed\nsmoke,{arabic_17}\nsmoke,17\nsmoke,{superscript_2}\n"
    path = _write(tmp_path, "t.csv", text)
    assert write_seeds(path, SeedOptions(per_test=1)).appended == 1


# R3
def test_csv_cr_only(tmp_path: Path) -> None:
    raw = b"test,seed\rsmoke,\rlong,3"
    path = tmp_path / "t.csv"
    path.write_bytes(raw)
    write_seeds(path, SeedOptions(replace=True, per_test=1))
    rows = _rows(path)
    s0, s2, s3 = rows[0]["seed"], rows[2]["seed"], rows[3]["seed"]
    assert path.read_bytes() == f"test,seed\rsmoke,{s0}\rlong,3\rsmoke,{s2}\rlong,{s3}\r".encode()


# R3
def test_csv_ending_in_cr_gets_no_blank_line(tmp_path: Path) -> None:
    path = tmp_path / "t.csv"
    path.write_bytes(b"test,seed\rsmoke,1\r")
    write_seeds(path, SeedOptions(per_test=1))
    assert path.read_bytes() == f"test,seed\rsmoke,1\rsmoke,{_rows(path)[1]['seed']}\r".encode()


# R3
def test_yaml_crlf_bom_and_markers(tmp_path: Path) -> None:
    raw = "﻿---\r\n- test: a\r\n  seed: 1\r\n...\r\n"
    path = tmp_path / "t.yaml"
    path.write_bytes(raw.encode())
    write_seeds(path, SeedOptions(per_test=1))
    seed = _rows(path)[1]["seed"]
    expected = f"﻿---\r\n- test: a\r\n  seed: 1\r\n- test: a\r\n  seed: {seed}\r\n...\r\n"
    assert path.read_bytes().decode() == expected


# R3
def test_yaml_indented_dash_kept(tmp_path: Path) -> None:
    text = "  - test: a\n    seed: 1\n"
    path = _write(tmp_path, "t.yaml", text)
    write_seeds(path, SeedOptions(per_test=1))
    assert path.read_text() == text + f"  - test: a\n    seed: {_rows(path)[1]['seed']}\n"


# R3: nothing to do leaves the bytes alone (no YAML round trip)
def test_replace_nothing_to_fill_is_byte_noop(tmp_path: Path) -> None:
    text = "- {test: a,   seed: 1}  # x\n"
    path = _write(tmp_path, "t.yaml", text)
    assert write_seeds(path, SeedOptions(replace=True)) == SeedResult(appended=0, filled=0)
    assert path.read_text() == text


def test_mode_preserved(tmp_path: Path) -> None:
    path = _write(tmp_path, "t.csv", "test,seed\nsmoke,1\n")
    path.chmod(0o640)
    write_seeds(path, SeedOptions(per_test=1))
    assert path.stat().st_mode & 0o777 == 0o640


def test_symlinked_table_edits_target(tmp_path: Path) -> None:
    real = _write(tmp_path, "real.csv", "test,seed\nsmoke,1\n")
    link = tmp_path / "link.csv"
    link.symlink_to(real.name)
    write_seeds(link, SeedOptions(per_test=1))
    assert link.is_symlink()
    assert len(_rows(real)) == 2


# R2: the read-back check refuses an edit that changed or lost rows; the table stays as it was
def test_readback_mismatch_leaves_table_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = "test,seed\nsmoke,1\n"
    path = _write(tmp_path, "t.csv", text)
    monkeypatch.setattr(seeds, "_edit_csv", lambda data, table, opts, edit: "test,seed\nsmoke,2\n")
    with pytest.raises(FlowError, match="internal check failed"):
        write_seeds(path, SeedOptions(per_test=1))
    assert path.read_text() == text
    assert [p.name for p in tmp_path.iterdir()] == ["t.csv"]
