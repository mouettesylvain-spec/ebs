"""P3-08 R2: LastConsumed = licenses in use (by anyone), pushed with sacctmgr only when changed."""

from __future__ import annotations

import io
from pathlib import Path

from ebs.admin.flexlm_sync import CommandResult, FlexlmSync
from tests.unit.admin.conftest import LICENSE, FakeCommands, lmstat_fixture, make_config


def _sync(tmp_path: Path, commands: FakeCommands, *, dry_run: bool = False) -> FlexlmSync:
    return FlexlmSync(make_config(tmp_path), runner=commands, out=io.StringIO(), dry_run=dry_run)


# R2: in use by anyone (3 sim checkouts, 1 GUI session), exact sacctmgr argv
def test_updates_changed_features_with_sacctmgr(tmp_path: Path, commands: FakeCommands) -> None:
    report = _sync(tmp_path, commands).run()
    assert commands.updates == [
        ["sacctmgr", "-i", "update", "resource", "simfeat", "server=flexlm",
         "set", "LastConsumed=3"],
        ["sacctmgr", "-i", "update", "resource", "gui_feat", "server=flexlm",
         "set", "LastConsumed=1"],
    ]  # fmt: skip
    assert report.updated == {("flexlm", "simfeat"): 3, ("flexlm", "gui_feat"): 1}
    assert report.unchanged == [("flexlm", "lintfeat")]
    assert report.ok


# R2: only when changed
def test_second_run_updates_nothing(tmp_path: Path, commands: FakeCommands) -> None:
    _sync(tmp_path, commands).run()
    commands.calls.clear()
    report = _sync(tmp_path, commands).run()
    assert commands.updates == []
    assert report.updated == {}
    assert report.ok


# R2: the current value is read from SLURM with one listing per server
def test_reads_current_last_consumed_from_slurm(tmp_path: Path, commands: FakeCommands) -> None:
    _sync(tmp_path, commands).run()
    shows = [c for c in commands.calls if c[1:4] == ["-n", "-P", "show"]]
    assert shows == [
        ["sacctmgr", "-n", "-P", "show", "resource", "server=flexlm",
         "format=Name,Server,LastConsumed"],
    ]  # fmt: skip


# R2: usage dropping to zero is a change too
def test_drop_to_zero_is_pushed(tmp_path: Path, commands: FakeCommands) -> None:
    commands.resources["flexlm"]["lintfeat"] = 5
    _sync(tmp_path, commands).run()
    assert ["sacctmgr", "-i", "update", "resource", "lintfeat", "server=flexlm",
            "set", "LastConsumed=0"] in commands.updates  # fmt: skip


# R2: SLURM showing no LastConsumed yet is a change, even to 0
def test_empty_last_consumed_is_pushed(tmp_path: Path, commands: FakeCommands) -> None:
    commands.resources["flexlm"]["lintfeat"] = None
    _sync(tmp_path, commands).run()
    assert ["sacctmgr", "-i", "update", "resource", "lintfeat", "server=flexlm",
            "set", "LastConsumed=0"] in commands.updates  # fmt: skip


# R2: dry run prints the commands and changes nothing
def test_dry_run_prints_commands_only(tmp_path: Path, commands: FakeCommands) -> None:
    out = io.StringIO()
    report = FlexlmSync(make_config(tmp_path), runner=commands, out=out, dry_run=True).run()
    assert commands.updates == []
    assert out.getvalue().splitlines() == [
        "sacctmgr -i update resource simfeat server=flexlm set LastConsumed=3",
        "sacctmgr -i update resource gui_feat server=flexlm set LastConsumed=1",
    ]
    assert report.updated == {("flexlm", "simfeat"): 3, ("flexlm", "gui_feat"): 1}
    assert not (tmp_path / "state.json").exists()


# R2: a failing update is reported, the other features are still pushed
def test_failed_update_is_not_ok(tmp_path: Path, commands: FakeCommands) -> None:
    commands.update_rc = 1
    report = _sync(tmp_path, commands).run()
    assert len(commands.updates) == 2
    assert report.updated == {}
    assert report.failed_updates == [("flexlm", "simfeat"), ("flexlm", "gui_feat")]
    assert not report.ok


# R2: only configured features are pushed, whatever else the server serves
def test_unconfigured_features_are_ignored(tmp_path: Path, commands: FakeCommands) -> None:
    commands.lmstat[LICENSE] = CommandResult(0, lmstat_fixture("lmstat_up.txt"), "")
    sync = FlexlmSync(
        make_config(tmp_path, features=["lintfeat"]), runner=commands, out=io.StringIO()
    )
    report = sync.run()
    assert commands.updates == []
    assert report.unchanged == [("flexlm", "lintfeat")]
