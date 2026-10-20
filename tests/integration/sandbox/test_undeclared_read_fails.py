"""Under bubblewrap an undeclared read fails with ENOENT (task P3-01 R3)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from ebs.cas.fs import FsCAS
from ebs.flow.model import SandboxMode
from ebs.plan.keys import with_key
from ebs.plan.types import ActionSpec
from ebs.runner.main import EXIT_INFRA, EXIT_OK
from tests.helpers.runner import Harness, shell_spec, source_file

pytestmark = pytest.mark.usefixtures("bwrap_available")


def _spec(script: str, mode: SandboxMode, **kw: object) -> ActionSpec:
    return with_key(replace(shell_spec(script, **kw), sandbox=mode))  # type: ignore[arg-type]


def _project_file(tmp_path: Path) -> Path:
    """A file the step never declared, like a path on project NFS or in the user's home."""
    path = tmp_path / "proj" / "defs.v"
    path.parent.mkdir()
    path.write_text("`define X 1\n")
    return path


# R3: the same read succeeds with staging only and fails inside bwrap.
@pytest.mark.parametrize(("mode", "status"), [("staging", "passed"), ("bwrap", "failed")])
def test_undeclared_read(cas: FsCAS, tmp_path: Path, mode: SandboxMode, status: str) -> None:
    undeclared = _project_file(tmp_path)
    h = Harness(tmp_path, cas)
    code, build = h.run(_spec(f"cat {undeclared}", mode))
    assert code == EXIT_OK
    manifest = h.result(build)
    assert manifest is not None
    assert manifest.status == status
    assert manifest.runner.sandbox == mode
    if mode == "bwrap":
        assert "No such file or directory" in h.log_text(manifest)


# R3: a dotfile in the user's home (synthetic here) is not there either; HOME is the empty
# scratch home.
def test_home_dotfile_hidden(cas: FsCAS, tmp_path: Path) -> None:
    dotfile = tmp_path / "users" / "alice" / ".synopsys_dc.setup"
    dotfile.parent.mkdir(parents=True)
    dotfile.write_text("set search_path .\n")
    h = Harness(tmp_path, cas, caller_env={"HOME": str(dotfile.parent)})
    script = f'test ! -e {dotfile} && test ! -e {dotfile.parent} && test -z "$(ls -A "$HOME")"'
    code, build = h.run(_spec(script, "bwrap"))
    assert code == EXIT_OK
    manifest = h.result(build)
    assert manifest is not None
    assert manifest.status == "passed", h.log_text(manifest)


# R2: declared inputs, small (copied) and large (linked read-only from the CAS), are readable;
# scratch is writable; the CAS is not.
def test_declared_inputs_readable(cas: FsCAS, tmp_path: Path) -> None:
    small = source_file(cas, "small.v", b"small\n")
    large = source_file(cas, "large.v", b"L" * 4096)
    h = Harness(tmp_path, cas, settings_overrides={"copy_threshold": 1024})
    script = (
        "set -e; cat small.v >/dev/null; cat large.v >/dev/null; test -L large.v;"
        ' echo ok > out.txt; ! touch "$(dirname "$(readlink large.v)")/x"'
    )
    code, build = h.run(_spec(script, "bwrap", inputs=[small, large]))
    assert code == EXIT_OK
    manifest = h.result(build)
    assert manifest is not None
    assert manifest.status == "passed", h.log_text(manifest)


# R1/R2: bwrap's own failure (here a site bind that does not exist on the node) is an infra
# failure, retried elsewhere, never a cached FAILED result; this pins bwrap's real message.
def test_setup_failure_is_infra(cas: FsCAS, tmp_path: Path) -> None:
    missing = str(tmp_path / "no-such-tool-tree")
    h = Harness(tmp_path, cas, settings_overrides={"sandbox_ro_binds": (missing,)})
    code, build = h.run(_spec("exit 0", "bwrap"))
    assert code == EXIT_INFRA
    assert h.result(build) is None
    assert "bwrap sandbox failed: bwrap: " in h.err.getvalue()
    assert missing in h.err.getvalue()
    assert h.scratch_entries() == []
