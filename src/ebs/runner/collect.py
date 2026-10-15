"""Hash and upload outputs and the tool log to the CAS."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from ebs.cas.api import CAS
from ebs.core.digest import Digest
from ebs.meta.api import BlobRef, OutputResult
from ebs.plan.types import ActionSpec, OutputSpec
from ebs.runner.result import output_id

__all__ = ["Collected", "collect_outputs", "log_tail", "output_paths", "stored_objects"]

LOG_TAIL_BYTES: Final = 64 << 10


@dataclass(frozen=True, slots=True)
class Collected:
    outputs: dict[str, OutputResult]
    missing: tuple[str, ...]  # required outputs absent (or of the wrong type), by name


def output_paths(spec: ActionSpec, work: Path) -> dict[str, Path]:
    """Outputs that exist with their declared type: name -> path in the work dir."""
    return {out.name: work / out.path for out in spec.outputs if _present(out, work / out.path)}


def _present(out: OutputSpec, path: Path) -> bool:
    if path.is_symlink():
        return False
    return path.is_file() if out.type == "file" else path.is_dir()


def collect_outputs(spec: ActionSpec, work: Path, cas: CAS, key: Digest) -> Collected:
    """Upload every present output; report required ones that are missing."""
    present = output_paths(spec, work)
    results: dict[str, OutputResult] = {}
    kind: Literal["file", "tree"]
    for out in spec.outputs:
        path = present.get(out.name)
        if path is None:
            continue
        if out.type == "file":
            content = cas.put_file(path)
            size = os.stat(path).st_size
            kind = "file"
        else:
            content = cas.put_tree(path)  # blobs first, manifests last (I9)
            size = sum(e.size for e in cas.get_tree(content).entries)
            kind = "tree"
        results[out.name] = OutputResult(
            digest=content, id=output_id(out, content, key), type=kind, size=size
        )
    missing = tuple(o.name for o in spec.outputs if not o.optional and o.name not in present)
    return Collected(results, missing)


def log_tail(path: Path, limit: int = LOG_TAIL_BYTES) -> str:
    """The last `limit` bytes of the log, decoded leniently, for `RulePlugin.classify`."""
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.fstat(f.fileno()).st_size - limit))
            return f.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def stored_objects(
    cas: CAS, outputs: Mapping[str, OutputResult], log: Digest | None, log_path: Path
) -> list[BlobRef]:
    """Every CAS object the runner stored for this action: output files, output trees with all
    their manifests and blobs, and the log (GC registration, P1-09 R1). A tree's size is the
    total of the file bytes below it, like `OutputResult.size`."""
    found: dict[Digest, BlobRef] = {}

    def tree(digest: Digest, size: int) -> None:
        found[digest] = BlobRef(digest=digest, size=size, kind="tree")
        for entry in cas.get_tree(digest).entries:
            if entry.digest is None or entry.digest in found:
                continue
            if entry.type == "dir":
                tree(entry.digest, entry.size)
            else:
                found[entry.digest] = BlobRef(digest=entry.digest, size=entry.size, kind="file")

    for out in outputs.values():
        if out.type == "tree":
            tree(out.digest, out.size)
        else:
            found[out.digest] = BlobRef(digest=out.digest, size=out.size, kind="file")
    if log is not None:
        found[log] = BlobRef(digest=log, size=os.stat(log_path).st_size, kind="file")
    return [found[d] for d in sorted(found, key=str)]
