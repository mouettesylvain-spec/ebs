"""A fake `lmutil` + `sacctmgr` for the FlexLM sync tests (process boundary only)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from ebs.admin.flexlm_sync import CommandResult, FlexlmSyncConfig

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "flexlm"
LICENSE = "27000@lic1.example"


def lmstat_fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


@dataclass
class FakeCommands:
    """`lmutil lmstat -a -c <license>` answers from `lmstat`; `sacctmgr show resource` lists
    `resources` (name -> LastConsumed, None = empty column); `sacctmgr update` is recorded."""

    lmstat: dict[str, CommandResult] = field(default_factory=dict)
    resources: dict[str, dict[str, int | None]] = field(default_factory=dict)
    show_result: CommandResult | None = None  # overrides the listing (e.g. slurmdbd down)
    update_rc: int = 0
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, argv: Sequence[str], *, timeout_s: float) -> CommandResult:
        argv = list(argv)
        self.calls.append(argv)
        if argv[1:4] == ["lmstat", "-a", "-c"]:
            return self.lmstat.get(argv[4], CommandResult(1, "", "no such server"))
        if argv[1:4] == ["-n", "-P", "show"]:
            if self.show_result is not None:
                return self.show_result
            server = next(a.split("=", 1)[1] for a in argv if a.startswith("server="))
            rows = self.resources.get(server, {})
            text = "".join(
                f"{name}|{server}|{'' if last is None else last}\n" for name, last in rows.items()
            )
            return CommandResult(0, text, "")
        if argv[1:4] == ["-i", "update", "resource"]:
            if self.update_rc == 0:
                name, server = argv[4], argv[5].split("=", 1)[1]
                last = int(argv[7].split("=", 1)[1])
                self.resources.setdefault(server, {})[name] = last
                return CommandResult(0, " Modified server resource ...\n", "")
            return CommandResult(self.update_rc, "", "sacctmgr: error: access denied")
        raise AssertionError(f"unexpected command {argv}")

    @property
    def updates(self) -> list[list[str]]:
        return [c for c in self.calls if c[1:4] == ["-i", "update", "resource"]]


@pytest.fixture
def commands() -> FakeCommands:
    return FakeCommands(
        lmstat={LICENSE: CommandResult(0, lmstat_fixture("lmstat_up.txt"), "")},
        resources={"flexlm": {"simfeat": 1, "lintfeat": 0, "gui_feat": None}},
    )


def make_config(
    tmp_path: Path, features: Sequence[str] = ("simfeat", "lintfeat", "gui_feat")
) -> FlexlmSyncConfig:
    return FlexlmSyncConfig.from_mapping(
        {
            "state_file": str(tmp_path / "state.json"),
            "servers": {"flexlm": {"license": LICENSE, "features": list(features)}},
        }
    )
