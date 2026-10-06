from __future__ import annotations

from pathlib import Path

import pytest

from ebs.core.errors import ConfigError
from ebs.rules.api import DEFAULT_LICENSE_ERROR_PATTERNS
from ebs.runner.config import DEFAULT_PASSTHROUGH_ENV, RunnerSettings


# R4 / R5: configured defaults.
def test_defaults() -> None:
    s = RunnerSettings.from_config({"cas": {"root": "/cas"}}, {"TMPDIR": "/scratch"})
    assert s.cas_root == Path("/cas")
    assert s.scratch_dir == Path("/scratch/ebs")
    assert s.passthrough_env == DEFAULT_PASSTHROUGH_ENV
    assert s.max_log == 2 << 30
    assert s.kill_grace_s == 30
    assert s.license_error_patterns == DEFAULT_LICENSE_ERROR_PATTERNS
    assert RunnerSettings.from_config({"cas": {"root": "/c"}}, {}).scratch_dir == Path("/tmp/ebs")


def test_from_config() -> None:
    s = RunnerSettings.from_config(
        {
            "cas": {"root": "/cas"},
            "scratch": {"dir": "${LOCAL}/ebs-runs"},
            "runner": {"passthrough_env": ["SITE_*"], "max_log": "64MiB", "kill_grace_s": 5},
            "rules": {"license_error_patterns": ["no seats"]},
        },
        {"LOCAL": "/local"},
    )
    assert s.scratch_dir == Path("/local/ebs-runs")
    assert s.passthrough_env == ("SITE_*",)
    assert s.max_log == 64 << 20
    assert s.kill_grace_s == 5
    assert s.license_error_patterns == ("no seats",)


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({}, r"\[cas\]\.root"),
        ({"cas": {"root": "relative"}}, r"absolute"),
        ({"cas": {"root": "/c"}, "scratch": {"dir": "${NOPE}/x"}}, r"NOPE"),
        ({"cas": {"root": "/c"}, "runner": {"max_log": "lots"}}, r"max_log"),
        ({"cas": {"root": "/c"}, "runner": {"max_log": 0}}, r"max_log"),
        ({"cas": {"root": "/c"}, "runner": {"passthrough_env": "SLURM_*"}}, r"passthrough_env"),
        ({"cas": {"root": "/c"}, "runner": {"kill_grace_s": -1}}, r"kill_grace_s"),
        ({"cas": {"root": "/c"}, "runner": {"bogus": 1}}, r"bogus"),
    ],
)
def test_invalid(config: dict[str, dict[str, object]], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        RunnerSettings.from_config(config, {})
