"""Large inputs are linked read-only from the CAS, never copied; writable ones copied (R1)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from ebs.cas.api import DEFAULT_COPY_THRESHOLD
from ebs.cas.fs import FsCAS
from ebs.core.digest import Digest, hash_file
from ebs.plan.types import InputRef, SourceInput
from ebs.runner.main import EXIT_OK
from ebs.runner.stage import Scratch, stage_inputs, verify_inputs
from tests.helpers.runner import Harness, shell_spec

BIG = 300 << 20  # 300 MB, above the 256 MiB default threshold


def _sparse_blob(cas: FsCAS, tmp_path: Path, size: int = BIG) -> Digest:
    """A sparse blob of `size` zero bytes, placed in the CAS without writing its bytes."""
    src = tmp_path / "sparse.bin"
    with open(src, "wb") as f:
        f.truncate(size)
    digest = hash_file(src)[0]
    dest = cas.blob_path(digest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dest)
    dest.chmod(0o444)
    return digest


def _ref(path: str, digest: Digest, *, writable: bool = False) -> InputRef:
    return InputRef(path, "file", SourceInput("db", "**"), digest, writable=writable)


def _allocated(root: Path) -> int:
    """Bytes allocated on disk below `root` (symlinks not followed)."""
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            total += os.lstat(os.path.join(dirpath, name)).st_blocks * 512
    return total


# R1
def test_large_input_not_copied(cas: FsCAS, tmp_path: Path) -> None:
    digest = _sparse_blob(cas, tmp_path)
    spec = shell_spec("true", inputs=[_ref("db/big.bin", digest)])
    scratch = Scratch.create(tmp_path / "scratch", "1", spec.action_id)
    contents = {"db/big.bin": digest}

    stage_inputs(spec, cas, scratch.work, contents, copy_threshold=DEFAULT_COPY_THRESHOLD)

    staged = scratch.work / "db" / "big.bin"
    assert staged.is_symlink()
    assert staged.resolve() == cas.blob_path(digest).resolve()
    assert stat.S_IMODE(staged.stat().st_mode) & 0o222 == 0  # read-only through the link
    assert _allocated(scratch.root) < 1 << 20  # nothing was copied
    verify_inputs(spec, scratch.work, contents)  # the link still verifies (I10)


# R1: below the threshold inputs are still copied (a tool may not follow links into NFS).
def test_small_input_copied(cas: FsCAS, tmp_path: Path) -> None:
    digest = cas.put_bytes(b"small")
    spec = shell_spec("true", inputs=[_ref("small.txt", digest)])
    scratch = Scratch.create(tmp_path / "scratch", "1", spec.action_id)

    stage_inputs(spec, cas, scratch.work, {"small.txt": digest}, copy_threshold=1 << 20)

    staged = scratch.work / "small.txt"
    assert not staged.is_symlink()
    assert staged.read_bytes() == b"small"


# R1
@pytest.mark.parametrize(
    "size", [pytest.param(5, id="small"), pytest.param(BIG, id="large", marks=pytest.mark.slow)]
)
def test_writable_copy(cas: FsCAS, tmp_path: Path, size: int) -> None:
    digest = cas.put_bytes(b"12345") if size == 5 else _sparse_blob(cas, tmp_path, size)
    spec = shell_spec("true", inputs=[_ref("w.bin", digest, writable=True)])
    scratch = Scratch.create(tmp_path / "scratch", "1", spec.action_id)

    stage_inputs(spec, cas, scratch.work, {"w.bin": digest}, copy_threshold=DEFAULT_COPY_THRESHOLD)

    staged = scratch.work / "w.bin"
    assert not staged.is_symlink()
    assert staged.stat().st_size == size
    assert stat.S_IMODE(staged.stat().st_mode) & 0o200  # the tool may write it
    assert staged.stat().st_ino != cas.blob_path(digest).stat().st_ino  # a copy, not a link
    with open(staged, "r+b") as f:  # writing never reaches the shared blob
        f.write(b"X")
    assert cas.verify(digest)


# R1: `[cas].copy_threshold` reaches the runner's staging.
def test_runner_uses_configured_threshold(cas: FsCAS, tmp_path: Path) -> None:
    digest = cas.put_bytes(b"0123456789")
    script = "test -L a.bin && test ! -L w.bin\n"
    spec = shell_spec(script, inputs=[_ref("a.bin", digest), _ref("w.bin", digest, writable=True)])
    h = Harness(tmp_path, cas, settings_overrides={"copy_threshold": 4})
    code, build = h.run(spec)
    assert code == EXIT_OK, h.err.getvalue()
    manifest = h.result(build)
    assert manifest is not None
    assert manifest.status == "passed", h.log_text(manifest)
