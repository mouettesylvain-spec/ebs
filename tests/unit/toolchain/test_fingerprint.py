"""Install-tree fingerprints (P0-07 R1, R2, R5)."""

from __future__ import annotations

import os
import random
import shutil
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ebs.core.digest import hash_bytes
from ebs.core.errors import ToolchainError
from ebs.toolchain.fingerprint import (
    FingerprintEntry,
    default_content_hash,
    fingerprint_roots,
    scan_roots,
)

ELF_HEADER = b"\x7fELF\x02\x01\x01" + b"\0" * 9


def _write(
    path: Path, data: bytes | str, *, mode: int = 0o644, mtime_ns: int | None = None
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    path.chmod(mode)
    if mtime_ns is not None:
        os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


MTIME = 1_700_000_000_123_456_789


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A small install tree with every kind of entry the fingerprint distinguishes."""
    root = tmp_path / "questa"
    _write(root / "bin" / "vsim", "#!/bin/sh\necho vsim\n", mode=0o755, mtime_ns=MTIME)
    _write(root / "lib" / "libfoo.so", b"not really a library", mtime_ns=MTIME)
    _write(root / "lib" / "libbar.so.1", ELF_HEADER + b"payload", mtime_ns=MTIME)
    _write(root / "tcl" / "init.tcl", "puts hi\n", mtime_ns=MTIME)
    _write(root / "doc" / "README.txt", "docs\n", mtime_ns=MTIME)
    (root / "empty").mkdir()
    (root / "bin" / "vlog").symlink_to("vsim")
    (root / "linkdir").symlink_to("lib")  # must be recorded, not followed
    return root


def _fp(*roots: Path, content_hash: Callable[[Path], bool] = default_content_hash) -> str:
    return str(fingerprint_roots(list(roots), content_hash=content_hash))


def _by_path(entries: list[FingerprintEntry]) -> dict[str, FingerprintEntry]:
    return {e.path: e for e in entries}


# R1
def test_covers_attributes_files(tree: Path) -> None:
    entries = _by_path(scan_roots([tree], content_hash=default_content_hash))
    readme = entries["doc/README.txt"]
    assert readme.root == str(tree)
    assert readme.kind == "file"
    assert readme.size == len("docs\n")
    assert readme.mtime_ns == MTIME
    assert readme.executable is False
    assert readme.content is None  # not selected for content hashing
    vsim = entries["bin/vsim"]
    assert vsim.executable is True
    assert vsim.content == hash_bytes(b"#!/bin/sh\necho vsim\n")


# R1
@pytest.mark.parametrize(
    ("rel", "why"),
    [
        ("bin/vsim", "executable bit"),
        ("lib/libbar.so.1", "ELF magic"),
        ("lib/libfoo.so", ".so suffix"),
        ("tcl/init.tcl", ".tcl suffix"),
    ],
)
def test_covers_attributes_content_hash_default(tree: Path, rel: str, why: str) -> None:
    entry = _by_path(scan_roots([tree], content_hash=default_content_hash))[rel]
    assert entry.content == hash_bytes((tree / rel).read_bytes()), why


# R1
@pytest.mark.parametrize("suffix", [".so", ".sh", ".tcl", ".py"])
def test_covers_attributes_default_suffixes(tmp_path: Path, suffix: str) -> None:
    assert default_content_hash(_write(tmp_path / f"x{suffix}", "data"))
    assert not default_content_hash(_write(tmp_path / f"x{suffix}.txt", "data"))


# R1
def test_covers_attributes_symlinks_by_target(tree: Path) -> None:
    entries = _by_path(scan_roots([tree], content_hash=default_content_hash))
    assert entries["bin/vlog"].kind == "symlink"
    assert entries["bin/vlog"].target == "vsim"
    assert entries["linkdir"].kind == "symlink"
    assert entries["linkdir"].target == "lib"
    assert not any(p.startswith("linkdir/") for p in entries)  # not followed
    assert entries["empty"].kind == "dir"


# R1
def test_covers_attributes_custom_predicate(tree: Path) -> None:
    seen: list[Path] = []

    def only_readme(path: Path) -> bool:
        seen.append(path)
        return path.name == "README.txt"

    entries = _by_path(scan_roots([tree], content_hash=only_readme))
    assert entries["doc/README.txt"].content == hash_bytes(b"docs\n")
    assert entries["bin/vsim"].content is None
    # Called with absolute paths of regular files only.
    assert sorted(p.relative_to(tree).as_posix() for p in seen) == [
        "bin/vsim",
        "doc/README.txt",
        "lib/libbar.so.1",
        "lib/libfoo.so",
        "tcl/init.tcl",
    ]


# R1
def test_order_independent(tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    other = tmp_path / "uvm"
    _write(other / "src" / "uvm.sv", "// uvm\n", mtime_ns=MTIME)
    baseline = _fp(tree, other)
    assert _fp(other, tree) == baseline  # root order
    assert _fp(tree, other, tree) == baseline  # duplicate roots

    real_scandir = os.scandir

    class Shuffled(list[os.DirEntry[str]]):
        def __enter__(self) -> Shuffled:
            return self

        def __exit__(self, *exc: object) -> None:
            pass

    def shuffled_scandir(path: str | os.PathLike[str]) -> Shuffled:
        with real_scandir(path) as it:
            entries = Shuffled(it)
        random.Random(len(entries)).shuffle(entries)
        entries.reverse()
        return entries

    monkeypatch.setattr(os, "scandir", shuffled_scandir)
    assert _fp(tree, other) == baseline  # directory listing order


_NAMES = st.lists(
    st.from_regex(r"[a-z]{1,3}(/[a-z]{1,3}){0,2}", fullmatch=True), min_size=1, max_size=12
)


# R1
@settings(
    max_examples=50, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(names=_NAMES, order=st.randoms(use_true_random=False))
def test_order_independent_property(
    tmp_path_factory: pytest.TempPathFactory, names: list[str], order: random.Random
) -> None:
    # Files are created in two different orders in two trees at the same absolute path.
    files = sorted({n for n in names if not any(o.startswith(n + "/") for o in names)})
    digests = []
    root = tmp_path_factory.mktemp("prop") / "root"
    for attempt in range(2):
        if attempt:
            order.shuffle(files)
            shutil.rmtree(root)
        for rel in files:
            mode = 0o755 if len(rel) % 2 else 0o644
            _write(root / rel, rel, mode=mode, mtime_ns=MTIME + len(rel))
        digests.append(_fp(root))
    assert digests[0] == digests[1]


# R1
def test_roots_are_distinguished(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    _write(a / "f", "x", mtime_ns=MTIME)
    _write(b / "f", "x", mtime_ns=MTIME)
    # Same relative content under a different root path is a different install.
    assert _fp(a) != _fp(b)


def _rename(tree: Path) -> None:
    (tree / "doc" / "README.txt").rename(tree / "doc" / "README2.txt")


def _grow(tree: Path) -> None:
    _write(tree / "doc" / "README.txt", "docs!\n", mtime_ns=MTIME)


def _touch(tree: Path) -> None:
    os.utime(tree / "doc" / "README.txt", ns=(MTIME, MTIME + 1))


def _chmod_x(tree: Path) -> None:
    (tree / "doc" / "README.txt").chmod(0o744)


def _patch_executable(tree: Path) -> None:
    # Same size, same mtime: only the content hash can see it.
    _write(tree / "bin" / "vsim", "#!/bin/sh\necho VSIM\n", mode=0o755, mtime_ns=MTIME)


def _retarget(tree: Path) -> None:
    (tree / "bin" / "vlog").unlink()
    (tree / "bin" / "vlog").symlink_to("vsim2")


def _add_file(tree: Path) -> None:
    _write(tree / "doc" / "NEW", "", mtime_ns=MTIME)


def _add_empty_dir(tree: Path) -> None:
    (tree / "empty2").mkdir()


def _file_to_symlink(tree: Path) -> None:
    (tree / "doc" / "README.txt").unlink()
    (tree / "doc" / "README.txt").symlink_to("x")


# R2
@pytest.mark.parametrize(
    "change",
    [
        _rename,
        _grow,
        _touch,
        _chmod_x,
        _patch_executable,
        _retarget,
        _add_file,
        _add_empty_dir,
        _file_to_symlink,
    ],
)
def test_sensitivity(tree: Path, change: Callable[[Path], None]) -> None:
    before = _fp(tree)
    change(tree)
    assert _fp(tree) != before


# R2
def test_sensitivity_custom_predicate_content(tree: Path) -> None:
    def everything(path: Path) -> bool:
        return True

    before = _fp(tree, content_hash=everything)
    _write(tree / "doc" / "README.txt", "DOCS\n", mtime_ns=MTIME)  # same size and mtime
    assert _fp(tree, content_hash=everything) != before


def test_unhashed_content_not_read(tree: Path) -> None:
    # Documents the cost model: a non-selected file is identified by its stat attributes only.
    before = _fp(tree)
    _write(tree / "doc" / "README.txt", "DOCS\n", mtime_ns=MTIME)
    assert _fp(tree) == before


# R2
def test_insensitive_to_atime_owner(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    before = _fp(tree)
    for path in (tree / "doc" / "README.txt", tree / "bin" / "vsim"):
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns + 10**12, st.st_mtime_ns))  # atime only
    (tree / "bin" / "vsim").read_bytes()
    assert _fp(tree) == before

    # Ownership: chown needs root, so present every entry with a different uid/gid.
    real_lstat = os.lstat

    def foreign_owner(path: str | os.PathLike[str], **kw: object) -> os.stat_result:
        st = real_lstat(path)
        fields = list(st)
        fields[4] += 4242  # st_uid
        fields[5] += 4242  # st_gid
        return os.stat_result(fields, {"st_mtime_ns": st.st_mtime_ns})

    monkeypatch.setattr(os, "lstat", foreign_owner)
    assert _fp(tree) == before


def test_insensitive_to_group_change_when_possible(tree: Path) -> None:
    path = tree / "doc" / "README.txt"
    others = [g for g in os.getgroups() if g != path.stat().st_gid]
    if not others:
        pytest.skip("the test user belongs to a single group; covered by the lstat variant")
    before = _fp(tree)
    os.chown(path, -1, others[0])
    assert _fp(tree) == before


def test_missing_root_is_actionable(tmp_path: Path) -> None:
    with pytest.raises(ToolchainError, match=r"install root .*missing.*does not exist"):
        fingerprint_roots([tmp_path / "missing"], content_hash=default_content_hash)


def test_root_must_be_directory(tmp_path: Path) -> None:
    f = _write(tmp_path / "file", "x")
    with pytest.raises(ToolchainError, match="not a directory"):
        fingerprint_roots([f], content_hash=default_content_hash)


def test_relative_root_rejected(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tree.parent)
    with pytest.raises(ToolchainError, match="absolute"):
        fingerprint_roots([Path("questa")], content_hash=default_content_hash)


def test_no_roots_rejected() -> None:
    with pytest.raises(ToolchainError, match="at least one install root"):
        fingerprint_roots([], content_hash=default_content_hash)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every file")
def test_unreadable_hashed_file_is_actionable(tree: Path) -> None:
    (tree / "bin" / "vsim").chmod(0o311)  # executable, not readable
    with pytest.raises(ToolchainError, match=r"bin/vsim"):
        fingerprint_roots([tree], content_hash=default_content_hash)


def test_special_files_recorded(tree: Path) -> None:
    os.mkfifo(tree / "pipe")
    entry = _by_path(scan_roots([tree], content_hash=lambda p: True))["pipe"]
    assert entry.kind == "other"  # recorded, never opened (opening a FIFO would block)


def test_non_utf8_and_non_nfc_names(tmp_path: Path) -> None:
    root = tmp_path / "t"
    root.mkdir()
    (Path(os.fsdecode(bytes(root) + b"/caf\xe9"))).write_text("latin-1 name")
    _write(root / "café", "NFD name")
    a = _fp(root)
    (root / "café").rename(root / "café")  # NFC spelling: a different file name
    assert _fp(root) != a


# R5
@pytest.mark.slow
def test_large_tree_perf(tmp_path: Path) -> None:
    root = tmp_path / "big"
    for d in range(200):
        sub = root / f"d{d:03}"
        sub.mkdir(parents=True)
        for f in range(100):
            p = sub / f"f{f:03}.dat"
            p.write_bytes(b"x" * 64)
            if f == 0:
                p.chmod(0o755)  # 200 executables, everything else stat-only
    start = time.perf_counter()
    entries = scan_roots([root], content_hash=default_content_hash)
    fingerprint_roots([root], content_hash=default_content_hash)
    elapsed = time.perf_counter() - start
    assert sum(e.kind == "file" for e in entries) == 20_000
    assert sum(e.content is not None for e in entries) == 200
    assert elapsed < 10.0, f"two passes over 20,000 files took {elapsed:.1f} s"
