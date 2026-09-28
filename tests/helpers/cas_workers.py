"""Process entry points for CAS concurrency tests (importable by spawned children)."""

from __future__ import annotations

from multiprocessing.queues import Queue
from multiprocessing.synchronize import Barrier
from pathlib import Path

from ebs.cas.fs import FsCAS


def put_same_blob(root: str, src: str, barrier: Barrier, results: Queue[str]) -> None:
    cas = FsCAS(Path(root), "test")
    barrier.wait()
    try:
        results.put(str(cas.put_file(Path(src))))
    except Exception as exc:  # reported to the parent instead of a silent exit code
        results.put(f"error: {exc!r}")
