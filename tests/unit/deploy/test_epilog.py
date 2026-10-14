"""The SLURM node epilog that removes scratch of SIGKILLed runners (P1-04 R4), run with bash."""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
EPILOG = REPO / "deploy" / "slurm" / "epilog.d" / "ebs-scratch-cleanup.sh"
JOB = "77"


def _marked(base: Path, build: str, action: str, job: str) -> Path:
    d = base / build / action
    (d / "work" / "sub").mkdir(parents=True)
    (d / "work" / "sub" / "f").write_text("x")
    (d / ".ebs-job").write_text(f"{job}\n")
    return d


def _run(
    base: Path, *, comment: str | None = "default", uid: int | None = None, job: str = JOB
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ["PATH"],
        "SLURM_JOB_ID": job,
        "SLURM_JOB_UID": str(os.getuid() if uid is None else uid),
        "SLURM_JOB_GID": str(os.getgid()),
        "EBS_SCRATCH_GLOBS": f"{base} {base.parent}/nomatch-*",
        "EBS_EPILOG_CONF": str(base.parent / "absent.conf"),
    }
    if comment == "default":
        env["SLURM_JOB_COMMENT"] = f"{uuid.uuid4()} sim[seed=1]"
    elif comment is not None:
        env["SLURM_JOB_COMMENT"] = comment
    return subprocess.run(
        ["bash", str(EPILOG)], env=env, capture_output=True, text=True, timeout=30
    )


@pytest.fixture
def base(tmp_path: Path) -> Path:
    b = tmp_path / "scratch"
    b.mkdir()
    return b


def test_script_is_executable_bash() -> None:
    assert os.access(EPILOG, os.X_OK)
    assert EPILOG.read_text().startswith("#!/bin/bash\n")
    subprocess.run(["bash", "-n", str(EPILOG)], check=True)


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_shellcheck_clean() -> None:
    subprocess.run(["shellcheck", "--shell=bash", str(EPILOG)], check=True)


# R4: removes exactly this job's scratch dirs (and build dirs left empty).
def test_removes_this_jobs_scratch(base: Path) -> None:
    mine = _marked(base, "1", "aaaa", JOB)
    (mine / "work" / "sub").chmod(0o500)  # tools leave read-only dirs behind
    other_job = _marked(base, "1", "bbbb", "78")
    prefix_job = _marked(base, "1", "eeee", JOB + "0")  # 770 is not 77
    alone = _marked(base, "2", "cccc", JOB)
    unmarked = base / "3" / "dddd"
    unmarked.mkdir(parents=True)

    result = _run(base)

    assert result.returncode == 0, result.stderr
    assert not mine.exists()
    assert not alone.exists()
    assert not (base / "2").exists()  # emptied build dir removed
    assert other_job.exists()  # another job's scratch on the same node
    assert prefix_job.exists()
    assert unmarked.exists()


# R4: a job whose comment is not an EBS build UUID is not an EBS job.
@pytest.mark.parametrize("comment", ["nightly regression", "1234"])
def test_non_ebs_job_untouched(base: Path, comment: str) -> None:
    mine = _marked(base, "1", "aaaa", JOB)
    result = _run(base, comment=comment)
    assert result.returncode == 0
    assert mine.exists()


# R4: if SLURM passes no (or an empty) comment, the markers alone decide.
@pytest.mark.parametrize("comment", [None, ""], ids=["unset", "empty"])
def test_without_comment(base: Path, comment: str | None) -> None:
    mine = _marked(base, "1", "aaaa", JOB)
    assert _run(base, comment=comment).returncode == 0
    assert not mine.exists()


# R4: a FIFO in place of the marker never hangs the epilog.
def test_fifo_marker(base: Path) -> None:
    d = base / "1" / "aaaa"
    d.mkdir(parents=True)
    os.mkfifo(d / ".ebs-job")
    assert _run(base).returncode == 0
    assert d.exists()


# R4: a marker owned by someone else is never followed.
def test_other_users_marker_ignored(base: Path) -> None:
    mine = _marked(base, "1", "aaaa", JOB)
    result = _run(base, uid=os.getuid() + 12345)
    assert result.returncode == 0
    assert mine.exists()


# R4: a symlinked build or action dir is never followed out of the scratch root.
def test_symlinks_not_followed(base: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    target = _marked(outside, "b", "aaaa", JOB)
    (base / "evil").symlink_to(outside / "b")
    (base / "1").mkdir()
    (base / "1" / "aaaa").symlink_to(target)
    result = _run(base)
    assert result.returncode == 0
    assert target.exists()
    assert (target / "work" / "sub" / "f").exists()


# R4: the epilog never fails (a failing epilog drains the node).
@pytest.mark.parametrize("job", ["", "abc", "1; rm -rf /"])
def test_bad_environment_exits_zero(base: Path, job: str) -> None:
    mine = _marked(base, "1", "aaaa", JOB)
    assert _run(base, job=job).returncode == 0
    assert mine.exists()


# R4: a symlinked scratch base is not followed either.
def test_symlinked_base_not_followed(tmp_path: Path) -> None:
    real = tmp_path / "real"
    mine = _marked(real, "1", "aaaa", JOB)
    link = tmp_path / "linkbase"
    link.symlink_to(real)
    assert _run(link).returncode == 0
    assert mine.exists()


def _unshare_root() -> bool:
    if shutil.which("unshare") is None:
        return False
    probe = subprocess.run(["unshare", "-r", "id", "-u"], capture_output=True, text=True)
    return probe.returncode == 0 and probe.stdout.strip() == "0"


def _stubs(tmp_path: Path, owner: str) -> tuple[Path, Path]:
    """PATH dir with `stat` claiming `owner` owns every marker and a logging `setpriv`."""
    bin_dir = tmp_path / "stubs"
    bin_dir.mkdir()
    log = tmp_path / "setpriv.log"
    (bin_dir / "stat").write_text(f"#!/bin/sh\necho {owner}\n")
    (bin_dir / "setpriv").write_text(
        f'#!/bin/bash\necho "$*" >> {log}\nwhile [[ $1 != -- ]]; do shift; done; shift; exec "$@"\n'
    )
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    return bin_dir, log


# R4: as root the epilog deletes only as the job's user (setpriv), and only its own markers.
@pytest.mark.skipif(not _unshare_root(), reason="needs unprivileged user namespaces (unshare -r)")
@pytest.mark.parametrize("owner", ["4242", "1"], ids=["job-user", "other-owner"])
def test_drops_privileges_as_root(base: Path, tmp_path: Path, owner: str) -> None:
    mine = _marked(base, "1", "aaaa", JOB)
    bin_dir, log = _stubs(tmp_path, owner)
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "SLURM_JOB_ID": JOB,
        "SLURM_JOB_UID": "4242",
        "SLURM_JOB_GID": "4343",
        "SLURM_JOB_COMMENT": f"{uuid.uuid4()} s",
        "EBS_SCRATCH_GLOBS": str(base),
        "EBS_EPILOG_CONF": str(tmp_path / "absent.conf"),
    }
    result = subprocess.run(
        ["unshare", "-r", "bash", str(EPILOG)], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines() if log.exists() else []
    if owner == "4242":
        assert not mine.exists()
        assert calls
        assert all(c.startswith("--reuid=4242 --regid=4343 --clear-groups -- ") for c in calls)
    else:
        assert mine.exists()
        assert calls == []
