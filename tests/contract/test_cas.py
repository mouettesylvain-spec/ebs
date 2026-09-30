"""CAS contract suite: backend-agnostic behaviour every `CAS` implementation must show.

Parametrized over backends; P3-07 adds the S3 backend to `BACKENDS`.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from ebs.cas.api import CAS
from ebs.cas.fs import FsCAS
from ebs.core.digest import hash_bytes, hash_file
from ebs.core.errors import CasError
from ebs.core.tree import build_tree
from tests.helpers.cas import sample_tree

Factory = Callable[[Path, str], CAS]
BACKENDS: dict[str, Factory] = {
    "fs": lambda tmp, domain: FsCAS(tmp / "cas", domain),
}


@pytest.fixture(params=sorted(BACKENDS))
def make(request: pytest.FixtureRequest, tmp_path: Path) -> Callable[[str], CAS]:
    factory = BACKENDS[request.param]
    return lambda domain: factory(tmp_path, domain)


def test_domain_attribute(make: Callable[[str], CAS]) -> None:
    assert make("alpha").domain == "alpha"


def test_put_has_open(make: Callable[[str], CAS]) -> None:
    cas = make("test")
    d = cas.put_bytes(b"blob")
    assert d == hash_bytes(b"blob")
    assert cas.has(d)
    with cas.open(d) as f:
        assert f.read() == b"blob"
    assert not cas.has(hash_bytes(b"other"))


def test_idempotent_put(make: Callable[[str], CAS]) -> None:
    cas = make("test")
    assert cas.put_bytes(b"x") == cas.put_bytes(b"x")
    assert [d for d, _ in cas.iter_digests()] == [hash_bytes(b"x")]


# R1 / I13-style scoping at the storage level
def test_domains_do_not_share_objects(make: Callable[[str], CAS]) -> None:
    a, b = make("a"), make("b")
    d = a.put_bytes(b"nda")
    assert not b.has(d)
    with pytest.raises(CasError):
        b.open(d)


# R5
def test_put_file_expected(make: Callable[[str], CAS], tmp_path: Path) -> None:
    cas = make("test")
    src = tmp_path / "f"
    src.write_bytes(b"bytes")
    with pytest.raises(CasError):
        cas.put_file(src, expected=hash_bytes(b"not these"))
    assert list(cas.iter_digests()) == []
    assert cas.put_file(src, expected=hash_bytes(b"bytes")) == hash_bytes(b"bytes")


# R4 / R6
def test_tree_roundtrip(make: Callable[[str], CAS], tmp_path: Path) -> None:
    cas = make("test")
    src = sample_tree(tmp_path / "src")
    d = cas.put_tree(src)
    expected, manifests = build_tree(src, hash_file)
    assert d == expected
    assert cas.has(d)
    assert cas.verify(d)
    assert cas.get_tree(d) == manifests[d]
    for manifest in manifests.values():
        for entry in manifest.entries:
            if entry.digest is not None:
                assert cas.has(entry.digest)


# R6
def test_verify(make: Callable[[str], CAS]) -> None:
    cas = make("test")
    d = cas.put_bytes(b"ok")
    assert cas.verify(d)
    assert not cas.verify(hash_bytes(b"absent"))


# R7
@pytest.mark.parametrize("writable", [False, True])
def test_materialize(make: Callable[[str], CAS], tmp_path: Path, writable: bool) -> None:
    cas = make("test")
    d = cas.put_tree(sample_tree(tmp_path / "src"))
    dest = tmp_path / "out"
    cas.materialize(d, "tree", dest, writable=writable)
    assert (dest / "sub" / "deeper" / "c.bin").read_bytes() == bytes(range(256)) * 8
    assert os.stat(dest / "run.sh").st_mode & stat.S_IXUSR
    assert bool(os.stat(dest / "a.txt").st_mode & stat.S_IWUSR) == writable
    f = tmp_path / "single"
    cas.materialize(cas.put_bytes(b"one"), "file", f)
    assert f.read_bytes() == b"one"


def test_delete(make: Callable[[str], CAS]) -> None:
    cas = make("test")
    d = cas.put_bytes(b"gone")
    cas.delete(d)
    assert not cas.has(d)
    cas.delete(d)
