"""R7: materializing blobs and trees on local scratch (copy / hardlink / symlink / auto)."""

from __future__ import annotations

import errno
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, BinaryIO

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from ebs.cas.fs import FsCAS
from ebs.cas.materialize import _resolve, materialize
from ebs.core.canon import canonical_json
from ebs.core.digest import Digest, hash_bytes
from ebs.core.errors import CasError
from ebs.core.tree import TreeEntry, TreeManifest
from tests.helpers.cas import sample_tree

THRESHOLD = 100  # sample_tree: c.bin is 2048 bytes (large), everything else is small


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.lstat().st_mode)


def _files(root: Path) -> dict[str, Path]:
    return {
        str(p.relative_to(root)): p for p in root.rglob("*") if not p.is_dir() or p.is_symlink()
    }


def _store_manifest(cas: FsCAS, manifest: TreeManifest) -> Digest:
    d = manifest.digest()
    path = cas.tree_path(d)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(manifest.to_json()))
    return d


def _link(name: str, target: str) -> TreeEntry:
    return TreeEntry(name, "symlink", None, 0, False, target)


def _no_exdev(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def cross_device(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        calls.append(os.fspath(dst))
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "link", cross_device)
    return calls


@pytest.fixture
def tree(cas: FsCAS, tmp_path: Path) -> Digest:
    return cas.put_tree(sample_tree(tmp_path / "src"))


# R7
@pytest.mark.parametrize(
    ("case", "expect"),
    [
        ("small-file", "copy"),
        ("large-file", "symlink"),
        ("tree-same-fs", "hardlink"),
        ("tree-cross-fs", "copy-or-symlink"),
    ],
)
def test_auto_modes(
    cas: FsCAS,
    tree: Digest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expect: str,
) -> None:
    dest = tmp_path / "scratch" / "out"
    if case.endswith("file"):
        data = b"s" * THRESHOLD if case == "small-file" else b"L" * (THRESHOLD + 1)
        d = cas.put_bytes(data)
        cas.materialize(d, "file", dest, copy_threshold=THRESHOLD)
        assert dest.read_bytes() == data
        if expect == "copy":
            assert not dest.is_symlink()
            assert dest.stat().st_ino != cas.blob_path(d).stat().st_ino
            assert _mode(dest) == 0o444
        else:
            assert dest.is_symlink()
            assert os.readlink(dest) == str(cas.blob_path(d))
            assert not os.access(dest, os.W_OK)
        return

    if case == "tree-cross-fs":
        _no_exdev(monkeypatch)
    cas.materialize(tree, "tree", dest, copy_threshold=THRESHOLD)
    small = dest / "sub" / "b.txt"
    large = dest / "sub" / "deeper" / "c.bin"
    blob_small = cas.blob_path(hash_bytes(b"bravo!"))
    if expect == "hardlink":
        assert small.stat().st_ino == blob_small.stat().st_ino
        assert large.stat().st_ino == cas.blob_path(hash_bytes(bytes(range(256)) * 8)).stat().st_ino
    else:
        assert not small.is_symlink()
        assert small.stat().st_ino != blob_small.stat().st_ino
        assert _mode(small) == 0o444
        assert large.is_symlink()
        assert Path(os.readlink(large)).is_relative_to(cas.blob_path(hash_bytes(b"x")).parents[3])
    assert small.read_bytes() == b"bravo!"
    assert large.read_bytes() == bytes(range(256)) * 8


# R7
def test_tree_contents_roundtrip(cas: FsCAS, tree: Digest, tmp_path: Path) -> None:
    dest = tmp_path / "out"
    cas.materialize(tree, "tree", dest)
    src = tmp_path / "src"
    assert sorted(_files(dest)) == sorted(_files(src))
    assert (dest / "sub" / "empty").is_dir()
    assert os.readlink(dest / "link") == "sub/b.txt"
    assert (dest / "link").read_bytes() == b"bravo!"
    for rel, p in _files(src).items():
        if not p.is_symlink():
            assert (dest / rel).read_bytes() == p.read_bytes()
    # Materializing again elsewhere reproduces the same tree digest.
    assert FsCAS(tmp_path / "cas2", "test").put_tree(dest) == tree


# R7
def test_cross_fs_link_attempted_once(
    cas: FsCAS, tree: Digest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _no_exdev(monkeypatch)
    cas.materialize(tree, "tree", tmp_path / "out", copy_threshold=THRESHOLD)
    assert len(calls) == 1


# R7
@pytest.mark.parametrize("mode", ["copy", "hardlink", "symlink"])
def test_explicit_file_modes(cas: FsCAS, tmp_path: Path, mode: str) -> None:
    d = cas.put_bytes(b"payload")
    dest = tmp_path / "out"
    cas.materialize(d, "file", dest, mode=mode)  # type: ignore[arg-type]
    assert dest.read_bytes() == b"payload"
    blob = cas.blob_path(d)
    assert dest.is_symlink() == (mode == "symlink")
    assert (dest.stat().st_ino == blob.stat().st_ino) == (mode != "copy")
    assert not os.access(dest, os.W_OK) or os.geteuid() == 0


# R7
@pytest.mark.parametrize("mode", ["copy", "symlink"])
def test_explicit_tree_modes(cas: FsCAS, tree: Digest, tmp_path: Path, mode: str) -> None:
    dest = tmp_path / "out"
    cas.materialize(tree, "tree", dest, mode=mode, copy_threshold=0)  # type: ignore[arg-type]
    for name in ("a.txt", "sub/b.txt", "sub/deeper/c.bin"):
        assert (dest / name).is_symlink() == (mode == "symlink")
    assert os.readlink(dest / "link") == "sub/b.txt"


# R7
@pytest.mark.parametrize("mode", ["auto", "copy", "hardlink", "symlink"])
def test_exec_bits(cas: FsCAS, tree: Digest, tmp_path: Path, mode: str) -> None:
    dest = tmp_path / "out"
    cas.materialize(tree, "tree", dest, mode=mode, copy_threshold=0)  # type: ignore[arg-type]
    run = dest / "run.sh"
    assert not run.is_symlink()
    assert _mode(run) == 0o555
    for name in ("a.txt", "sub/b.txt"):
        assert not os.stat(dest / name).st_mode & 0o111
    # The shared blob itself never becomes executable or writable.
    blob = cas.blob_path(hash_bytes(b"#!/bin/sh\necho hi\n"))
    assert _mode(blob) == 0o444


# R7
def test_readonly_files_writable_dirs(cas: FsCAS, tree: Digest, tmp_path: Path) -> None:
    dest = tmp_path / "out"
    cas.materialize(tree, "tree", dest, mode="copy")
    for d in (dest, dest / "sub", dest / "sub" / "empty"):
        assert _mode(d) & stat.S_IWUSR
    for name in ("a.txt", "sub/deeper/c.bin"):
        assert _mode(dest / name) == 0o444


# R7
@pytest.mark.parametrize("mode", ["auto", "hardlink", "symlink"])
def test_writable_copy(cas: FsCAS, tree: Digest, tmp_path: Path, mode: str) -> None:
    dest = tmp_path / "out"
    cas.materialize(tree, "tree", dest, mode=mode, copy_threshold=0, writable=True)  # type: ignore[arg-type]
    for rel, p in _files(dest).items():
        if rel == "link":
            continue
        assert not p.is_symlink()
        assert p.stat().st_nlink == 1  # its own inode, not the CAS blob
        assert _mode(p) == (0o755 if rel == "run.sh" else 0o644)
    (dest / "sub" / "b.txt").write_bytes(b"edited")
    assert cas.verify(hash_bytes(b"bravo!"))
    assert cas.blob_path(hash_bytes(b"bravo!")).read_bytes() == b"bravo!"

    single = tmp_path / "single"
    d = cas.put_bytes(b"x" * 10)
    cas.materialize(d, "file", single, mode="symlink", writable=True)
    assert not single.is_symlink()
    assert _mode(single) == 0o644


# R7 + P0-03 note: symlinks are created last, after every file and directory.
def test_symlinks_created_last(
    cas: FsCAS, tree: Digest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    real = {name: getattr(os, name) for name in ("symlink", "mkdir", "open", "link")}

    def spy(name: str) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            events.append(name)
            return real[name](*args, **kwargs)

        return call

    for name in real:
        monkeypatch.setattr(os, name, spy(name))
    cas.materialize(tree, "tree", tmp_path / "out", copy_threshold=THRESHOLD)
    monkeypatch.undo()
    first_symlink = events.index("symlink")
    assert all(e == "symlink" for e in events[first_symlink:]), events
    # c.bin (large) under auto + same fs is a hard link, so the only symlink is the tree's own.
    assert events.count("symlink") == 1


# R7 + P0-03 note
@pytest.mark.parametrize(
    "entries_builder",
    [
        pytest.param("lexical", id="lexical-escape"),
        pytest.param("chain", id="chain-escape"),
    ],
)
def test_escaping_symlinks_rejected(cas: FsCAS, tmp_path: Path, entries_builder: str) -> None:
    if entries_builder == "lexical":
        sub = _store_manifest(cas, TreeManifest((_link("evil", "../../outside"),)))
        root = TreeManifest((TreeEntry("sub", "dir", sub, 0, False, None),))
    else:
        # Each link stays inside lexically; the chain x -> sub/up/.. -> <dest>/.. escapes.
        sub = _store_manifest(cas, TreeManifest((_link("up", ".."),)))
        root = TreeManifest((TreeEntry("sub", "dir", sub, 0, False, None), _link("x", "sub/up/..")))
    d = _store_manifest(cas, root)
    dest = tmp_path / "scratch" / "out"
    with pytest.raises(CasError, match="escapes"):
        cas.materialize(d, "tree", dest)
    assert not dest.exists()
    assert not dest.is_symlink()
    assert list((tmp_path / "scratch").iterdir()) == []


# R7
def test_inner_links_allowed(cas: FsCAS, tmp_path: Path) -> None:
    sub = _store_manifest(cas, TreeManifest((_link("dangling", "nothere"), _link("up", ".."))))
    root = TreeManifest((TreeEntry("sub", "dir", sub, 0, False, None), _link("x", "sub/up/sub")))
    dest = tmp_path / "out"
    cas.materialize(_store_manifest(cas, root), "tree", dest)
    assert os.path.realpath(dest / "x") == os.path.realpath(dest / "sub")


def test_destination_must_not_exist(cas: FsCAS, tree: Digest, tmp_path: Path) -> None:
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(CasError, match="already exists"):
        cas.materialize(tree, "tree", dest)
    (tmp_path / "dangling").symlink_to("nowhere")
    with pytest.raises(CasError, match="already exists"):
        cas.materialize(cas.put_bytes(b"x"), "file", tmp_path / "dangling")


def test_missing_objects(cas: FsCAS, tmp_path: Path) -> None:
    missing = hash_bytes(b"missing")
    for kind in ("file", "tree"):
        with pytest.raises(CasError, match="not found"):
            cas.materialize(missing, kind, tmp_path / kind)
        assert not (tmp_path / kind).exists()


def test_tree_missing_blob_cleans_up(cas: FsCAS, tree: Digest, tmp_path: Path) -> None:
    cas.delete(hash_bytes(b"delta"))
    dest = tmp_path / "out"
    with pytest.raises(CasError, match="not found"):
        cas.materialize(tree, "tree", dest, mode="copy")
    assert not dest.exists()


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"mode": "move"}, "mode"),
        ({"kind": "dir"}, "kind"),
        ({"copy_threshold": -1}, "copy_threshold"),
    ],
)
def test_invalid_arguments(cas: FsCAS, tmp_path: Path, kwargs: dict[str, Any], match: str) -> None:
    args: dict[str, Any] = {"kind": "file", **kwargs}
    kind = args.pop("kind")
    with pytest.raises(CasError, match=match):
        cas.materialize(cas.put_bytes(b"x"), kind, tmp_path / "out", **args)


def test_explicit_hardlink_cross_fs_fails(
    cas: FsCAS, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = cas.put_bytes(b"x")
    _no_exdev(monkeypatch)
    with pytest.raises(CasError, match="cross-device"):
        cas.materialize(d, "file", tmp_path / "out", mode="hardlink")
    assert not (tmp_path / "out").exists()


# R7: a blob at its hard-link limit is copied; linking continues for the others.
def test_link_limit_falls_back_per_file(
    cas: FsCAS, tree: Digest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_link = os.link
    full = cas.blob_path(hash_bytes(b"alpha"))

    def link(src: Any, dst: Any) -> None:
        if Path(src) == full:
            raise OSError(errno.EMLINK, "Too many links")
        real_link(src, dst)

    monkeypatch.setattr(os, "link", link)
    dest = tmp_path / "out"
    cas.materialize(tree, "tree", dest, copy_threshold=THRESHOLD)
    assert (dest / "a.txt").stat().st_ino != full.stat().st_ino
    assert _mode(dest / "a.txt") == 0o444
    assert (dest / "sub" / "b.txt").stat().st_ino == cas.blob_path(
        hash_bytes(b"bravo!")
    ).stat().st_ino


def test_unexpected_link_error_reported(
    cas: FsCAS, tree: Digest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(src: Any, dst: Any) -> None:
        raise OSError(errno.EACCES, "Permission denied", os.fspath(dst))

    monkeypatch.setattr(os, "link", denied)
    dest = tmp_path / "out"
    with pytest.raises(CasError, match="Permission denied"):
        cas.materialize(tree, "tree", dest)
    assert not dest.exists()


def test_partial_file_removed(cas: FsCAS, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(fd: int, mode: int) -> None:
        raise OSError(errno.EIO, "Input/output error")

    d = cas.put_bytes(b"x" * 10)
    monkeypatch.setattr(os, "fchmod", fail)
    dest = tmp_path / "out"
    with pytest.raises(CasError, match="Input/output error"):
        cas.materialize(d, "file", dest, mode="copy")
    assert not os.path.lexists(dest)


class _RemoteCAS:
    """A backend without local paths (like S3 later): only streams can be copied."""

    def __init__(self, inner: FsCAS) -> None:
        self._inner = inner
        self.domain = inner.domain

    def local_path(self, d: Digest) -> Path | None:
        return None

    def open(self, d: Digest) -> BinaryIO:
        return self._inner.open(d)

    def get_tree(self, d: Digest) -> TreeManifest:
        return self._inner.get_tree(d)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


# R7
def test_backend_without_local_paths(cas: FsCAS, tree: Digest, tmp_path: Path) -> None:
    remote = _RemoteCAS(cas)
    materialize(remote, tree, "tree", tmp_path / "out", copy_threshold=0)
    assert not (tmp_path / "out" / "sub" / "deeper" / "c.bin").is_symlink()
    assert (tmp_path / "out" / "a.txt").stat().st_nlink == 1
    for mode in ("hardlink", "symlink"):
        with pytest.raises(CasError, match="local path"):
            materialize(remote, cas.put_bytes(b"x"), "file", tmp_path / mode, mode=mode)


# R7 + P0-03 note: symlink cycles stay inside and are materialized as-is.
def test_symlink_cycle_is_not_an_escape(cas: FsCAS, tmp_path: Path) -> None:
    root = TreeManifest((_link("a", "b"), _link("b", "a")))
    dest = tmp_path / "out"
    cas.materialize(_store_manifest(cas, root), "tree", dest)
    assert os.readlink(dest / "a") == "b"


# R7: regression — a per-branch hop limit took 4**40 steps on links naming other links repeatedly.
def test_link_fanout_resolves_in_bounded_time(cas: FsCAS, tmp_path: Path) -> None:
    fanout = "/".join(["a"] * 8)
    root = TreeManifest((_link("a", fanout), _link("b", f"{fanout}/../..")))
    dest = tmp_path / "out"
    start = time.monotonic()
    cas.materialize(_store_manifest(cas, root), "tree", dest)
    assert time.monotonic() - start < 5
    assert os.readlink(dest / "b") == f"{fanout}/../.."


_PARTS = st.sampled_from(["..", ".", "d", "e", "l0", "l1", "l2"])


@st.composite
def _link_farms(draw: st.DrawFn) -> tuple[list[str], dict[str, str]]:
    dirs = draw(st.lists(st.sampled_from(["d", "d/e", "e", "e/d"]), unique=True, max_size=3))
    homes = ["", *dirs]
    links: dict[str, str] = {}
    for i in range(draw(st.integers(min_value=1, max_value=3))):
        home = draw(st.sampled_from(homes))
        target = "/".join(draw(st.lists(_PARTS, min_size=1, max_size=4)))
        links[f"{home}/l{i}".lstrip("/")] = target
    return dirs, links


# R7: the logical escape check agrees with the kernel's resolution on disk.
@settings(
    max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(farm=_link_farms())
def test_resolve_matches_kernel(tmp_path: Path, farm: tuple[list[str], dict[str, str]]) -> None:
    dirs, links = farm
    with tempfile.TemporaryDirectory(dir=tmp_path) as work:
        root = Path(work) / "outer" / "root"
        root.mkdir(parents=True)
        for d in sorted(dirs):
            (root / d).mkdir(parents=True, exist_ok=True)
        for rel, target in links.items():
            (root / rel).symlink_to(target)
        table = {tuple(rel.split("/")): target for rel, target in links.items()}
        real_root = os.path.realpath(root)
        for rel, target in links.items():
            try:
                os.stat(root / rel)
            except OSError as exc:
                assume(exc.errno != errno.ELOOP)
            on_disk = os.path.realpath(root / rel)
            disk_escapes = os.path.commonpath([real_root, on_disk]) != real_root
            key = tuple(rel.split("/"))
            assert (_resolve(key[:-1], target, table) is None) == disk_escapes, (rel, target)


# R7: the link budget is the kernel's: a 40-link chain resolves (and here escapes), 41 is ELOOP.
@pytest.mark.parametrize("n", [39, 40, 41])
def test_hop_budget_matches_kernel(cas: FsCAS, tmp_path: Path, n: int) -> None:
    chain = {f"l{i:02d}": f"l{i + 1:02d}" for i in range(n - 1)}
    chain[f"l{n - 1:02d}"] = ".."
    root = tmp_path / "disk" / "root"
    root.mkdir(parents=True)
    for name, target in chain.items():
        (root / name).symlink_to(target)
    kernel_resolves = os.path.exists(root / "l00")  # False on ELOOP; ".." itself exists
    assert kernel_resolves == (n <= 40)
    table: dict[tuple[str, ...], str] = {(name,): target for name, target in chain.items()}
    assert (_resolve((), "l00", table) is None) == kernel_resolves

    manifest = TreeManifest(tuple(_link(name, target) for name, target in sorted(chain.items())))
    dest = tmp_path / "out"
    # The chain's last link alone escapes, so the tree is always rejected; the budget decides
    # only whether l00 is flagged too, which the direct check above pins down.
    with pytest.raises(CasError, match="escapes"):
        cas.materialize(_store_manifest(cas, manifest), "tree", dest)
    assert not os.path.lexists(dest)


# R7 + P0-03 note: a copy never writes through a symlink planted at its destination.
def test_copy_never_writes_through_planted_symlink(
    cas: FsCAS, tree: Digest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "out"
    outside = tmp_path / "outside"
    real_mkdir = os.mkdir

    def mkdir_then_plant(path: Any, *args: Any, **kwargs: Any) -> None:
        real_mkdir(path, *args, **kwargs)
        if Path(path) == dest / "sub":
            (dest / "sub" / "b.txt").symlink_to(outside)  # a concurrent writer's trap

    monkeypatch.setattr(os, "mkdir", mkdir_then_plant)
    with pytest.raises(CasError, match="exists"):
        cas.materialize(tree, "tree", dest, mode="copy")
    assert not os.path.lexists(outside)
    assert not os.path.lexists(dest)


# R7: cleanup after a failure removes only what this call created.
def test_failure_keeps_dest_created_by_someone_else(
    cas: FsCAS, tree: Digest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "out"
    real_mkdir, real_open = os.mkdir, os.open

    def other_wins_mkdir(path: Any, *args: Any, **kwargs: Any) -> None:
        if Path(path) == dest:
            real_mkdir(path)
            (dest / "theirs").write_bytes(b"keep")
        real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", other_wins_mkdir)
    with pytest.raises(CasError, match="exists"):
        cas.materialize(tree, "tree", dest)
    assert (dest / "theirs").read_bytes() == b"keep"
    monkeypatch.undo()

    single = tmp_path / "single"

    def other_wins_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if Path(path) == single:
            single.write_bytes(b"theirs")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", other_wins_open)
    with pytest.raises(CasError, match="exists"):
        cas.materialize(cas.put_bytes(b"mine"), "file", single, mode="copy")
    assert single.read_bytes() == b"theirs"


# R7: EPERM (protected_hardlinks on a foreign blob) falls back for that file only.
def test_eperm_falls_back_per_file(
    cas: FsCAS, tree: Digest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_link = os.link
    foreign = cas.blob_path(hash_bytes(b"alpha"))

    def link(src: Any, dst: Any) -> None:
        if Path(src) == foreign:
            raise OSError(errno.EPERM, "Operation not permitted")
        real_link(src, dst)

    monkeypatch.setattr(os, "link", link)
    dest = tmp_path / "out"
    cas.materialize(tree, "tree", dest, copy_threshold=THRESHOLD)
    assert (dest / "a.txt").stat().st_ino != foreign.stat().st_ino
    bravo = cas.blob_path(hash_bytes(b"bravo!"))
    assert (dest / "sub" / "b.txt").stat().st_ino == bravo.stat().st_ino
