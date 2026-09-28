from __future__ import annotations

import os
import socket
import stat
import sys
import unicodedata
from collections.abc import Callable, Iterator
from pathlib import Path
from types import TracebackType

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ebs.core.canon import digest_json
from ebs.core.digest import Digest, hash_bytes, hash_file
from ebs.core.errors import TreeError
from ebs.core.tree import TreeEntry, TreeManifest, _kind, build_tree
from ebs.core.types import JsonValue

Hasher = Callable[[Path], tuple[Digest, int]]
D1 = hash_bytes(b"one")
D2 = hash_bytes(b"two")
NON_NFC = "e" + chr(0x301)  # "e" + COMBINING ACUTE ACCENT; NFC form is U+00E9


def _write(path: Path, data: bytes, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o755 if executable else 0o644)


def _sample_tree(root: Path) -> Path:
    _write(root / "a.txt", b"alpha")
    _write(root / "run.sh", b"#!/bin/sh\n", executable=True)
    _write(root / "sub" / "b.txt", b"bravo!")
    _write(root / "sub" / "deeper" / "c.bin", bytes(range(10)))
    (root / "sub" / "empty").mkdir()
    (root / "link").symlink_to("sub/b.txt")
    return root


def _tree(root: Path, hasher: Hasher = hash_file) -> tuple[Digest, dict[Digest, TreeManifest]]:
    return build_tree(root, hasher=hasher)


def _entry(manifest: TreeManifest, name: str) -> TreeEntry:
    (entry,) = [e for e in manifest.entries if e.name == name]
    return entry


# --- R1 -----------------------------------------------------------------------------------------


# R1
def test_nested_manifests_returned(tmp_path: Path) -> None:
    root_digest, manifests = _tree(_sample_tree(tmp_path / "t"))
    root = manifests[root_digest]
    assert [e.name for e in root.entries] == ["a.txt", "link", "run.sh", "sub"]
    sub = manifests[_entry(root, "sub").digest]  # type: ignore[index]
    assert [e.name for e in sub.entries] == ["b.txt", "deeper", "empty"]
    deeper = manifests[_entry(sub, "deeper").digest]  # type: ignore[index]
    empty = manifests[_entry(sub, "empty").digest]  # type: ignore[index]
    assert [e.name for e in deeper.entries] == ["c.bin"]
    assert empty.entries == ()
    assert len(manifests) == 4
    for digest, manifest in manifests.items():
        assert manifest.digest() == digest
        assert digest_json(manifest.to_json()) == digest
    assert _entry(root, "a.txt") == TreeEntry(
        name="a.txt",
        type="file",
        digest=hash_bytes(b"alpha"),
        size=5,
        executable=False,
        target=None,
    )


# R1
def test_entries_sorted_by_utf8_bytes(tmp_path: Path) -> None:
    root = tmp_path / "t"
    # UTF-8 byte order: "B" < "a" < U+00E9 (C3 A9) < U+FF21 (EF BC A1) < U+1F600 (F0 ...)
    names = ["a", "B", chr(0xE9), chr(0xFF21), chr(0x1F600)]
    for name in reversed(names):
        _write(root / name, name.encode())
    root_digest, manifests = _tree(root)
    assert [e.name for e in manifests[root_digest].entries] == sorted(
        names, key=lambda n: n.encode("utf-8")
    )
    assert [e.name for e in manifests[root_digest].entries] == [
        "B",
        "a",
        chr(0xE9),
        chr(0xFF21),
        chr(0x1F600),
    ]


# --- R2 / I5 ------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def shared_tree(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Digest]:
    root = _sample_tree(tmp_path_factory.mktemp("meta") / "t")
    return root, _tree(root)[0]


# R2 / I5
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=50)
@given(
    times=st.lists(st.integers(min_value=0, max_value=2**31), min_size=4, max_size=4),
    extra_bits=st.lists(
        st.sampled_from([0, 0o020, 0o002, 0o040, 0o004, 0o070, 0o007]), min_size=3, max_size=3
    ),
    user_write=st.booleans(),
)
def test_metadata_independence(
    shared_tree: tuple[Path, Digest], times: list[int], extra_bits: list[int], user_write: bool
) -> None:
    root, expected = shared_tree
    files = [root / "a.txt", root / "run.sh", root / "sub" / "b.txt"]
    try:
        for path, bits in zip(files, extra_bits, strict=True):
            user = stat.S_IRUSR | (stat.S_IWUSR if user_write else 0)
            user |= stat.S_IXUSR if path.name == "run.sh" else 0
            path.chmod(user | bits)
        for path, t in zip([*files, root / "sub"], times, strict=True):
            os.utime(path, (t, t))
        (root / "sub").chmod(0o700 | extra_bits[0])
        assert _tree(root)[0] == expected
    finally:
        for path in files:
            path.chmod(0o755 if path.name == "run.sh" else 0o644)
        (root / "sub").chmod(0o755)


# R2 / I5: the executable bit and content are part of the identity, other bits are not.
def test_executable_bit_and_content_matter(tmp_path: Path) -> None:
    root = _sample_tree(tmp_path / "t")
    base = _tree(root)[0]
    (root / "a.txt").chmod(0o744)
    with_x = _tree(root)[0]
    assert with_x != base
    (root / "a.txt").chmod(0o644)
    (root / "a.txt").chmod(0o654)  # group-execute only: not recorded
    assert _tree(root)[0] == base
    (root / "sub" / "b.txt").write_bytes(b"bravo?")
    assert _tree(root)[0] != base


# R2 / I5
def test_symlink_target_matters(tmp_path: Path) -> None:
    root = _sample_tree(tmp_path / "t")
    base = _tree(root)[0]
    (root / "link").unlink()
    (root / "link").symlink_to("a.txt")
    assert _tree(root)[0] != base


class _ReversedScandir:
    def __init__(self, entries: list[os.DirEntry[str]]) -> None:
        self._entries = entries

    def __enter__(self) -> Iterator[os.DirEntry[str]]:
        return iter(self._entries)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def __iter__(self) -> Iterator[os.DirEntry[str]]:
        return iter(self._entries)


# R2 / I5
@pytest.mark.parametrize("order", ["forward", "reversed"])
def test_scandir_order_independence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, order: str
) -> None:
    root = _sample_tree(tmp_path / "t")
    for i in range(20):
        _write(root / "many" / f"f{i:02d}", str(i).encode())
    expected = _tree(root)
    real_scandir = os.scandir
    calls: list[str] = []

    def fake_scandir(path: str | os.PathLike[str]) -> _ReversedScandir:
        calls.append(os.fspath(path))
        with real_scandir(path) as it:
            entries = sorted(it, key=lambda e: e.name, reverse=order == "reversed")
        return _ReversedScandir(entries)

    monkeypatch.setattr(os, "scandir", fake_scandir)
    assert _tree(root) == expected
    assert calls, "build_tree must enumerate directories with os.scandir"


# --- R3 -----------------------------------------------------------------------------------------


# R3
def test_symlink_recorded_not_followed(tmp_path: Path) -> None:
    root = tmp_path / "t"
    _write(root / "real" / "big.bin", b"x" * 100)
    (root / "dirlink").symlink_to("real")
    (root / "dangling").symlink_to("nowhere/at/all")
    seen: list[Path] = []

    def hasher(path: Path) -> tuple[Digest, int]:
        seen.append(path)
        return hash_file(path)

    root_digest, manifests = _tree(root, hasher)
    top = manifests[root_digest]
    assert _entry(top, "dirlink") == TreeEntry(
        name="dirlink", type="symlink", digest=None, size=0, executable=False, target="real"
    )
    assert _entry(top, "dangling").target == "nowhere/at/all"
    assert seen == [root / "real" / "big.bin"]  # the link to "real" is not descended into
    assert len(manifests) == 2
    assert top.digest() == root_digest


# R3
@pytest.mark.parametrize(
    ("link", "target"),
    [
        pytest.param("abs", "/etc/passwd", id="abs"),
        pytest.param("dotdot", "../outside", id="dotdot"),
        pytest.param("sub/nested", "../../outside", id="nested-dotdot"),
        pytest.param("sub/sneaky", "x/../../../outside", id="dotdot-after-descend"),
        pytest.param("esc-dot", "./../outside", id="dot-then-dotdot"),
        pytest.param("esc-dslash", "a//../../outside", id="double-slash"),
    ],
)
def test_symlink_escape_rejected(tmp_path: Path, link: str, target: str) -> None:
    root = tmp_path / "t"
    (root / "sub").mkdir(parents=True)
    (root / link).symlink_to(target)
    with pytest.raises(TreeError) as excinfo:
        _tree(root)
    message = str(excinfo.value)
    assert str(root / link) in message
    assert target in message


# R3: these resolve back inside today, but only through the root's own name, so the tree would
# point elsewhere once materialized under another directory; the lexical check rejects them.
@pytest.mark.parametrize(
    "template",
    ["../{root}/a.txt", "./../{root}/a.txt", "a//../../{root}/a.txt", "sub/.././../{root}"],
)
def test_symlink_through_root_name_rejected(tmp_path: Path, template: str) -> None:
    root = tmp_path / "t"
    _write(root / "a.txt", b"a")
    (root / "sub").mkdir()
    (root / "l").symlink_to(template.format(root=root.name))
    with pytest.raises(TreeError, match="escapes the tree root"):
        _tree(root)


# R3: every link stays inside lexically, but following the chain leaves the root.
def test_symlink_chain_escape_rejected(tmp_path: Path) -> None:
    root = tmp_path / "t"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "up").symlink_to("..")
    (root / "x").symlink_to("sub/up/..")
    assert (root / "x").resolve() == tmp_path  # the escape is real
    with pytest.raises(TreeError) as excinfo:
        _tree(root)
    assert str(root / "x") in str(excinfo.value)
    assert "outside the tree root" in str(excinfo.value)


# R3
def test_symlink_loop_is_recorded(tmp_path: Path) -> None:
    root = tmp_path / "t"
    root.mkdir()
    (root / "a").symlink_to("b")
    (root / "b").symlink_to("a")
    root_digest, manifests = _tree(root)
    assert [e.target for e in manifests[root_digest].entries] == ["b", "a"]


# R3
@pytest.mark.parametrize(
    ("link", "target"),
    [
        ("d-up", "d/../a.txt"),
        ("dotsname", "..foo"),
        ("sub/up", ".."),
        ("sub/sib", "../a.txt"),
        ("dot", "./a.txt"),
        ("sub/x", "y/./../z"),
    ],
)
def test_symlink_inside_tree_accepted(tmp_path: Path, link: str, target: str) -> None:
    root = tmp_path / "t"
    _write(root / "a.txt", b"a")
    (root / "sub").mkdir()
    (root / link).symlink_to(target)
    root_digest, manifests = _tree(root)
    assert root_digest in manifests


# --- R4 -----------------------------------------------------------------------------------------


def _make_fifo(path: Path) -> None:
    os.mkfifo(path)


def _make_socket(path: Path) -> None:
    # AF_UNIX paths are limited to ~108 bytes, so bind relative to the directory.
    cwd = Path.cwd()
    os.chdir(path.parent)
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(path.name)
        sock.close()
    finally:
        os.chdir(cwd)


# R4
@pytest.mark.parametrize("make", [_make_fifo, _make_socket], ids=["fifo", "socket"])
def test_special_files_rejected(tmp_path: Path, make: Callable[[Path], None]) -> None:
    root = tmp_path / "t"
    (root / "sub").mkdir(parents=True)
    make(root / "sub" / "special")
    with pytest.raises(TreeError) as excinfo:
        _tree(root)
    assert str(root / "sub" / "special") in str(excinfo.value)


# R4: device nodes need root to create, so the message helper is checked directly.
@pytest.mark.parametrize(
    ("mode", "label"),
    [
        (stat.S_IFIFO, "FIFO"),
        (stat.S_IFSOCK, "socket"),
        (stat.S_IFCHR, "device"),
        (stat.S_IFBLK, "device"),
        (0o160000, "mode"),  # S_IFWHT (BSD whiteout): an unknown type on Linux
    ],
)
def test_special_file_kind_labels(mode: int, label: str) -> None:
    assert label in _kind(mode)


# R4
def test_non_utf8_name_rejected(tmp_path: Path) -> None:
    root = tmp_path / "t"
    root.mkdir()
    try:
        fd = os.open(os.path.join(os.fsencode(root), b"bad\xffname"), os.O_CREAT | os.O_WRONLY)
    except OSError:  # pragma: no cover - filesystems that enforce UTF-8 names
        pytest.skip("filesystem does not allow non-UTF-8 names")
    os.close(fd)
    with pytest.raises(TreeError, match="UTF-8") as excinfo:
        _tree(root)
    assert str(root) in str(excinfo.value)


# R4
def test_non_nfc_name_rejected(tmp_path: Path) -> None:
    root = tmp_path / "t"
    _write(root / "sub" / NON_NFC, b"x")
    with pytest.raises(TreeError) as excinfo:
        _tree(root)
    message = str(excinfo.value)
    assert "NFC" in message
    assert "rename" in message
    assert str(root / "sub") in message


# R4
def test_non_nfc_symlink_target_rejected(tmp_path: Path) -> None:
    root = tmp_path / "t"
    root.mkdir()
    (root / "l").symlink_to(NON_NFC)
    with pytest.raises(TreeError, match="NFC"):
        _tree(root)


# R4
def test_hard_links_are_files(tmp_path: Path) -> None:
    root = tmp_path / "t"
    _write(root / "orig", b"shared")
    os.link(root / "orig", root / "alias")
    root_digest, manifests = _tree(root)
    top = manifests[root_digest]
    assert _entry(top, "alias").type == "file"
    assert _entry(top, "alias").digest == _entry(top, "orig").digest == hash_bytes(b"shared")


# R4
@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_unreadable_directory_raises_tree_error(tmp_path: Path) -> None:
    root = tmp_path / "t"
    (root / "locked").mkdir(parents=True)
    (root / "locked").chmod(0)
    try:
        with pytest.raises(TreeError) as excinfo:
            _tree(root)
    finally:
        (root / "locked").chmod(0o755)
    assert str(root / "locked") in str(excinfo.value)
    assert "permissions" in str(excinfo.value)


# R4
@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_unreadable_root_raises_tree_error(tmp_path: Path) -> None:
    root = tmp_path / "t"
    root.mkdir()
    root.chmod(0o300)  # searchable and writable, not listable
    try:
        with pytest.raises(TreeError, match="cannot read") as excinfo:
            _tree(root)
    finally:
        root.chmod(0o755)
    assert str(root) in str(excinfo.value)


# R4
def test_hasher_os_errors_become_tree_errors(tmp_path: Path) -> None:
    root = tmp_path / "t"
    _write(root / "f", b"x")

    def vanished(path: Path) -> tuple[Digest, int]:
        raise FileNotFoundError(2, "No such file or directory", str(path))

    with pytest.raises(TreeError, match="cannot read"):
        _tree(root, vanished)


# R4
def test_excessive_nesting_raises_tree_error(tmp_path: Path) -> None:
    root = tmp_path / "t"
    deep = root.joinpath(*["d"] * 120)
    deep.mkdir(parents=True)
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(100)
    try:
        with pytest.raises(TreeError, match="nested too deeply"):
            _tree(root)
    finally:
        sys.setrecursionlimit(limit)


# R4
def test_root_must_be_a_directory(tmp_path: Path) -> None:
    _write(tmp_path / "file", b"x")
    with pytest.raises(TreeError, match="not a directory"):
        _tree(tmp_path / "file")
    with pytest.raises(TreeError, match="not a directory"):
        _tree(tmp_path / "missing")


# --- R5 / R7 / R8 -------------------------------------------------------------------------------


# R5
def test_empty_dir_distinct(tmp_path: Path) -> None:
    with_empty = tmp_path / "with"
    without = tmp_path / "without"
    _write(with_empty / "f", b"x")
    _write(without / "f", b"x")
    (with_empty / "e").mkdir()
    d_with, manifests = _tree(with_empty)
    d_without, _ = _tree(without)
    assert d_with != d_without
    empty_entry = _entry(manifests[d_with], "e")
    assert empty_entry.type == "dir"
    assert empty_entry.size == 0
    assert manifests[empty_entry.digest] == TreeManifest(entries=())  # type: ignore[index]
    # An empty file is not an empty directory either.
    (with_empty / "e").rmdir()
    (with_empty / "e").touch()
    assert _tree(with_empty)[0] not in {d_with, d_without}


# R7
def test_dir_size_sum(tmp_path: Path) -> None:
    root_digest, manifests = _tree(_sample_tree(tmp_path / "t"))
    top = manifests[root_digest]
    sub = manifests[_entry(top, "sub").digest]  # type: ignore[index]
    assert _entry(sub, "deeper").size == 10
    assert _entry(sub, "empty").size == 0
    assert _entry(top, "sub").size == 6 + 10
    assert _entry(top, "link").size == 0
    assert sum(e.size for e in top.entries) == 5 + 10 + 6 + 10


# R8
def test_hasher_injected_is_used(tmp_path: Path) -> None:
    root = _sample_tree(tmp_path / "t")
    calls: list[Path] = []

    def fake(path: Path) -> tuple[Digest, int]:
        calls.append(path)
        return hash_bytes(path.name.encode()), 1000

    root_digest, manifests = _tree(root, fake)
    assert sorted(calls) == sorted(
        [root / "a.txt", root / "run.sh", root / "sub" / "b.txt", root / "sub/deeper/c.bin"]
    )
    top = manifests[root_digest]
    assert _entry(top, "a.txt").digest == hash_bytes(b"a.txt")
    assert _entry(top, "a.txt").size == 1000
    assert _entry(top, "sub").size == 2000
    assert root_digest != _tree(root)[0]


# --- R6 -----------------------------------------------------------------------------------------

names = (
    st.text(
        alphabet=st.characters(exclude_categories=["Cs"], exclude_characters="/\x00"),
        min_size=1,
        max_size=8,
    )
    .map(lambda s: unicodedata.normalize("NFC", s))
    .filter(lambda s: s not in {"", ".", ".."} and "/" not in s and "\x00" not in s)
)
digests = st.binary(min_size=32, max_size=32).map(lambda b: Digest("sha256", b.hex()))
sizes = st.integers(min_value=0, max_value=2**53 - 1)
targets = st.lists(names, min_size=1, max_size=3).map("/".join)


@st.composite
def entries(draw: st.DrawFn, name: str) -> TreeEntry:
    kind = draw(st.sampled_from(["file", "dir", "symlink"]))
    if kind == "file":
        return TreeEntry(name, "file", draw(digests), draw(sizes), draw(st.booleans()), None)
    if kind == "dir":
        return TreeEntry(name, "dir", draw(digests), draw(sizes), False, None)
    return TreeEntry(name, "symlink", None, 0, False, draw(targets))


@st.composite
def manifests(draw: st.DrawFn) -> TreeManifest:
    unique = draw(st.lists(names, max_size=6, unique=True))
    unique.sort(key=lambda n: n.encode("utf-8"))
    return TreeManifest(entries=tuple(draw(entries(n)) for n in unique))


# R6
@given(manifest=manifests())
def test_json_roundtrip(manifest: TreeManifest) -> None:
    assert TreeManifest.from_json(manifest.to_json()) == manifest
    assert manifest.digest() == digest_json(manifest.to_json())


# R6
def test_to_json_shape() -> None:
    manifest = TreeManifest(
        entries=(
            TreeEntry("d", "dir", D1, 3, False, None),
            TreeEntry("f", "file", D2, 3, True, None),
            TreeEntry("l", "symlink", None, 0, False, "f"),
        )
    )
    assert manifest.to_json() == {
        "v": 1,
        "entries": [
            {"name": "d", "type": "dir", "digest": str(D1), "size": 3},
            {"name": "f", "type": "file", "digest": str(D2), "size": 3, "executable": True},
            {"name": "l", "type": "symlink", "target": "f"},
        ],
    }
    assert manifest.digest().algo == "sha256"


# R6
def test_digest_algo_passed() -> None:
    pytest.importorskip("blake3", reason="missing dependency: install the `blake3` extra")
    manifest = TreeManifest(entries=())
    assert manifest.digest("blake3") == digest_json(manifest.to_json(), "blake3")
    assert manifest.digest("blake3").algo == "blake3"


def _file(name: str, **overrides: JsonValue) -> dict[str, JsonValue]:
    entry: dict[str, JsonValue] = {
        "name": name,
        "type": "file",
        "digest": str(D1),
        "size": 1,
        "executable": False,
    }
    entry.update(overrides)
    return entry


def _doc(*entries: JsonValue, **top: JsonValue) -> JsonValue:
    doc: dict[str, JsonValue] = {"v": 1, "entries": list(entries)}
    doc.update(top)
    return doc


# R6
@pytest.mark.parametrize(
    "doc",
    [
        pytest.param([], id="not-object"),
        pytest.param(_doc(v=2), id="unknown-version"),
        pytest.param(_doc(v=True), id="version-bool"),
        pytest.param(_doc(v=1.0), id="version-float"),  # type: ignore[arg-type]
        pytest.param({"entries": []}, id="missing-version"),
        pytest.param(_doc(extra=1), id="unknown-top-key"),
        pytest.param({"v": 1, "entries": {}}, id="entries-not-list"),
        pytest.param(_doc(_file("b"), _file("a")), id="unsorted"),
        pytest.param(_doc(_file("a"), _file("a")), id="duplicate"),
        pytest.param(_doc("a"), id="entry-not-object"),
        pytest.param(_doc(_file("a", type="socket")), id="unknown-type"),
        pytest.param(_doc(_file("a", digest="sha256:XYZ")), id="bad-digest"),
        pytest.param(_doc(_file("a", size=-1)), id="negative-size"),
        pytest.param(_doc(_file("a", size="1")), id="size-not-int"),
        pytest.param(_doc(_file("a", size=True)), id="size-bool"),
        pytest.param(_doc(_file("a", executable=1)), id="executable-not-bool"),
        pytest.param(_doc(_file("a", target="x")), id="file-with-target"),
        pytest.param(
            _doc({"name": "a", "type": "file", "digest": str(D1), "size": 1}),
            id="file-missing-executable",
        ),
        pytest.param(
            _doc({"name": "a", "type": "dir", "digest": str(D1), "size": 1, "executable": False}),
            id="dir-with-executable",
        ),
        pytest.param(
            _doc({"name": "a", "type": "symlink", "target": "x", "digest": str(D1)}),
            id="symlink-with-digest",
        ),
        pytest.param(_doc({"name": "a", "type": "symlink"}), id="symlink-missing-target"),
        pytest.param(_doc({"name": "a", "type": "symlink", "target": 1}), id="target-not-str"),
        pytest.param(_doc(_file("a", digest=1)), id="digest-not-str"),
        pytest.param(
            _doc({"name": "a", "type": "symlink", "target": "/abs"}), id="symlink-absolute"
        ),
        pytest.param(_doc(_file("a/b")), id="slash-in-name"),
        pytest.param(_doc(_file("..")), id="dotdot-name"),
        pytest.param(_doc(_file(NON_NFC)), id="non-nfc-name"),
        pytest.param(_doc(_file(1)), id="name-not-str"),  # type: ignore[arg-type]
    ],
)
def test_from_json_rejects(doc: JsonValue) -> None:
    with pytest.raises(TreeError):
        TreeManifest.from_json(doc)


# R6
@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"name": "", "type": "file", "digest": D1}, id="empty-name"),
        pytest.param({"name": ".", "type": "file", "digest": D1}, id="dot"),
        pytest.param({"name": "a\x00b", "type": "file", "digest": D1}, id="nul"),
        pytest.param({"name": "a" + chr(0xDCFF), "type": "file", "digest": D1}, id="surrogate"),
        pytest.param({"name": "a", "type": "file", "digest": None}, id="file-no-digest"),
        pytest.param({"name": "a", "type": "file", "digest": D1, "target": "x"}, id="file-target"),
        pytest.param({"name": "a", "type": "file", "digest": D1, "executable": 1}, id="exec-int"),
        pytest.param(
            {"name": "a", "type": "dir", "digest": D1, "executable": True}, id="dir-executable"
        ),
        pytest.param({"name": "a", "type": "symlink", "target": "x", "size": 1}, id="symlink-size"),
        pytest.param({"name": "a", "type": "symlink", "target": ""}, id="symlink-empty-target"),
        pytest.param(
            {"name": "a", "type": "symlink", "target": "x", "digest": D1}, id="symlink-digest"
        ),
        pytest.param(
            {"name": "a", "type": "symlink", "target": "x", "executable": True}, id="symlink-exec"
        ),
        pytest.param({"name": "a", "type": "file", "digest": D1, "size": True}, id="size-bool"),
        pytest.param({"name": "a", "type": "file", "digest": D1, "size": 1.0}, id="size-float"),
        pytest.param({"name": "a", "type": "file", "digest": D1, "size": -1}, id="size-negative"),
        pytest.param({"name": 1, "type": "file", "digest": D1}, id="name-not-str"),
        pytest.param({"name": "a", "type": "symlink", "target": "x\x00"}, id="symlink-nul"),
        pytest.param({"name": "a", "type": "fifo", "digest": D1}, id="bad-type"),
    ],
)
def test_entry_rejects(kwargs: dict[str, object]) -> None:
    full: dict[str, object] = {
        "digest": None,
        "size": 0,
        "executable": False,
        "target": None,
        **kwargs,
    }
    with pytest.raises(TreeError):
        TreeEntry(**full)  # type: ignore[arg-type]


# R6
def test_manifest_rejects_unsorted_and_duplicates() -> None:
    a = TreeEntry("a", "file", D1, 1, False, None)
    b = TreeEntry("b", "file", D1, 1, False, None)
    with pytest.raises(TreeError, match="sorted"):
        TreeManifest(entries=(b, a))
    with pytest.raises(TreeError, match="duplicate"):
        TreeManifest(entries=(a, a))
