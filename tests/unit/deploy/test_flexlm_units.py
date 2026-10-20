"""P3-08 R4: `ebs-flexlm-sync` runs every minute as an admin account, never inside a user build."""

from __future__ import annotations

import configparser
import io
import tomllib
from pathlib import Path

import pytest

from ebs.admin.flexlm_sync import CommandResult, FlexlmSyncConfig, main

ROOT = Path(__file__).resolve().parents[3]
FLEXLM = ROOT / "deploy" / "flexlm"


def _unit(name: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read_string((FLEXLM / name).read_text())
    return parser


# R4
def test_timer_fires_every_minute() -> None:
    timer = _unit("ebs-flexlm-sync.timer")
    assert timer["Timer"]["OnCalendar"] == "minutely"
    assert timer["Timer"]["Unit"] == "ebs-flexlm-sync.service"
    assert timer["Install"]["WantedBy"] == "timers.target"


# R4
def test_service_runs_as_the_admin_account() -> None:
    service = _unit("ebs-flexlm-sync.service")["Service"]
    assert service["Type"] == "oneshot"
    assert service["User"] == "ebs-flexlm"  # SLURM operator, not root and not a user
    assert service["ExecStart"] == "/opt/ebs/bin/ebs-flexlm-sync"
    assert service["Environment"] == "EBS_CONFIG=/etc/ebs/flexlm.toml"
    assert service["StateDirectory"] == "ebs-flexlm"  # the warn-once state (R3)
    assert int(service["TimeoutStartSec"].removesuffix("s")) < 60  # done before the next tick
    assert service["NoNewPrivileges"] == "yes"


# R4: the entry point is installed with the package
def test_entry_point_is_declared() -> None:
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]
    assert scripts["ebs-flexlm-sync"] == "ebs.admin.flexlm_sync:main"


def test_example_config_is_valid() -> None:
    config = tomllib.loads((FLEXLM / "flexlm.toml.example").read_text())
    sync = FlexlmSyncConfig.from_mapping(config["flexlm"])
    assert sync.state_file == Path("/var/lib/ebs-flexlm/state.json")
    assert [s.name for s in sync.servers] == ["flexlm"]


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "flexlm.toml"
    path.write_text(
        f'[flexlm]\nstate_file = "{tmp_path / "state.json"}"\n'
        '[flexlm.servers.flexlm]\nlicense = "27000@lic1.example"\nfeatures = ["simfeat"]\n'
    )
    return path


def _never(argv: object, *, timeout_s: float) -> CommandResult:
    raise AssertionError("must not run any command")


@pytest.fixture(autouse=True)
def _hermetic_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`main` also reads ./.ebs/config.toml: keep the checkout's out of the way."""
    monkeypatch.chdir(tmp_path)


# R4: the default per-call timeout keeps a run with one hung server inside the unit's limit
def test_default_timeouts_fit_the_unit() -> None:
    limit = int(_unit("ebs-flexlm-sync.service")["Service"]["TimeoutStartSec"])
    config = FlexlmSyncConfig.from_mapping(
        tomllib.loads((FLEXLM / "flexlm.toml.example").read_text())["flexlm"]
    )
    assert 2 * config.timeout_s * len(config.servers) < limit


# R4: inside a SLURM job (a user build) it refuses before touching anything
@pytest.mark.parametrize("var", ["SLURM_JOB_ID", "SLURM_JOBID"])
def test_refuses_to_run_inside_a_slurm_job(tmp_path: Path, var: str) -> None:
    environ = {"EBS_CONFIG": str(_config(tmp_path)), var: "42", "HOME": str(tmp_path)}
    err = io.StringIO()
    assert main([], environ=environ, runner=_never, out=io.StringIO(), err=err) == 2
    assert "SLURM job" in err.getvalue()


def test_config_without_flexlm_section_is_a_usage_error(tmp_path: Path) -> None:
    path = tmp_path / "other.toml"
    path.write_text('[slurm]\nlicense_server = "flexlm"\n')
    environ = {"EBS_CONFIG": str(path), "HOME": str(tmp_path)}
    err = io.StringIO()
    assert main([], environ=environ, runner=_never, out=io.StringIO(), err=err) == 2
    assert "[flexlm]" in err.getvalue()


def test_main_exit_codes(tmp_path: Path) -> None:
    environ = {"EBS_CONFIG": str(_config(tmp_path)), "HOME": str(tmp_path)}

    def lmstat_down(argv: object, *, timeout_s: float) -> CommandResult:
        return CommandResult(1, "", "Cannot connect to license server system")

    assert main([], environ=environ, runner=lmstat_down, out=io.StringIO()) == 1
    environ["EBS_CONFIG"] = str(tmp_path / "missing.toml")
    assert main([], environ=environ, runner=_never, out=io.StringIO()) == 2


@pytest.mark.parametrize("flag", ["--dry-run", "-n"])
def test_dry_run_flag(tmp_path: Path, flag: str) -> None:
    environ = {"EBS_CONFIG": str(_config(tmp_path)), "HOME": str(tmp_path)}
    up = (ROOT / "tests" / "fixtures" / "flexlm" / "lmstat_up.txt").read_text()
    calls: list[list[str]] = []

    def runner(argv: object, *, timeout_s: float) -> CommandResult:
        calls.append(list(argv))  # type: ignore[call-overload]
        if calls[-1][1] == "lmstat":
            return CommandResult(0, up, "")
        return CommandResult(0, "simfeat|flexlm|0\n", "")

    out = io.StringIO()
    assert main([flag], environ=environ, runner=runner, out=out) == 0
    expected = "sacctmgr -i update resource simfeat server=flexlm set LastConsumed=3\n"
    assert out.getvalue() == expected
    assert all("update" not in c for c in calls)
