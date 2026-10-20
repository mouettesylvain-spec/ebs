"""Tests for the bubblewrap and Apptainer argv builders (task P3-01 R2)."""

from __future__ import annotations

import pytest

from ebs.core.errors import SandboxError
from ebs.sandbox.api import Mount
from ebs.sandbox.apptainer import ApptainerSandbox
from ebs.sandbox.bwrap import BwrapSandbox
from ebs.sandbox.staging import StagingSandbox

MOUNTS = (
    Mount("/eda/questa-2025.2", "/eda/questa-2025.2"),
    Mount("/cas/cpu-nda", "/cas/cpu-nda"),
    Mount("/scratch/7/a1b2", "/scratch/7/a1b2", writable=True),
)
ENV = {"PATH": "/eda/questa-2025.2/bin:/usr/bin", "HOME": "/scratch/7/a1b2/home"}
WORK = "/scratch/7/a1b2/work"


# R2
def test_argv_golden() -> None:
    argv = BwrapSandbox("/usr/bin/bwrap").wrap(["vsim", "-c", "top"], MOUNTS, ENV, WORK)
    assert argv == [
        "/usr/bin/bwrap",
        "--unshare-all",
        "--share-net",
        "--die-with-parent",
        "--clearenv",
        "--ro-bind", "/usr", "/usr",
        "--ro-bind-try", "/lib", "/lib",
        "--ro-bind-try", "/lib32", "/lib32",
        "--ro-bind-try", "/lib64", "/lib64",
        "--ro-bind-try", "/libx32", "/libx32",
        "--ro-bind-try", "/bin", "/bin",
        "--ro-bind-try", "/sbin", "/sbin",
        "--ro-bind-try", "/etc/passwd", "/etc/passwd",
        "--ro-bind-try", "/etc/group", "/etc/group",
        "--ro-bind-try", "/etc/hosts", "/etc/hosts",
        "--ro-bind-try", "/etc/resolv.conf", "/etc/resolv.conf",
        "--ro-bind-try", "/etc/nsswitch.conf", "/etc/nsswitch.conf",
        "--ro-bind-try", "/etc/ld.so.cache", "/etc/ld.so.cache",
        "--ro-bind-try", "/etc/ld.so.conf", "/etc/ld.so.conf",
        "--ro-bind-try", "/etc/ld.so.conf.d", "/etc/ld.so.conf.d",
        "--ro-bind-try", "/etc/alternatives", "/etc/alternatives",
        "--ro-bind-try", "/etc/localtime", "/etc/localtime",
        "--ro-bind-try", "/var/lib/sss/pipes", "/var/lib/sss/pipes",
        "--ro-bind-try", "/run/nscd", "/run/nscd",
        "--tmpfs", "/tmp",
        "--proc", "/proc",
        "--dev", "/dev",
        "--ro-bind", "/eda/questa-2025.2", "/eda/questa-2025.2",
        "--ro-bind", "/cas/cpu-nda", "/cas/cpu-nda",
        "--bind", "/scratch/7/a1b2", "/scratch/7/a1b2",
        "--setenv", "HOME", "/scratch/7/a1b2/home",
        "--setenv", "PATH", "/eda/questa-2025.2/bin:/usr/bin",
        "--chdir", WORK,
        "--",
        "vsim", "-c", "top",
    ]  # fmt: skip


# R2: nothing outside the profile is visible: no bind of `/`, `/home` or a project path.
def test_profile_hides_the_host() -> None:
    argv = BwrapSandbox("/usr/bin/bwrap").wrap(["true"], MOUNTS, {}, WORK)
    sources = {argv[i + 1] for i, a in enumerate(argv) if a in ("--bind", "--ro-bind")}
    assert "/" not in sources
    assert not any(s.startswith(("/home", "/proj", "/nfs")) for s in sources)
    assert "--share-net" in argv  # FlexLM needs the network


# R2
@pytest.mark.parametrize(
    "mount",
    [Mount("relative/dir", "/x"), Mount("/x", "relative"), Mount("/a/../b", "/a/../b")],
)
def test_mount_paths_must_be_absolute_and_normal(mount: Mount) -> None:
    with pytest.raises(SandboxError, match="absolute"):
        BwrapSandbox("/usr/bin/bwrap").wrap(["true"], [mount], {}, WORK)


# R2: bwrap's own setup errors are told apart from the tool's output.
def test_setup_error() -> None:
    bwrap = BwrapSandbox("/usr/bin/bwrap")
    log = "bwrap: Can't find source path /eda/questa-2025.2: No such file or directory\n"
    assert bwrap.setup_error(1, log) == log.strip()
    assert bwrap.setup_error(1, "Error: vsim failed\n") is None
    assert bwrap.setup_error(2, log) is None  # bwrap reports setup failures with exit code 1
    assert bwrap.setup_error(1, "Error: x\nbwrap: y\n") is None  # only bwrap's first line counts


# R2 (Apptainer fallback, D6)
def test_apptainer_argv_golden() -> None:
    argv = ApptainerSandbox("/usr/bin/apptainer", "/images/os.sif").wrap(
        ["vsim", "-c"], MOUNTS, ENV, WORK
    )
    assert argv == [
        "/usr/bin/apptainer",
        "exec",
        "--containall",
        "--cleanenv",
        "--no-home",
        "--no-mount", "bind-paths",
        "--bind", "/eda/questa-2025.2:/eda/questa-2025.2:ro",
        "--bind", "/cas/cpu-nda:/cas/cpu-nda:ro",
        "--bind", "/scratch/7/a1b2:/scratch/7/a1b2:rw",
        "--pwd", WORK,
        "/images/os.sif",
        "/usr/bin/env", "-i",
        "HOME=/scratch/7/a1b2/home",
        "PATH=/eda/questa-2025.2/bin:/usr/bin",
        "vsim", "-c",
    ]  # fmt: skip


# R2: Apptainer bind specs are `src:dst:opts`, so a ':' or ',' in a path cannot be expressed.
@pytest.mark.parametrize("path", ["/a:b", "/a,b"])
def test_apptainer_rejects_separator_in_path(path: str) -> None:
    with pytest.raises(SandboxError, match="':' and ','"):
        ApptainerSandbox("/usr/bin/apptainer", "/i.sif").wrap(
            ["true"], [Mount(path, path)], {}, WORK
        )


# R2: `env -i` would take a program named `A=B` for an assignment and run the next argument.
def test_apptainer_rejects_assignment_as_program() -> None:
    with pytest.raises(SandboxError, match="'='"):
        ApptainerSandbox("/usr/bin/apptainer", "/i.sif").wrap(["A=B", "x"], [], {}, WORK)


# R2: Apptainer's own failure (exit 255, `FATAL:` first) is told apart from the tool's.
def test_apptainer_setup_error() -> None:
    app = ApptainerSandbox("/usr/bin/apptainer", "/i.sif")
    fatal = "FATAL:   could not open image /i.sif: no such file\n"
    assert app.setup_error(255, fatal) == fatal.strip()
    assert app.setup_error(255, "Error: x\n") is None
    assert app.setup_error(1, fatal) is None


# R1: the v1 staging sandbox runs the command as is.
def test_staging_is_a_no_op() -> None:
    assert StagingSandbox().wrap(["make", "lint"], MOUNTS, ENV, WORK) == ["make", "lint"]
    assert StagingSandbox().setup_error(1, "bwrap: x") is None
