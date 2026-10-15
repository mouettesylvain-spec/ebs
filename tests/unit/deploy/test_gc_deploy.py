"""Static checks of the GC deployment files (P1-09 R7): one domain per run, nightly, as the
service account with the domain's group and direct database access."""

from __future__ import annotations

import configparser
import tomllib
from pathlib import Path

from ebs.gc.sweep import GcConfig
from ebs.meta.pg import MetadataConfig

GC = Path(__file__).resolve().parents[3] / "deploy" / "gc"


def _unit(name: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read_string((GC / name).read_text())
    return parser


# R7
def test_service_runs_one_domain_as_the_gc_account() -> None:
    service = _unit("ebs-gc@.service")["Service"]
    assert service["ExecStart"].endswith("ebs gc --domain %i")
    assert service["User"] == "ebs-gc"
    assert service["SupplementaryGroups"] == "%i"
    assert service["ReadWritePaths"] == "/cas/%i"
    assert service["NoNewPrivileges"] == "yes"


def test_timer_is_nightly() -> None:
    timer = _unit("ebs-gc@.timer")["Timer"]
    assert timer["OnCalendar"].startswith("*-*-* ")
    assert timer["Persistent"] == "true"


def test_example_config_is_valid() -> None:
    config = tomllib.loads((GC / "gc.toml.example").read_text())
    assert MetadataConfig.from_mapping(config["metadata"]).url.startswith("postgresql")
    assert GcConfig.from_mapping(config["gc"]) == GcConfig()
