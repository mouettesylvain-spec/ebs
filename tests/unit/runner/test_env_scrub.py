"""I11: only declared env + toolchain env reach the tool; HOME is an empty scratch dir."""

from __future__ import annotations

from pathlib import Path

from ebs.cas.fs import FsCAS
from ebs.core.digest import hash_bytes
from ebs.plan.types import PlanToolchain, ToolchainRef
from ebs.runner.env import DEFAULT_PATH
from ebs.runner.main import EXIT_OK
from ebs.runner.stage import action_dir_name
from tests.helpers.runner import Harness, shell_spec

CALLER_ENV = {
    "PATH": "/home/someone/bin:/usr/bin",
    "USER": "someone",
    "HOME": "/home/someone",
    "LD_PRELOAD": "/home/someone/evil.so",
    "PYTHONPATH": "/home/someone/py",
    "MAKEFLAGS": "-j99",
    "LM_LICENSE_FILE": "1717@license.example.invalid",
    "SNPSLMD_LICENSE_FILE": "27000@license.example.invalid",
    "SLURM_JOB_ID": "4242",
    "SLURM_CPUS_ON_NODE": "8",
    "TMPDIR": "/tmp/someone",
}
TC_ID = hash_bytes(b"toolchain")
TOOLCHAINS = {
    "questa": PlanToolchain(
        "questa/2025.2",
        TC_ID,
        {
            "PATH": "/opt/eda-tools/questa/bin:/usr/bin:/bin",
            "QUESTA_HOME": "/opt/eda-tools/questa",
            "MGC_HOME_CFG": "$HOME/.questa",
        },
    )
}


def _env_of(h: Harness, build: int, action_id: str = "s") -> dict[str, str]:
    manifest = h.result(build, action_id)  # type: ignore[arg-type]
    assert manifest is not None
    pairs = [p for p in h.log_text(manifest).split("\0") if p]
    return dict(p.split("=", 1) for p in pairs)


def _scratch(h: Harness, build: int, action_id: str = "s") -> Path:
    return h.tmp_path / "scratch" / str(build) / action_dir_name(action_id)


# R4 (I11)
def test_env_exact(cas: FsCAS, tmp_path: Path) -> None:
    h = Harness(tmp_path, cas, caller_env=dict(CALLER_ENV))
    spec = shell_spec(
        "",
        argv=["env", "-0"],
        env={"UVM_HOME": "uvm", "QUESTA_HOME": "/override"},  # declared env wins over toolchain
        runtime_env={"MAKEFLAGS": "-j$EBS_CPUS"},
        toolchain=ToolchainRef("questa", "questa/2025.2", TC_ID),
        cpus=4,
    )
    code, build = h.run(spec, toolchains=TOOLCHAINS)
    assert code == EXIT_OK
    scratch = _scratch(h, build)
    assert _env_of(h, build) == {
        "PATH": "/opt/eda-tools/questa/bin:/usr/bin:/bin",
        "QUESTA_HOME": "/override",
        "MGC_HOME_CFG": f"{scratch / 'home'}/.questa",  # captured $HOME -> this action's HOME
        "UVM_HOME": "uvm",
        "MAKEFLAGS": "-j4",
        "HOME": str(scratch / "home"),
        "TMPDIR": str(scratch / "tmp"),
        "EBS_CPUS": "4",
        "EBS_ACTION_ID": "s",
        "EBS_SCRATCH": str(scratch),
        "LM_LICENSE_FILE": "1717@license.example.invalid",
        "SNPSLMD_LICENSE_FILE": "27000@license.example.invalid",
        "SLURM_JOB_ID": "4242",
        "SLURM_CPUS_ON_NODE": "8",
    }


# R4 (I11): without a toolchain, PATH is a fixed system default, never the caller's.
def test_env_exact_without_toolchain(cas: FsCAS, tmp_path: Path) -> None:
    h = Harness(tmp_path, cas, caller_env={"PATH": "/home/someone/bin", "USER": "someone"})
    code, build = h.run(shell_spec("", argv=["env", "-0"]))
    assert code == EXIT_OK
    scratch = _scratch(h, build)
    assert _env_of(h, build) == {
        "PATH": DEFAULT_PATH,
        "HOME": str(scratch / "home"),
        "TMPDIR": str(scratch / "tmp"),
        "EBS_CPUS": "1",
        "EBS_ACTION_ID": "s",
        "EBS_SCRATCH": str(scratch),
    }


# R4 (I11): the runner's variables cannot be overridden by the flow.
def test_runner_vars_win(cas: FsCAS, tmp_path: Path) -> None:
    h = Harness(tmp_path, cas)
    spec = shell_spec("", argv=["env", "-0"], env={"HOME": "/home/someone", "EBS_CPUS": "64"})
    code, build = h.run(spec)
    assert code == EXIT_OK
    env = _env_of(h, build)
    assert env["HOME"] == str(_scratch(h, build) / "home")
    assert env["EBS_CPUS"] == "1"


# R4 (I11)
def test_home_empty(cas: FsCAS, tmp_path: Path) -> None:
    h = Harness(tmp_path, cas, caller_env={"HOME": str(Path.home())})
    script = 'echo "$HOME"; ls -A "$HOME" | wc -l; test -w "$HOME"; test -w "$TMPDIR"\n'
    code, build = h.run(shell_spec(script))
    assert code == EXIT_OK
    manifest = h.result(build)
    assert manifest is not None
    assert manifest.status == "passed"
    home, count = h.log_text(manifest).split()
    assert home == str(_scratch(h, build) / "home")
    assert count == "0"


# R4: passthrough variables come from `[runner].passthrough_env` and stay out of the key.
def test_passthrough(cas: FsCAS, tmp_path: Path) -> None:
    caller = {"SITE_TOKEN": "t", "LM_LICENSE_FILE": "1@x.invalid", "SLURM_JOB_ID": "1"}
    h = Harness(
        tmp_path, cas, caller_env=caller, settings_overrides={"passthrough_env": ("SITE_*",)}
    )
    spec = shell_spec("", argv=["env", "-0"])
    code, build = h.run(spec)
    assert code == EXIT_OK
    env = _env_of(h, build)
    assert env["SITE_TOKEN"] == "t"
    assert "LM_LICENSE_FILE" not in env  # the configured list replaces the default one
    assert "SLURM_JOB_ID" not in env
    manifest = h.result(build)
    assert manifest is not None
    assert manifest.action_key == spec.key  # same key whatever the caller passed through


# R4: a passthrough variable never overrides toolchain or declared env.
def test_passthrough_does_not_override_declared(cas: FsCAS, tmp_path: Path) -> None:
    h = Harness(tmp_path, cas, caller_env={"LM_LICENSE_FILE": "caller"})
    code, build = h.run(shell_spec("", argv=["env", "-0"], env={"LM_LICENSE_FILE": "declared"}))
    assert code == EXIT_OK
    assert _env_of(h, build)["LM_LICENSE_FILE"] == "declared"


def test_default_passthrough_patterns() -> None:
    from ebs.runner.config import DEFAULT_PASSTHROUGH_ENV
    from ebs.runner.env import passthrough

    env = passthrough(
        {"LM_LICENSE_FILE": "a", "X_LICENSE_FILE": "b", "SLURM_JOB_ID": "c", "LICENSE": "d",
         "MY_SLURM_X": "e", "slurm_job_id": "f"},
        DEFAULT_PASSTHROUGH_ENV,
    )  # fmt: skip
    assert env == {"LM_LICENSE_FILE": "a", "X_LICENSE_FILE": "b", "SLURM_JOB_ID": "c"}


# R4 (I11): the toolchain's captured value wins over the caller's too.
def test_passthrough_does_not_override_toolchain(cas: FsCAS, tmp_path: Path) -> None:
    h = Harness(tmp_path, cas, caller_env={"LM_LICENSE_FILE": "caller"})
    toolchains = {"t": PlanToolchain("t/1", TC_ID, {"LM_LICENSE_FILE": "toolchain"})}
    spec = shell_spec("", argv=["env", "-0"], toolchain=ToolchainRef("t", "t/1", TC_ID))
    code, build = h.run(spec, toolchains=toolchains)
    assert code == EXIT_OK
    assert _env_of(h, build)["LM_LICENSE_FILE"] == "toolchain"


# R4: the rule's runtime env overrides the declared env (documented layer order).
def test_runtime_env_over_declared(cas: FsCAS, tmp_path: Path) -> None:
    h = Harness(tmp_path, cas)
    spec = shell_spec(
        "",
        argv=["env", "-0"],
        env={"MAKEFLAGS": "-j1"},
        runtime_env={"MAKEFLAGS": "-j$EBS_CPUS"},
        cpus=4,
    )
    code, build = h.run(spec)
    assert code == EXIT_OK
    assert _env_of(h, build)["MAKEFLAGS"] == "-j4"


# R4: the toolchain env applied must belong to the toolchain id the action was keyed with.
def test_toolchain_id_mismatch_exit_64(cas: FsCAS, tmp_path: Path) -> None:
    from ebs.runner.main import EXIT_USAGE

    h = Harness(tmp_path, cas)
    other = {"questa": PlanToolchain("questa/2025.2", hash_bytes(b"other"), {})}
    spec = shell_spec("true", toolchain=ToolchainRef("questa", "questa/2025.2", TC_ID))
    code, _ = h.run(spec, toolchains=other)
    assert code == EXIT_USAGE
    assert "toolchain" in h.err.getvalue()
