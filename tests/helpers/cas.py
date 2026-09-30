"""Shared helpers for CAS tests: a sample directory tree and the I9 completeness check."""

from __future__ import annotations

import json
from pathlib import Path

from ebs.core.digest import Digest


def write(path: Path, data: bytes, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o755 if executable else 0o644)


def sample_tree(root: Path) -> Path:
    """A tree with nesting, a duplicate file, an executable, an empty dir and a symlink."""
    write(root / "a.txt", b"alpha")
    write(root / "run.sh", b"#!/bin/sh\necho hi\n", executable=True)
    write(root / "sub" / "b.txt", b"bravo!")
    write(root / "sub" / "copy_of_a.txt", b"alpha")
    write(root / "sub" / "deeper" / "c.bin", bytes(range(256)) * 8)
    write(root / "other" / "d.txt", b"delta")
    (root / "sub" / "empty").mkdir()
    (root / "link").symlink_to("sub/b.txt")
    return root


def visible_manifests(domain_root: Path) -> dict[Digest, Path]:
    return {
        Digest("sha256", p.name.removesuffix(".json")): p
        for p in (domain_root / "trees").rglob("*.json")
    }


def visible_blobs(domain_root: Path) -> set[Digest]:
    blobs = domain_root / "blobs"
    return {Digest(p.parts[-4], p.name) for p in blobs.rglob("*") if p.is_file()}  # type: ignore[arg-type]


def dangling_references(domain_root: Path) -> list[str]:
    """Every visible manifest must reference only visible blobs and manifests (I9)."""
    manifests = visible_manifests(domain_root)
    blobs = visible_blobs(domain_root)
    problems: list[str] = []
    for digest, path in manifests.items():
        for entry in json.loads(path.read_bytes())["entries"]:
            if entry["type"] == "file" and Digest.parse(entry["digest"]) not in blobs:
                problems.append(f"{digest} -> missing blob {entry['digest']}")
            if entry["type"] == "dir" and Digest.parse(entry["digest"]) not in manifests:
                problems.append(f"{digest} -> missing manifest {entry['digest']}")
    return problems
