"""License requests: merge, validation, rendering, error surfacing (P1-03 R1, R2)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from ebs.cas.fs import FsCAS
from ebs.core.clock import FakeClock
from ebs.core.errors import ConfigError, ExecutorError
from ebs.exec.slurm.executor import SlurmExecutor, SlurmSettings
from ebs.exec.slurm.licenses import (
    batch_licenses,
    license_error,
    license_request,
    merge_licenses,
    render_licenses,
)
from tests.helpers.executor import batch, spec
from tests.helpers.slurm import ScriptedSlurm


def _executor(
    tmp_path: Path, slurm: ScriptedSlurm, cas: FsCAS, **settings: object
) -> SlurmExecutor:
    return SlurmExecutor(
        cli=slurm.cli(),
        cas=cas,
        settings=SlurmSettings(debug_root=tmp_path / "debug", **settings),  # type: ignore[arg-type]
        runner_argv=("/opt/ebs/bin/ebs-runner",),
        env={"PATH": "/usr/bin:/bin"},
        clock=FakeClock(),
    )


def _licenses_opt(argv: list[str]) -> str | None:
    return next((a.split("=", 1)[1] for a in argv if a.startswith("--licenses=")), None)


# R1
@pytest.mark.parametrize(
    ("toolchain", "step", "merged", "rendered"),
    [
        ({}, {}, {}, ""),
        ({"simfeat": 1}, {}, {"simfeat": 1}, "simfeat@flexlm:1"),
        ({}, {"lintfeat": 2}, {"lintfeat": 2}, "lintfeat@flexlm:2"),
        # The step overrides the toolchain per feature and adds its own; output is sorted.
        (
            {"simfeat": 1, "zfeat": 1},
            {"simfeat": 3, "afeat": 1},
            {"afeat": 1, "simfeat": 3, "zfeat": 1},
            "afeat@flexlm:1,simfeat@flexlm:3,zfeat@flexlm:1",
        ),
        # A feature given with an explicit server is the same feature: the step still wins.
        ({"simfeat@other": 1}, {"simfeat": 2}, {"simfeat": 2}, "simfeat@flexlm:2"),
        ({"simfeat": 1}, {"simfeat@other": 2}, {"simfeat@other": 2}, "simfeat@other:2"),
    ],
)
def test_merge_and_render(
    toolchain: dict[str, int], step: dict[str, int], merged: dict[str, int], rendered: str
) -> None:
    got = merge_licenses(toolchain, step)
    assert got == merged
    assert render_licenses(license_request(got, server="flexlm", fuzzy=False)) == rendered


# R1
def test_fuzzy() -> None:
    lic = {"simfeat": 2, "afeat": 1}
    assert license_request(lic, server="flexlm", fuzzy=True) == {"afeat": 1, "simfeat": 2}
    assert render_licenses(license_request(lic, server="flexlm", fuzzy=True)) == (
        "afeat:1,simfeat:2"
    )
    assert render_licenses(license_request(lic, server="lm2", fuzzy=False)) == (
        "afeat@lm2:1,simfeat@lm2:2"
    )


# R1: the executor puts the merged request on the sbatch line, the largest count per feature.
def test_executor_passes_licenses(tmp_path: Path, cas: FsCAS) -> None:
    slurm = ScriptedSlurm()
    ex = _executor(tmp_path, slurm, cas, license_server="lmsrv")
    b = batch("t[0]", "t[1]")
    b = dataclasses.replace(
        b,
        actions=(
            dataclasses.replace(b.actions[0], licenses={"simfeat": 1, "vipfeat": 1}),
            dataclasses.replace(b.actions[1], licenses={"simfeat": 2}),
        ),
    )
    assert batch_licenses(b.actions) == {"simfeat": 2, "vipfeat": 1}
    ex.submit(b)
    ex.submit(batch("plain"))
    with_lic, without = (c for c in slurm.calls if Path(c[0]).name == "sbatch")
    assert _licenses_opt(with_lic) == "simfeat@lmsrv:2,vipfeat@lmsrv:1"
    assert _licenses_opt(without) is None

    fuzzy = ScriptedSlurm()
    _executor(tmp_path, fuzzy, cas, license_fuzzy=True).submit(
        dataclasses.replace(
            batch("t[0]"), actions=(dataclasses.replace(spec("t[0]"), licenses={"simfeat": 1}),)
        )
    )
    assert _licenses_opt(fuzzy.calls[0]) == "simfeat:1"


# R2
@pytest.mark.parametrize(
    ("licenses", "message"),
    [
        ({"sim feat": 1}, "sim feat"),
        ({"sim,feat": 1}, "sim,feat"),
        ({"simfeat:2": 1}, "simfeat:2"),
        ({"": 1}, "''"),
        ({"simfeat@": 1}, "simfeat@"),
        ({"simfeat@a@b": 1}, "simfeat@a@b"),
        ({"simfeat": 0}, "positive"),
        ({"simfeat": -1}, "positive"),
        ({"simfeat": True}, "positive"),
        ({"simfeat": "1"}, "positive"),
    ],
)
def test_validation(licenses: dict[str, object], message: str) -> None:
    with pytest.raises(ExecutorError, match=message):
        license_request(licenses, server="flexlm", fuzzy=False)  # type: ignore[arg-type]


# R2: names that SLURM may not know are still allowed (it is the judge), with dots and dashes.
def test_validation_accepts_unknown_names() -> None:
    lic = {"Feat_1.x-y": 4}
    assert license_request(lic, server="lm-1.site", fuzzy=False) == {"Feat_1.x-y@lm-1.site": 4}


# R2
@pytest.mark.parametrize("server", ["", "flex lm", "a@b", "x:1"])
def test_server_validation(server: str) -> None:
    with pytest.raises(ConfigError, match="license_server"):
        license_request({"simfeat": 1}, server=server, fuzzy=False)
    with pytest.raises(ConfigError, match="license_server"):
        SlurmSettings.from_config({"slurm": {"license_server": server}})


# R2: SLURM rejects an unknown feature; the error names the step and every feature requested.
def test_unknown_feature_error_surface(tmp_path: Path, cas: FsCAS) -> None:
    slurm = ScriptedSlurm(sbatch_error="sbatch: error: Invalid license specification")
    ex = _executor(tmp_path, slurm, cas)
    b = batch("t[0]")
    b = dataclasses.replace(
        b, actions=(dataclasses.replace(b.actions[0], licenses={"nosuchfeat": 1, "simfeat": 1}),)
    )
    with pytest.raises(ExecutorError) as info:
        ex.submit(b)
    text = str(info.value)
    assert "nosuchfeat@flexlm" in text
    assert "simfeat@flexlm" in text
    assert "Invalid license specification" in text  # SLURM's own words
    assert "step 't'" in text
    assert "scontrol show licenses" in text

    # Other sbatch failures are not blamed on licenses.
    other = ScriptedSlurm(sbatch_error="sbatch: error: Batch job submission failed: QOS limit")
    with pytest.raises(ExecutorError) as info:
        _executor(tmp_path, other, cas).submit(b)
    assert "QOS limit" in str(info.value)
    assert "license" not in str(info.value).lower()


# R1: a server pinned in the flow is kept, fuzzy or not.
def test_fuzzy_keeps_pinned_server(tmp_path: Path, cas: FsCAS) -> None:
    lic = {"simfeat@lm2": 1, "afeat": 2}
    assert license_request(lic, server="flexlm", fuzzy=True) == {"afeat": 2, "simfeat@lm2": 1}
    slurm = ScriptedSlurm()
    _executor(tmp_path, slurm, cas, license_fuzzy=True).submit(
        dataclasses.replace(
            batch("t[0]"), actions=(dataclasses.replace(spec("t[0]"), licenses=lic),)
        )
    )
    assert _licenses_opt(slurm.calls[0]) == "afeat:2,simfeat@lm2:1"


# R2: two names for one SLURM license are an error, not a silent pick.
@pytest.mark.parametrize(
    ("licenses", "server"),
    [({"simfeat": 1, "simfeat@flexlm": 2}, "flexlm"), ({"simfeat@x": 1, "simfeat": 2}, "x")],
)
def test_same_license_twice_rejected(licenses: dict[str, int], server: str) -> None:
    with pytest.raises(ExecutorError, match="requested twice"):
        license_request(licenses, server=server, fuzzy=False)


# R2: a job without licenses is never blamed on them, whatever SLURM says.
def test_no_licenses_no_license_blame(tmp_path: Path, cas: FsCAS) -> None:
    assert license_error(ExecutorError("Invalid license specification"), {}, step="s") is None
    slurm = ScriptedSlurm(sbatch_error="sbatch: error: Invalid license specification")
    with pytest.raises(ExecutorError) as info:
        _executor(tmp_path, slurm, cas).submit(batch("plain"))
    assert "--licenses=" not in str(info.value)
    assert "scontrol show licenses" not in str(info.value)
