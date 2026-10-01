"""Glob resolution: gitignore-style semantics (R1) and the stay-inside-base rule (R2)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ebs.core.errors import SourceError, SourceEscapeError
from ebs.sources.globs import resolve_glob
from tests.helpers.sources import write


@pytest.fixture
def base(tmp_path: Path) -> Path:
    root = tmp_path / "base"
    for rel in (
        "a.sv",
        "b.v",
        ".hidden.sv",
        "sub/c.sv",
        "sub/.h/d.sv",
        "sub/deep/e.sv",
        "sub/deep/f.txt",
    ):
        write(root / rel, rel)
    (root / "lnk.sv").symlink_to("a.sv")
    (root / "sublink").symlink_to("sub")
    return root


SEMANTICS = [
    ("*.sv", ["a.sv", "lnk.sv"]),
    (".*.sv", [".hidden.sv"]),
    ("**/*.sv", ["a.sv", "lnk.sv", "sub/c.sv", "sub/deep/e.sv"]),
    ("sub/**", ["sub/c.sv", "sub/deep/e.sv", "sub/deep/f.txt"]),
    ("sub/**/e.sv", ["sub/deep/e.sv"]),
    ("sub/**/c.sv", ["sub/c.sv"]),  # ** matches zero directories
    ("**/**/c.sv", ["sub/c.sv"]),  # overlapping ** produce no duplicates
    ("sub/.h/*.sv", ["sub/.h/d.sv"]),
    ("**/.h/*.sv", ["sub/.h/d.sv"]),  # a literal hidden segment may follow **
    ("sub/*/*.sv", ["sub/deep/e.sv"]),  # * does not match hidden .h
    ("?.v", ["b.v"]),
    ("[ab].*", ["a.sv", "b.v"]),
    ("[!a].*", ["b.v"]),
    ("sub", ["sub/c.sv", "sub/deep/e.sv", "sub/deep/f.txt"]),  # a dir means dir/**
    ("a.sv", ["a.sv"]),
    ("./sub//c.sv", ["sub/c.sv"]),
    ("sub/../a.sv", ["a.sv"]),  # '..' that stays inside is normalized
    ("**", ["a.sv", "b.v", "lnk.sv", "sub/c.sv", "sub/deep/e.sv", "sub/deep/f.txt", "sublink"]),
    ("sublink", ["sublink"]),  # a final symlink is kept, never expanded
    ("sublink/c.sv", ["sublink/c.sv"]),  # an inner symlink to a dir inside base is followed
    ("s*/c.sv", ["sub/c.sv", "sublink/c.sv"]),
]


# R1
@pytest.mark.parametrize(("pattern", "expected"), SEMANTICS, ids=[p for p, _ in SEMANTICS])
def test_semantics(base: Path, pattern: str, expected: list[str]) -> None:
    assert resolve_glob(base, pattern) == expected


# R1
def test_results_sorted_bytewise(tmp_path: Path) -> None:
    for name in ("b", "B", "a", "é", "_"):
        write(tmp_path / name, name)
    assert resolve_glob(tmp_path, "*") == ["B", "_", "a", "b", "é"]


# R1
def test_no_match_error(base: Path) -> None:
    with pytest.raises(SourceError, match=r"'\*\*/\*\.vhd'.*matched no files") as exc:
        resolve_glob(base, "**/*.vhd")
    assert str(base) in str(exc.value)
    assert not isinstance(exc.value, SourceEscapeError)
    assert resolve_glob(base, "**/*.vhd", optional=True) == []


# R1
def test_empty_directory_is_no_match(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    with pytest.raises(SourceError, match="matched no files"):
        resolve_glob(tmp_path, "empty")


# R1
def test_missing_base_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(SourceError, match="not a directory"):
        resolve_glob(tmp_path / "nope", "*")


# R1
@pytest.mark.parametrize("pattern", ["", ".", "./"])
def test_empty_pattern_rejected(base: Path, pattern: str) -> None:
    with pytest.raises(SourceError, match="pattern"):
        resolve_glob(base, pattern)


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    out = tmp_path / "outside"
    write(out / "secret.sv", "secret")
    return out


ESCAPES = [
    ("../outside/secret.sv", "outside/secret.sv"),
    ("sub/../../outside/*", "outside/*"),
    ("/etc/passwd", "/etc/passwd"),
    ("out", "outside"),
    ("out/*.sv", "outside"),
    ("**", "outside"),
]


# R2
@pytest.mark.parametrize(("pattern", "resolved"), ESCAPES, ids=[p for p, _ in ESCAPES])
def test_escape_rejected(base: Path, outside: Path, pattern: str, resolved: str) -> None:
    (base / "out").symlink_to(outside)  # a symlink resolving outside base
    with pytest.raises(SourceEscapeError) as exc:
        resolve_glob(base, pattern, optional=True)
    message = str(exc.value)
    assert repr(pattern) in message
    expected = resolved if resolved.startswith("/") else str(base.parent / resolved)
    assert f"resolves to {expected}" in message


# R2
def test_escape_via_file_symlink(base: Path, outside: Path) -> None:
    (base / "x.sv").symlink_to(outside / "secret.sv")
    with pytest.raises(SourceEscapeError, match=str(outside / "secret.sv")):
        resolve_glob(base, "*.sv")


# R2
def test_escape_via_dangling_symlink(base: Path, tmp_path: Path) -> None:
    (base / "x.sv").symlink_to(tmp_path / "gone" / "x.sv")
    with pytest.raises(SourceEscapeError):
        resolve_glob(base, "x.sv")


# R2
def test_symlink_chain_inside_base_is_allowed(base: Path) -> None:
    (base / "l2.sv").symlink_to("lnk.sv")
    assert resolve_glob(base, "l2.sv") == ["l2.sv"]


# R2
def test_base_itself_through_symlink(tmp_path: Path, base: Path) -> None:
    alias = tmp_path / "alias"
    alias.symlink_to(base)
    assert resolve_glob(alias, "sub/c.sv") == ["sub/c.sv"]
    assert resolve_glob(alias, "sublink/c.sv") == ["sublink/c.sv"]


# R1: unreadable directory is an actionable error, not a silent no-match
@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any directory")
def test_unreadable_dir(base: Path) -> None:
    (base / "sub").chmod(0)
    try:
        with pytest.raises(SourceError, match="cannot read"):
            resolve_glob(base, "sub/**")
    finally:
        (base / "sub").chmod(0o755)


_NAMES = st.sampled_from(["a.sv", "b.v", ".h.sv", "c", ".d", "e f.sv", "ü.sv"])


def _reference_all(root: Path) -> list[str]:
    """Non-hidden files below root through non-hidden dirs: the documented `**` meaning."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        rel = Path(dirpath).relative_to(root)
        found += [(rel / f).as_posix() for f in filenames if not f.startswith(".")]
    return sorted(found)


# R1: `**` and `**/*.sv` agree with a straightforward reference walk on random trees
@given(st.lists(st.lists(_NAMES, min_size=1, max_size=4), min_size=1, max_size=12))
def test_double_star_matches_reference(paths: list[list[str]]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for parts in paths:
            target = root.joinpath(*parts)
            if any(p.is_file() for p in [*target.parents, target] if root in p.parents):
                continue  # a file already occupies a parent name
            if target.is_dir():
                continue
            write(target, "x")
        everything = _reference_all(root)
        assert resolve_glob(root, "**", optional=True) == everything
        assert resolve_glob(root, "**/*.sv", optional=True) == [
            # a matched directory stands for everything below it
            p
            for p in everything
            if any(part.endswith(".sv") for part in p.split("/"))
        ]
