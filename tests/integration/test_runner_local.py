"""The runner as a process (`ebs-runner`): scratch cleanup on every exit path (P0-13 R8)."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from ebs.cas.fs import FsCAS
from ebs.plan.types import OutputSpec
from ebs.runner.main import EXIT_INFRA, EXIT_OK
from tests.helpers.runner import shell_spec, source_file, store_plan
from tests.helpers.wait import wait_until


def _setup(tmp_path: Path) -> tuple[FsCAS, dict[str, str]]:
    cas = FsCAS(tmp_path / "cas", "test")
    config = tmp_path / "config.toml"
    config.write_text(
        f'[cas]\nroot = "{tmp_path / "cas"}"\n'
        f'[scratch]\ndir = "{tmp_path / "scratch"}"\n'
        "[runner]\nkill_grace_s = 2\n"
    )
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path / "home"),
        "EBS_CONFIG": str(config),
    }
    return cas, env


def _runner(digest: object, env: dict[str, str], *extra: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "ebs.runner.main", "--plan", str(digest), "--action", "s",
         "--domain", "test", *extra],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )  # fmt: skip


def _scratch_left(tmp_path: Path) -> list[Path]:
    root = tmp_path / "scratch"
    return list(root.rglob("*")) if root.exists() else []


def _dead(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] == "Z"
    except FileNotFoundError:
        return True


LICENSE_FAIL = "echo 'license checkout failed'; exit 1\n"


# R8
@pytest.mark.parametrize(
    ("script", "time", "expected"),
    [
        pytest.param("echo ok > out.txt\n", None, EXIT_OK, id="pass"),
        pytest.param("exit 4\n", None, EXIT_OK, id="fail"),
        pytest.param(LICENSE_FAIL, None, EXIT_INFRA, id="infra"),
        pytest.param("sleep 30\n", 1, EXIT_INFRA, id="timeout"),
    ],
)
def test_cleanup_all_paths(tmp_path: Path, script: str, time: int | None, expected: int) -> None:
    cas, env = _setup(tmp_path)
    src = source_file(cas, "in.txt", b"input")
    spec = shell_spec(
        script, inputs=[src], time=time, outputs=[OutputSpec("o", "out.txt", "file", optional=True)]
    )
    proc = _runner(store_plan(cas, spec), env)
    _out, err = proc.communicate(timeout=60)
    assert proc.returncode == expected, err
    assert _scratch_left(tmp_path) == []


# R8: an unexpected error after staging (here: the CAS refuses the output upload).
def test_cleanup_on_exception(tmp_path: Path) -> None:
    cas, env = _setup(tmp_path)
    digest = store_plan(
        cas, shell_spec("echo x > out.txt\n", outputs=[OutputSpec("o", "out.txt", "file")])
    )
    # Replace the CAS staging dir with a regular file: uploads then fail for every user. (A
    # read-only dir is not enough: CI runs as root, which ignores permission bits.)
    staging = tmp_path / "cas" / "test" / "tmp"
    shutil.rmtree(staging)
    staging.write_text("not a directory")
    proc = _runner(digest, env)
    _out, err = proc.communicate(timeout=60)
    assert proc.returncode == EXIT_INFRA, err  # CAS trouble is temporary: the driver retries
    assert "out.txt" in err or "CAS" in err
    assert _scratch_left(tmp_path) == []


# R8: SIGTERM (SLURM cancel/preempt) or SIGINT kills the tool's group first, then cleans up.
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"])
def test_cleanup_on_signal(tmp_path: Path, signum: int) -> None:
    cas, env = _setup(tmp_path)
    pids = tmp_path / "pids"
    script = f"sleep 60 & echo $! > {pids}; echo $$ >> {pids}; wait\n"
    proc = _runner(store_plan(cas, shell_spec(script)), env)
    wait_until(
        lambda: pids.exists() and len(pids.read_text().split()) == 2,
        timeout=30,
        what="the tool and its child to start",
    )
    proc.send_signal(signum)
    _out, err = proc.communicate(timeout=30)
    assert proc.returncode == EXIT_INFRA, err
    assert all(_dead(int(p)) for p in pids.read_text().split())
    assert _scratch_left(tmp_path) == []


# R8: a second SIGTERM (SLURM repeats it) must not interrupt the cleanup the first one started.
def test_cleanup_on_repeated_sigterm(tmp_path: Path) -> None:
    cas, env = _setup(tmp_path)
    ready = tmp_path / "ready"
    # Many files make the scratch removal long enough for the repeats to land during it.
    script = f"mkdir many; for i in $(seq 1 3000); do : > many/$i; done; touch {ready}; sleep 60\n"
    proc = _runner(store_plan(cas, shell_spec(script)), env)
    wait_until(ready.exists, timeout=30, what="the tool to fill its scratch")
    for _ in range(5):
        proc.send_signal(signal.SIGTERM)
    _out, err = proc.communicate(timeout=30)
    assert proc.returncode == EXIT_INFRA, err
    assert _scratch_left(tmp_path) == []


# R8
def test_keep_scratch(tmp_path: Path) -> None:
    cas, env = _setup(tmp_path)
    proc = _runner(store_plan(cas, shell_spec("echo kept > note.txt\n")), env, "--keep-scratch")
    _out, err = proc.communicate(timeout=60)
    assert proc.returncode == EXIT_OK, err
    kept = [line.split(" at ", 1)[1] for line in err.splitlines() if "scratch kept at" in line]
    assert len(kept) == 1
    assert (Path(kept[0]) / "work" / "note.txt").read_text() == "kept\n"
    assert (Path(kept[0]) / "logs" / "tool.log").exists()
