"""deploy/admin/create_domain.sh (P2-01 R4), run with bash and a fake `chgrp`/`getent`: the test
runs unprivileged, so group changes are recorded instead of made."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "deploy" / "admin" / "create_domain.sh"


@pytest.fixture
def fake_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "getent").write_text('#!/bin/bash\n[ "$1" = group ] && [ "$2" = nda-test ]\n')
    (bin_dir / "chgrp").write_text(f'#!/bin/bash\necho "$*" >> {tmp_path / "chgrp.log"}\n')
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    return bin_dir


def _run(fake_bin: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {"PATH": f"{fake_bin}:{os.environ['PATH']}"}
    return subprocess.run(
        ["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=30
    )


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_script_is_executable_bash() -> None:
    assert os.access(SCRIPT, os.X_OK)
    assert SCRIPT.read_text().startswith("#!/bin/bash\n")
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


# R4: dirs with setgid modes and the domain's group; a second run repairs and changes nothing else.
def test_idempotent_modes_and_group(tmp_path: Path, fake_bin: Path) -> None:
    cas, debug = tmp_path / "cas", tmp_path / "debug"
    cas.mkdir()
    debug.mkdir()
    args = ["cpu-nda", "nda-test", "--cas-root", str(cas), "--debug-root", str(debug)]
    first = _run(fake_bin, *args)
    assert first.returncode == 0, first.stderr
    expected = {
        cas / "cpu-nda": 0o2750,
        cas / "cpu-nda" / "blobs": 0o2770,
        cas / "cpu-nda" / "trees": 0o2770,
        cas / "cpu-nda" / "tmp": 0o2770,
        debug / "cpu-nda": 0o2770,
    }
    assert {p: _mode(p) for p in expected} == expected
    log = (tmp_path / "chgrp.log").read_text().splitlines()
    assert sorted(log) == sorted(f"-- nda-test {p}" for p in expected)
    (cas / "cpu-nda" / "blobs" / "kept").write_text("object")
    (cas / "cpu-nda" / "tmp").chmod(0o700)  # drifted: repaired by the next run
    second = _run(fake_bin, *args)
    assert second.returncode == 0, second.stderr
    assert second.stdout == first.stdout
    assert {p: _mode(p) for p in expected} == expected
    assert (cas / "cpu-nda" / "blobs" / "kept").read_text() == "object"
    assert len((tmp_path / "chgrp.log").read_text().splitlines()) == 2 * len(expected)


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["cpu-nda", "no-such-group"], "does not exist"),
        (["../evil", "nda-test"], "invalid domain name"),
        (["cpu-nda"], "usage"),
        (["cpu-nda", "nda-test", "--cas-root"], "usage"),
    ],
)
def test_refusals(tmp_path: Path, fake_bin: Path, args: list[str], message: str) -> None:
    cas = tmp_path / "cas"
    cas.mkdir()
    full = [*args, "--cas-root", str(cas), "--debug-root", str(cas)] if len(args) == 2 else args
    result = _run(fake_bin, *full)
    assert result.returncode == 2
    assert message in result.stderr
    assert list(cas.iterdir()) == []


def test_missing_root(tmp_path: Path, fake_bin: Path) -> None:
    result = _run(fake_bin, "cpu-nda", "nda-test", "--cas-root", str(tmp_path / "nope"),
                  "--debug-root", str(tmp_path))  # fmt: skip
    assert result.returncode == 2
    assert "does not exist (mount or create it first)" in result.stderr
