"""I9 / R4: put_tree writes blobs, then child manifests bottom-up, then the root manifest."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ebs.cas.fs import FsCAS
from ebs.core.digest import Digest
from ebs.core.errors import CasError
from tests.helpers.cas import dangling_references, sample_tree, write


def _count_renames(src: Path, root: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    renamed: list[str] = []
    real_rename = os.rename

    def spy(a: Any, b: Any) -> None:
        renamed.append(os.fspath(b))
        real_rename(a, b)

    monkeypatch.setattr(os, "rename", spy)
    FsCAS(root, "test").put_tree(src)
    monkeypatch.setattr(os, "rename", real_rename)
    return renamed


def _fail_after(n: int, monkeypatch: pytest.MonkeyPatch) -> None:
    real_rename = os.rename
    count = {"n": 0}

    def flaky(a: Any, b: Any) -> None:
        if count["n"] >= n:
            raise OSError(28, "No space left on device")
        count["n"] += 1
        real_rename(a, b)

    monkeypatch.setattr(os, "rename", flaky)


# R4
def test_write_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = sample_tree(tmp_path / "src")
    order = _count_renames(src, tmp_path / "cas", monkeypatch)
    cas = FsCAS(tmp_path / "cas", "test")
    position = {Path(p): i for i, p in enumerate(order)}
    manifests = [Path(p) for p in order if p.endswith(".json")]
    blobs = [Path(p) for p in order if not p.endswith(".json")]
    assert blobs
    assert manifests
    assert max(position[b] for b in blobs) < min(position[m] for m in manifests)
    for m in manifests:
        for entry in json.loads(m.read_bytes())["entries"]:
            if entry["type"] == "dir":
                child = cas.tree_path(Digest.parse(entry["digest"]))
                assert position[child] < position[m]
    # The root manifest is the very last write.
    assert Path(order[-1]) == cas.tree_path(cas.put_tree(src))


# R4 (I9): exception after N writes never leaves a manifest referencing a missing object.
@pytest.mark.parametrize("fail_after", range(11))
def test_fault_injection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_after: int) -> None:
    src = sample_tree(tmp_path / "src")
    total = len(_count_renames(src, tmp_path / "probe", monkeypatch))
    assert total == 10  # 5 distinct blobs + 5 manifests (root, sub, sub/deeper, sub/empty, other)
    cas = FsCAS(tmp_path / "cas", "test")
    _fail_after(fail_after, monkeypatch)
    if fail_after < total:
        with pytest.raises(CasError, match="No space left"):
            cas.put_tree(src)
    else:
        cas.put_tree(src)
    monkeypatch.undo()
    base = tmp_path / "cas" / "test"
    assert dangling_references(base) == []
    assert os.listdir(base / "tmp") == []
    # Retrying after the fault completes the tree.
    root = cas.put_tree(src)
    assert dangling_references(base) == []
    assert cas.get_tree(root)


@st.composite
def _tree_specs(draw: st.DrawFn) -> dict[str, bytes]:
    names = st.sampled_from(["a", "b", "c", "d"])
    paths = st.lists(names, min_size=1, max_size=4).map("/".join)
    return draw(st.dictionaries(paths, st.binary(max_size=8), min_size=1, max_size=8))


# R4 (I9), randomized shapes and fault points.
@settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(spec=_tree_specs(), fail_after=st.integers(min_value=0, max_value=20))
def test_fault_injection_random_trees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spec: dict[str, bytes], fail_after: int
) -> None:
    with tempfile.TemporaryDirectory(dir=tmp_path) as work:
        src = Path(work) / "src"
        src.mkdir()
        for rel, data in spec.items():
            target = src / rel
            # A path can't be both a file and a dir: skip entries that collide with earlier ones.
            if any(p.is_file() for p in target.parents if p != src) or target.is_dir():
                continue
            write(target, data)
        cas = FsCAS(Path(work) / "cas", "test")
        with monkeypatch.context() as m:
            _fail_after(fail_after, m)
            with contextlib.suppress(CasError):
                cas.put_tree(src)
        base = Path(work) / "cas" / "test"
        assert dangling_references(base) == []
        assert os.listdir(base / "tmp") == []


# R4: blobs follow the CAS algo, manifests stay sha256 and are filed under that digest.
def test_put_tree_non_default_algo(tmp_path: Path) -> None:
    pytest.importorskip("blake3", reason="missing dependency: optional blake3 extra")
    cas = FsCAS(tmp_path / "cas", "test", algo="blake3")
    root = cas.put_tree(sample_tree(tmp_path / "src"))
    assert root.algo == "sha256"
    assert dangling_references(tmp_path / "cas" / "test") == []
    manifest = cas.get_tree(root)
    files = [e.digest for e in manifest.entries if e.type == "file"]
    assert files
    assert all(d is not None and d.algo == "blake3" for d in files)
    assert cas.verify(root)
