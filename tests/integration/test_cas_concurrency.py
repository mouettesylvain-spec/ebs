"""I8 / R3: concurrent writers of the same blob, in separate processes."""

from __future__ import annotations

import multiprocessing as mp
import os
from pathlib import Path

from ebs.cas.fs import FsCAS
from ebs.core.digest import hash_file
from tests.helpers.cas_workers import put_same_blob

WRITERS = 8
SIZE = 50 << 20


# R3 (I8)
def test_same_blob_parallel(tmp_path: Path) -> None:
    src = tmp_path / "blob.bin"
    with open(src, "wb") as f:
        for _ in range(SIZE >> 20):
            f.write(os.urandom(1 << 20))
    expected, _ = hash_file(src)
    root = tmp_path / "cas"

    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(WRITERS)
    results = ctx.Queue()
    procs = [
        ctx.Process(target=put_same_blob, args=(str(root), str(src), barrier, results))
        for _ in range(WRITERS)
    ]
    for p in procs:
        p.start()
    outcomes = [results.get(timeout=120) for _ in procs]
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0

    assert outcomes == [str(expected)] * WRITERS
    cas = FsCAS(root, "test")
    assert [d for d, _ in cas.iter_digests()] == [expected]
    assert hash_file(cas.blob_path(expected))[0] == expected
    assert cas.verify(expected)
    assert os.listdir(root / "test" / "tmp") == []
