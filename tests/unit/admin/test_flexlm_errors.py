"""P3-08 R3: server down or partial output -> no update + alert log; features unknown to SLURM
-> one warning (across runs, through the state file)."""

from __future__ import annotations

import io
import json
import sys
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from ebs.admin import flexlm_sync
from ebs.admin.flexlm_sync import (
    CommandResult,
    FlexlmSync,
    FlexlmSyncConfig,
    SubprocessRunner,
    SyncReport,
)
from tests.unit.admin.conftest import LICENSE, FakeCommands, lmstat_fixture, make_config

_Logs = list[MutableMapping[str, Any]]


def _run(
    tmp_path: Path, commands: FakeCommands, config: FlexlmSyncConfig | None = None
) -> tuple[SyncReport, _Logs]:
    sync = FlexlmSync(config or make_config(tmp_path), runner=commands, out=io.StringIO())
    with capture_logs() as logs:
        report = sync.run()
    return report, logs


def _events(logs: _Logs, level: str) -> list[object]:
    return [e["event"] for e in logs if e["log_level"] == level]


# R3
@pytest.mark.parametrize(
    "result",
    [
        CommandResult(0, lmstat_fixture("lmstat_server_down.txt"), ""),  # lmstat exits 0
        CommandResult(0, lmstat_fixture("lmstat_daemon_down.txt"), ""),  # vendor daemon down
        CommandResult(0, lmstat_fixture("lmstat_truncated.txt"), ""),  # partial output
        CommandResult(1, lmstat_fixture("lmstat_up.txt"), "lmstat: error"),  # non-zero exit
        CommandResult(124, "", "lmutil: timed out after 30 s"),
    ],
    ids=["server-down", "daemon-down", "truncated", "exit-code", "timeout"],
)
def test_unavailable_server_updates_nothing_and_alerts(
    tmp_path: Path, commands: FakeCommands, result: CommandResult
) -> None:
    commands.lmstat[LICENSE] = result
    report, logs = _run(tmp_path, commands)
    assert commands.updates == []
    assert report.failed_servers == ["flexlm"]
    assert not report.ok
    assert _events(logs, "error") == ["flexlm_server_unavailable"]


# R3: a configured feature missing from otherwise good output is left alone (not set to 0)
def test_feature_missing_from_lmstat_is_not_updated(tmp_path: Path, commands: FakeCommands) -> None:
    commands.resources["flexlm"]["absentfeat"] = 7
    config = make_config(tmp_path, features=["simfeat", "absentfeat"])
    report, logs = _run(tmp_path, commands, config)
    assert [c[4] for c in commands.updates] == ["simfeat"]
    assert report.unreported == [("flexlm", "absentfeat")]
    assert not report.ok
    assert _events(logs, "error") == ["flexlm_feature_unreported"]


# R3: slurmdbd down -> no blind updates
def test_sacctmgr_listing_failure_updates_nothing(tmp_path: Path, commands: FakeCommands) -> None:
    commands.show_result = CommandResult(1, "", "sacctmgr: error: slurmdbd: connection refused")
    report, logs = _run(tmp_path, commands)
    assert commands.updates == []
    assert report.failed_servers == ["flexlm"]
    assert not report.ok
    assert _events(logs, "error") == ["flexlm_slurm_unavailable"]


# R3: unknown to SLURM -> warn once, never `sacctmgr update` it
def test_feature_absent_in_slurm_warns_once(tmp_path: Path, commands: FakeCommands) -> None:
    del commands.resources["flexlm"]["gui_feat"]
    report, logs = _run(tmp_path, commands)
    assert report.missing_in_slurm == [("flexlm", "gui_feat")]
    assert report.ok  # a config mismatch to fix, not an outage
    assert "gui_feat" not in [c[4] for c in commands.updates]
    assert _events(logs, "warning") == ["flexlm_feature_not_in_slurm"]

    report, logs = _run(tmp_path, commands)  # next timer run
    assert report.missing_in_slurm == [("flexlm", "gui_feat")]
    assert _events(logs, "warning") == []


# R3: once the resource exists again, a later disappearance warns again
def test_warning_rearms_when_feature_reappears(tmp_path: Path, commands: FakeCommands) -> None:
    del commands.resources["flexlm"]["gui_feat"]
    _run(tmp_path, commands)
    commands.resources["flexlm"]["gui_feat"] = 1
    _run(tmp_path, commands)
    del commands.resources["flexlm"]["gui_feat"]
    _, logs = _run(tmp_path, commands)
    assert _events(logs, "warning") == ["flexlm_feature_not_in_slurm"]


# R3: SLURM matches resource names without case; the update uses SLURM's spelling
def test_resource_names_match_case_insensitively(tmp_path: Path, commands: FakeCommands) -> None:
    commands.resources["flexlm"] = {"SimFeat": 0, "lintfeat": 0, "gui_feat": 1}
    report, _ = _run(tmp_path, commands)
    assert [c[4] for c in commands.updates] == ["SimFeat"]
    assert report.missing_in_slurm == []


# R3: a corrupt state file is replaced, not fatal
def test_corrupt_state_file_is_ignored(tmp_path: Path, commands: FakeCommands) -> None:
    (tmp_path / "state.json").write_text("{not json")
    del commands.resources["flexlm"]["gui_feat"]
    report, logs = _run(tmp_path, commands)
    assert report.ok
    assert "flexlm_feature_not_in_slurm" in _events(logs, "warning")
    _, logs = _run(tmp_path, commands)
    assert _events(logs, "warning") == []


# R3: one server down does not stop the others
def test_servers_are_independent(tmp_path: Path, commands: FakeCommands) -> None:
    commands.lmstat["27001@lic2.example"] = CommandResult(
        0, lmstat_fixture("lmstat_server_down.txt"), ""
    )
    commands.resources["vendorb"] = {"simfeat": 0}
    config = FlexlmSyncConfig.from_mapping(
        {
            "state_file": str(tmp_path / "state.json"),
            "servers": {
                "vendorb": {"license": "27001@lic2.example", "features": ["simfeat"]},
                "flexlm": {"license": LICENSE, "features": ["simfeat"]},
            },
        }
    )
    report, _ = _run(tmp_path, commands, config)
    assert report.failed_servers == ["vendorb"]
    assert report.updated == {("flexlm", "simfeat"): 3}


# R3: the real runner turns a missing binary or a hang into a failed result, not a crash
def test_subprocess_runner_missing_binary(tmp_path: Path) -> None:
    result = SubprocessRunner()([str(tmp_path / "no-lmutil"), "lmstat"], timeout_s=5)
    assert result.returncode == 127
    assert "not found" in result.stderr


def test_subprocess_runner_timeout() -> None:
    result = SubprocessRunner()([sys.executable, "-c", "while True: pass"], timeout_s=0.2)
    assert result.returncode == 124


# R3: lmstat's down markers are errors even when a feature section follows
@pytest.mark.parametrize(
    "marker",
    [
        "lic1.example: License server machine is down or not responding.",
        "lic1.example: license server DOWN",
        "Error getting status: Cannot connect to license server system. (-15,10:115)",
        "Cannot connect to license server system. (-15,570:111)",
        "     vendora: The desired vendor daemon is down. (-97,121)",
    ],
)
def test_down_markers_with_feature_section_are_errors(marker: str) -> None:
    text = lmstat_fixture("lmstat_up.txt").replace(
        "Feature usage info:", f"{marker}\nFeature usage info:"
    )
    with pytest.raises(flexlm_sync.FlexlmError, match="unavailable"):
        flexlm_sync.parse_lmstat(text)


# R3: the alert says what lmstat said
def test_server_down_reason_is_logged(tmp_path: Path, commands: FakeCommands) -> None:
    commands.lmstat[LICENSE] = CommandResult(0, lmstat_fixture("lmstat_server_down.txt"), "")
    _, logs = _run(tmp_path, commands)
    (event,) = [e for e in logs if e["event"] == "flexlm_server_unavailable"]
    assert "lmgrd is not running" in event["reason"]


# R3: an outage between two runs keeps the warn-once memory
def test_warn_once_survives_a_server_outage(tmp_path: Path, commands: FakeCommands) -> None:
    del commands.resources["flexlm"]["gui_feat"]
    _run(tmp_path, commands)
    up = commands.lmstat[LICENSE]
    commands.lmstat[LICENSE] = CommandResult(0, lmstat_fixture("lmstat_server_down.txt"), "")
    _run(tmp_path, commands)
    state = json.loads((tmp_path / "state.json").read_text())
    assert state == {"missing": {"flexlm": ["gui_feat"]}}
    commands.lmstat[LICENSE] = up
    _, logs = _run(tmp_path, commands)
    assert _events(logs, "warning") == []


# R3: a state file of the wrong shape is replaced, never a crash
@pytest.mark.parametrize(
    "content",
    [
        '{"missing": ["gui_feat"]}',
        '{"missing": {"flexlm": "gui_feat"}}',
        "[]",
        '{"other": 1}',
    ],
)
def test_wrong_shape_state_file_is_ignored(
    tmp_path: Path, commands: FakeCommands, content: str
) -> None:
    (tmp_path / "state.json").write_text(content)
    del commands.resources["flexlm"]["gui_feat"]
    report, logs = _run(tmp_path, commands)
    assert report.ok
    assert _events(logs, "warning") == ["flexlm_state_unreadable", "flexlm_feature_not_in_slurm"]


# R3: an unwritable state file is a warning; the sync itself still happens
def test_unwritable_state_file_is_a_warning(tmp_path: Path, commands: FakeCommands) -> None:
    config = FlexlmSyncConfig.from_mapping(
        {
            "state_file": str(tmp_path / "nodir" / "state.json"),
            "servers": {"flexlm": {"license": LICENSE, "features": ["simfeat", "gui_feat"]}},
        }
    )
    del commands.resources["flexlm"]["gui_feat"]
    report, logs = _run(tmp_path, commands, config)
    assert report.updated == {("flexlm", "simfeat"): 3}
    assert "flexlm_state_unwritable" in _events(logs, "warning")
    assert list(tmp_path.glob("**/*.tmp")) == []


# R3: the state file is replaced atomically: a failed rename keeps the old contents
def test_state_write_is_atomic(
    tmp_path: Path, commands: FakeCommands, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = '{"missing": {"flexlm": ["x"]}}'
    (tmp_path / "state.json").write_text(old)

    def fail(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("ebs.admin.flexlm_sync.os.replace", fail)
    _run(tmp_path, commands)
    assert (tmp_path / "state.json").read_text() == old
    assert list(tmp_path.glob("*.tmp")) == []


# R3: a configured name in another case still finds lmstat's and SLURM's spelling
def test_configured_feature_case_differs(tmp_path: Path, commands: FakeCommands) -> None:
    commands.resources["flexlm"] = {"simfeat": 0}
    report, _ = _run(tmp_path, commands, make_config(tmp_path, features=["SimFeat"]))
    assert [c[4:] for c in commands.updates] == [
        ["simfeat", "server=flexlm", "set", "LastConsumed=3"]
    ]
    assert report.missing_in_slurm == []
    assert report.unreported == []


# R3: every command gets the configured timeout (the unit allows 50 s per run)
def test_configured_timeout_is_passed_to_every_command(tmp_path: Path) -> None:
    timeouts: list[float] = []
    inner = FakeCommands(
        lmstat={LICENSE: CommandResult(0, lmstat_fixture("lmstat_up.txt"), "")},
        resources={"flexlm": {"simfeat": 0}},
    )

    def runner(argv: Any, *, timeout_s: float) -> CommandResult:
        timeouts.append(timeout_s)
        return inner(argv, timeout_s=timeout_s)

    config = FlexlmSyncConfig.from_mapping(
        {"timeout_s": 5, "servers": {"flexlm": {"license": LICENSE, "features": ["simfeat"]}}}
    )
    FlexlmSync(config, runner=runner, out=io.StringIO()).run()
    assert timeouts == [5.0, 5.0, 5.0]  # lmstat, show, update
