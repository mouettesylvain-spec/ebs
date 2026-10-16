"""Accounts, QOS and node constraints for SLURM jobs (P1-03 R3, R4)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from ebs.cas.fs import FsCAS
from ebs.core.clock import FakeClock
from ebs.core.errors import ConfigError, ExecutorError, FlowError
from ebs.exec.slurm.accounts import (
    BuildKind,
    JobPolicy,
    QosSettings,
    job_constraint,
    resolve_account,
    select_qos,
)
from ebs.exec.slurm.executor import SlurmExecutor, SlurmSettings
from ebs.flow.model import Resources
from tests.helpers.executor import batch
from tests.helpers.slurm import ScriptedSlurm

ACCOUNTS = {"rv32x-cpu": "cpu-team", "soc-top": "soc-team"}
QOS = QosSettings(default="normal", ci="ci-high", release="release-top")


def _executor(
    tmp_path: Path,
    slurm: ScriptedSlurm,
    cas: FsCAS,
    *,
    project: str | None = "rv32x-cpu",
    build_kind: BuildKind = "dev",
    **settings: object,
) -> SlurmExecutor:
    return SlurmExecutor(
        cli=slurm.cli(),
        cas=cas,
        settings=SlurmSettings(debug_root=tmp_path / "debug", **settings),  # type: ignore[arg-type]
        runner_argv=("/opt/ebs/bin/ebs-runner",),
        env={"PATH": "/usr/bin:/bin"},
        clock=FakeClock(),
        project=project,
        build_kind=build_kind,
    )


def _opt(argv: list[str], name: str) -> str | None:
    prefix = f"--{name}="
    return next((a[len(prefix) :] for a in argv if a.startswith(prefix)), None)


# R3
@pytest.mark.parametrize(
    ("project", "kind", "account", "qos"),
    [
        ("rv32x-cpu", "dev", "cpu-team", "normal"),
        ("rv32x-cpu", "ci", "cpu-team", "ci-high"),
        ("soc-top", "release", "soc-team", "release-top"),
        ("soc-top", "dev", "soc-team", "normal"),
    ],
)
def test_mapping_and_qos(
    tmp_path: Path, cas: FsCAS, project: str, kind: BuildKind, account: str, qos: str
) -> None:
    assert resolve_account(ACCOUNTS, project) == account
    assert select_qos(QOS, kind) == qos
    policy = JobPolicy.resolve(
        account_map=ACCOUNTS, qos=QOS, os_constraint=None, project=project, build_kind=kind
    )
    assert (policy.account, policy.qos) == (account, qos)

    slurm = ScriptedSlurm()
    ex = _executor(
        tmp_path, slurm, cas, project=project, build_kind=kind, account_map=ACCOUNTS, qos=QOS
    )
    ex.submit(batch("t[0]", "t[1]"))
    ex.submit(batch("one"))
    for argv in slurm.calls:
        assert _opt(argv, "account") == account
        assert _opt(argv, "qos") == qos


# R3: an unmapped project fails before anything is submitted, naming the known projects.
def test_unknown_project(tmp_path: Path, cas: FsCAS) -> None:
    with pytest.raises(ConfigError) as info:
        resolve_account(ACCOUNTS, "gpu-x")
    text = str(info.value)
    assert "gpu-x" in text
    assert "rv32x-cpu, soc-top" in text
    assert "[slurm.account_map]" in text

    slurm = ScriptedSlurm()
    with pytest.raises(ConfigError, match="gpu-x"):
        _executor(tmp_path, slurm, cas, project="gpu-x", account_map=ACCOUNTS)
    with pytest.raises(ConfigError, match="project"):
        _executor(tmp_path, slurm, cas, project=None, account_map=ACCOUNTS)
    assert slurm.calls == []


# R3: without an account map or QOS config, SLURM's defaults apply (no option on the line).
def test_no_account_map_no_options(tmp_path: Path, cas: FsCAS) -> None:
    assert resolve_account({}, "rv32x-cpu") is None
    assert select_qos(QosSettings(), "ci") is None
    # An unset ci/release QOS falls back to the default one.
    assert select_qos(QosSettings(default="normal"), "release") == "normal"
    assert select_qos(QosSettings(default="normal"), "ci") == "normal"
    ci = ScriptedSlurm()
    _executor(tmp_path, ci, cas, build_kind="ci", qos=QosSettings(default="normal")).submit(
        batch("one")
    )
    assert _opt(ci.calls[0], "qos") == "normal"
    slurm = ScriptedSlurm()
    _executor(tmp_path, slurm, cas, project=None).submit(batch("one"))
    (argv,) = slurm.calls
    assert _opt(argv, "account") is None
    assert _opt(argv, "qos") is None
    assert _opt(argv, "constraint") is None


# R3
def test_settings_parse_accounts_and_qos() -> None:
    s = SlurmSettings.from_config(
        {
            "slurm": {
                "account_map": dict(ACCOUNTS),
                "qos": {"default": "normal", "ci": "ci-high", "release": "release-top"},
                "os_constraint": "rhel9",
                "license_server": "lmsrv",
                "license_fuzzy": True,
            }
        }
    )
    assert s.account_map == ACCOUNTS
    assert s.qos == QOS
    assert s.os_constraint == "rhel9"
    assert (s.license_server, s.license_fuzzy) == ("lmsrv", True)
    d = SlurmSettings.from_config({})
    assert d.account_map == {}
    assert d.qos == QosSettings()
    assert d.os_constraint is None
    assert (d.license_server, d.license_fuzzy) == ("flexlm", False)


# R3, R4
@pytest.mark.parametrize(
    ("section", "message"),
    [
        ({"account_map": ["x"]}, "account_map"),
        ({"account_map": {"rv32x-cpu": 3}}, "account_map"),
        ({"account_map": {"rv32x-cpu": "a b"}}, "account_map"),
        ({"qos": "normal"}, "qos"),
        ({"qos": {"nightly": "x"}}, "qos"),
        ({"qos": {"ci": "a;b"}}, "qos"),
        ({"os_constraint": "rhel 9"}, "os_constraint"),
        ({"os_constraint": "a|b"}, "os_constraint"),
        ({"license_fuzzy": "yes"}, "license_fuzzy"),
    ],
)
def test_settings_validation(section: dict[str, object], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        SlurmSettings.from_config({"slurm": section})


# R4
def test_constraints(tmp_path: Path, cas: FsCAS) -> None:
    assert job_constraint(None, []) is None
    assert job_constraint("rhel9", []) == "rhel9"
    assert job_constraint(None, ["avx512"]) == "avx512"
    # Step constraints are ANDed with the OS image, without repeating a feature.
    assert job_constraint("rhel9", ["avx512&bigmem", "avx512"]) == "rhel9&avx512&bigmem"
    assert job_constraint("rhel9", ["rhel9"]) == "rhel9"
    with pytest.raises(ExecutorError, match="a\\|b"):
        job_constraint("rhel9", ["a|b"])

    slurm = ScriptedSlurm()
    ex = _executor(tmp_path, slurm, cas, os_constraint="rhel9")
    b = batch("t[0]", "t[1]")
    b = dataclasses.replace(
        b,
        actions=tuple(
            dataclasses.replace(a, resources=Resources(constraint="avx512")) for a in b.actions
        ),
    )
    ex.submit(b)
    ex.submit(batch("plain"))
    step, plain = slurm.calls
    assert _opt(step, "constraint") == "rhel9&avx512"
    assert _opt(plain, "constraint") == "rhel9"


# R4: the flow accepts `resources.constraint` as features joined by `&`.
@pytest.mark.parametrize("good", ["avx512", "rhel9&avx512", "os_img.v2&gpu-a"])
def test_flow_constraint_accepted(good: str) -> None:
    assert Resources(constraint=good).constraint == good


@pytest.mark.parametrize("bad", ["", "a b", "a|b", "a&", "&a", "[a|b]", "${row.os}"])
def test_flow_constraint_rejected(bad: str) -> None:
    with pytest.raises((FlowError, ValueError)):
        Resources(constraint=bad)


# R3: an empty string in [slurm.qos] means unset; missing keys too.
def test_qos_empty_and_partial() -> None:
    s = SlurmSettings.from_config({"slurm": {"qos": {"default": "normal", "ci": ""}}})
    assert s.qos == QosSettings(default="normal")
    s = SlurmSettings.from_config({"slurm": {"qos": {"release": "rel"}}})
    assert s.qos == QosSettings(release="rel")


# R3
def test_unknown_build_kind() -> None:
    with pytest.raises(ConfigError, match="build kind"):
        JobPolicy.resolve(
            account_map={},
            qos=QOS,
            os_constraint=None,
            project=None,
            build_kind="nightly",  # type: ignore[arg-type]
        )
