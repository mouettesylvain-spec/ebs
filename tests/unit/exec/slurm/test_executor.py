"""SlurmExecutor against an in-process scripted SLURM (P1-02 R1, R3, R4, R7, R8).

Polling and cancellation against FakeSlurm (R5, R6) are in
tests/integration/slurm/test_slurm_executor.py.
"""

from __future__ import annotations

import dataclasses
import errno
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from ebs.cas.fs import FsCAS
from ebs.core.clock import FakeClock
from ebs.core.digest import Digest
from ebs.core.errors import ConfigError, ExecutorError, MetadataError
from ebs.exec.api import ExecutorBusy, JobHandle
from ebs.exec.slurm.arrays import load_array_map
from ebs.exec.slurm.cli import CommandResult
from ebs.exec.slurm.executor import (
    JOB_TIME_GRACE_S,
    LOST_AFTER_POLLS,
    SlurmExecutor,
    SlurmSettings,
)
from ebs.flow.model import Resources
from ebs.meta.api import BuildId
from ebs.meta.memory import InMemoryMetadataStore
from ebs.runner.errors import format_infra_line
from tests.helpers.executor import PLAN, batch
from tests.helpers.runner import new_build
from tests.helpers.slurm import ScriptedSlurm

RUNNER = ("/opt/ebs/bin/ebs-runner",)


@dataclasses.dataclass
class Env:
    tmp_path: Path
    slurm: ScriptedSlurm
    cas: FsCAS
    store: InMemoryMetadataStore
    clock: FakeClock

    def executor(self, **overrides: object) -> SlurmExecutor:
        base: dict[str, object] = {"debug_root": self.tmp_path / "debug"}
        base.update(overrides)
        return SlurmExecutor(
            cli=self.slurm.cli(),
            cas=self.cas,
            settings=SlurmSettings(**base),  # type: ignore[arg-type]
            store=self.store,
            runner_argv=RUNNER,
            env={"PATH": "/usr/bin:/bin"},
            clock=self.clock,
        )

    def build(self, *action_ids: str) -> BuildId:
        return new_build(self.store, *action_ids, cache_mode="read")


@pytest.fixture
def env(tmp_path: Path, cas: FsCAS, fake_clock: FakeClock) -> Env:
    return Env(tmp_path, ScriptedSlurm(), cas, InMemoryMetadataStore(clock=fake_clock), fake_clock)


def _sbatches(slurm: ScriptedSlurm) -> list[list[str]]:
    return [c for c in slurm.calls if Path(c[0]).name == "sbatch"]


def _opt(argv: list[str], name: str) -> str | None:
    prefix = f"--{name}="
    return next((a[len(prefix) :] for a in argv if a.startswith(prefix)), None)


# R1
def test_one_action_is_one_plain_job(env: Env) -> None:
    ex = env.executor()
    build = env.build("t[0]")
    (h,) = ex.submit(batch("t[0]", build=build))
    (argv,) = _sbatches(env.slurm)
    assert _opt(argv, "array") is None
    assert h == JobHandle(executor="slurm", job_id="100", action_id="t[0]")
    script = env.slurm.scripts[0]
    assert "--action 't[0]'" in script
    assert "--array-map" not in script


# R1: a batch is one array whose map, stored in the CAS, names the action of each index.
def test_batch_is_one_array_with_map(env: Env) -> None:
    ids = [f"t[{i}]" for i in range(5)]
    ex = env.executor()
    handles = ex.submit(batch(*ids, build=env.build(*ids)))
    (argv,) = _sbatches(env.slurm)
    assert _opt(argv, "array") == "0-4%200"
    assert [h.job_id for h in handles] == [f"100_{i}" for i in range(5)]
    assert [h.action_id for h in handles] == ids
    script = env.slurm.scripts[0]
    map_arg = script.split("--array-map ", 1)[1].split()[0]
    loaded = load_array_map(env.cas, _digest(map_arg))
    assert loaded.plan == PLAN
    assert list(loaded.actions) == ids


# R1: arrays above max_array_size are split; each part has its own map.
def test_large_batch_is_split(env: Env) -> None:
    ids = [f"t[{i}]" for i in range(7)]
    ex = env.executor(max_array_size=3)
    handles = ex.submit(batch(*ids, build=env.build(*ids)))
    arrays = [_opt(a, "array") for a in _sbatches(env.slurm)]
    assert arrays == ["0-2%200", "0-2%200", None]
    assert [h.job_id for h in handles] == [
        "100_0",
        "100_1",
        "100_2",
        "101_0",
        "101_1",
        "101_2",
        "102",
    ]
    maps = [s.split("--array-map ", 1)[1].split()[0] for s in env.slurm.scripts[:2]]
    assert [list(load_array_map(env.cas, _digest(m)).actions) for m in maps] == [ids[:3], ids[3:6]]


# R2: the step's max_parallel overrides the configured throttle.
def test_throttle_from_step(env: Env) -> None:
    ids = ["t[0]", "t[1]"]
    b = batch(*ids, build=env.build(*ids))
    b = dataclasses.replace(
        b,
        actions=tuple(
            dataclasses.replace(a, resources=Resources(max_parallel=1)) for a in b.actions
        ),
    )
    env.executor(array_throttle=50).submit(b)
    (argv,) = _sbatches(env.slurm)
    assert _opt(argv, "array") == "0-1%1"


# R3: --export=NONE, the job's env from the env file only, logs under the debug area.
def test_sbatch_options(env: Env) -> None:
    ex = env.executor(default_partition="short")
    build = env.build("t[0]", "t[1]", "t[2]")
    b = batch("t[0]", "t[1]", build=build, cpus={"t[0]": 2, "t[1]": 2}, mem="3G")
    b = dataclasses.replace(
        b,
        actions=tuple(
            dataclasses.replace(a, resources=Resources(cpus=2, mem="3G", time="10m"))
            for a in b.actions
        ),
    )
    ex.submit(b)
    ex.submit(batch("t[2]", build=build))
    array, single = _sbatches(env.slurm)
    logs = env.tmp_path / "debug" / "test" / str(build) / "slurm"
    assert "--export=NONE" in array
    assert _opt(array, "output") == str(logs / "%A_%a.out")
    assert _opt(single, "output") == str(logs / "%j.out")
    assert _opt(array, "error") is None  # stderr (with the infra line) goes to the same file
    assert _opt(array, "partition") == "short"
    assert _opt(array, "cpus-per-task") == "2"
    assert _opt(array, "mem") == "3072M"
    # The runner times the tool out itself; SLURM's limit leaves it time to clean up and report.
    assert JOB_TIME_GRACE_S == 120
    assert _opt(array, "time") == "0-00:12:00"
    assert _opt(single, "time") is None
    assert "--no-requeue" in array
    env_file = Path(_opt(array, "export-file") or "")
    assert env_file.read_bytes() == b"PATH=/usr/bin:/bin\x00"
    assert (env_file.stat().st_mode & 0o777) == 0o600
    assert logs.is_dir()
    for script in env.slurm.scripts:
        assert script.startswith("#!/bin/bash\n")
        assert "set -euo pipefail\n" in script
        assert f"exec {RUNNER[0]} --plan {PLAN}" in script
        assert f"--domain test --build {build}" in script


def test_no_build_logs_under_local(env: Env) -> None:
    env.executor().submit(batch("t[0]"))
    (argv,) = _sbatches(env.slurm)
    assert _opt(argv, "output") == str(
        env.tmp_path / "debug" / "test" / "local" / "slurm" / "%j.out"
    )
    assert _opt(argv, "comment") is None
    assert _opt(argv, "job-name") == "ebs:local:t"
    assert "--build" not in env.slurm.scripts[0]


# R4
def test_job_name_and_comment(env: Env) -> None:
    ids = ["t[0]", "t[1]"]
    build = env.build(*ids)
    uuid = env.store.get_build(build).uuid
    ex = env.executor()
    ex.submit(batch(*ids, build=build))
    ex.submit(batch("t[0]", build=build))
    for argv in _sbatches(env.slurm):
        assert _opt(argv, "job-name") == f"ebs:{uuid.hex[:8]}:t"
        assert _opt(argv, "comment") == str(uuid)


# R7
def test_job_ids_recorded_before_return(env: Env) -> None:
    ids = ["t[0]", "t[1]", "t[2]"]
    build = env.build(*ids)
    ex = env.executor(max_array_size=2)
    handles = ex.submit(batch(*ids, build=build))
    rows = {r.action_id: r for r in env.store.list_actions(build)}
    for h in handles:
        assert rows[h.action_id].slurm_job_id == h.job_id
        assert rows[h.action_id].state == "pending"
    assert [rows[a].slurm_job_id for a in ids] == ["100_0", "100_1", "101"]


# R7: a job the store cannot record is cancelled, so no untracked job is left behind.
def test_record_failure_cancels_submitted_jobs(env: Env) -> None:
    ids = ["t[0]", "t[1]", "t[2]"]
    build = env.build(*ids)
    calls = 0
    real = env.store.set_action_state

    def flaky(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise MetadataError("metadata service unreachable")
        real(*args, **kwargs)  # type: ignore[arg-type]

    env.store.set_action_state = flaky  # type: ignore[method-assign]
    ex = env.executor(max_array_size=2)
    with pytest.raises(ExecutorError, match="metadata service unreachable"):
        ex.submit(batch(*ids, build=build))
    assert env.slurm.count("scancel") == 1
    assert {j.state for j in env.slurm.jobs} == {"CANCELLED"}


def test_sbatch_failure_cancels_earlier_parts(env: Env) -> None:
    ids = [f"t[{i}]" for i in range(4)]
    env.slurm.fail_sbatch_after = 1
    ex = env.executor(max_array_size=2)
    with pytest.raises(ExecutorError, match="QOS limit"):
        ex.submit(batch(*ids, build=env.build(*ids)))
    (scancel,) = [c for c in env.slurm.calls if Path(c[0]).name == "scancel"]
    assert scancel[1:] == ["100"]
    # Nothing is in flight: the next batch is accepted.
    env.slurm.fail_sbatch_after = None
    ex.submit(batch("t[0]", build=env.build("t[0]")))


# R8: sbatch calls per minute and in-flight jobs are capped; over the limits the batch waits.
def test_rate_limit_and_backpressure(env: Env) -> None:
    ex = env.executor(max_submits_per_min=2, max_pending_jobs=5)
    ex.submit(batch("t[0]"))
    env.clock.advance(10)
    ex.submit(batch("t[1]"))
    with pytest.raises(ExecutorBusy) as busy:
        ex.submit(batch("t[2]"))
    assert busy.value.retry_after_s == pytest.approx(50.0)  # until the first call leaves the window
    assert env.slurm.count("sbatch") == 2
    env.clock.advance(50)
    ex.submit(batch("t[2]"))  # the window has room again
    assert env.slurm.count("sbatch") == 3

    # 3 jobs in flight, limit 5: a batch of 3 more must wait (array tasks count one each)...
    env.clock.advance(120)
    with pytest.raises(ExecutorBusy) as busy:
        ex.submit(batch("u[0]", "u[1]", "u[2]"))
    assert busy.value.retry_after_s > 0
    assert env.slurm.count("sbatch") == 3
    # ...until jobs finish (the executor learns it by polling).
    env.slurm.set_state("100", "COMPLETED")
    env.slurm.set_state("101", "COMPLETED")
    ex.poll([JobHandle("slurm", "100", "t[0]"), JobHandle("slurm", "101", "t[1]")])
    handles = ex.submit(batch("u[0]", "u[1]", "u[2]"))
    assert len(handles) == 3
    env.clock.advance(60)
    ex.submit(batch("v[0]"))  # 5 in flight: exactly the limit
    with pytest.raises(ExecutorBusy):
        ex.submit(batch("w[0]"))  # the sixth must wait, although the rate allows it
    assert env.slurm.count("sbatch") == 5


# R8: a batch larger than the whole limit still goes when nothing else is in flight.
def test_backpressure_never_deadlocks(env: Env) -> None:
    ex = env.executor(max_pending_jobs=2, max_submits_per_min=1)
    ids = [f"t[{i}]" for i in range(4)]
    assert len(ex.submit(batch(*ids))) == 4


def test_unknown_handle_and_wrong_domain(env: Env, tmp_path: Path) -> None:
    ex = env.executor()
    with pytest.raises(ExecutorError, match="unknown job handle"):
        ex.poll([JobHandle("slurm", "1", "x")])
    other = SlurmExecutor(
        cli=env.slurm.cli(),
        cas=FsCAS(tmp_path / "cas2", "other"),
        settings=SlurmSettings(debug_root=tmp_path / "debug"),
    )
    with pytest.raises(ExecutorError, match="domain"):
        other.submit(batch("t[0]"))


# R5 (unit view): one squeue for every in-flight handle; terminal ones are not asked again.
def test_poll_queries_once_and_maps_states(env: Env) -> None:
    ids = [f"t[{i}]" for i in range(4)]
    ex = env.executor()
    build = env.build(*ids)
    handles = ex.submit(batch(*ids, build=build))
    single = ex.submit(batch("s[0]"))[0]
    env.slurm.set_state("100_0", "RUNNING")
    env.slurm.set_state("100_1", "COMPLETED")
    env.slurm.set_state("100_2", "PENDING", reason="Licenses")
    env.slurm.set_state("100_3", "FAILED", exit_code=75)
    env.slurm.set_state("101", "FAILED", exit_code=76)
    logs = env.tmp_path / "debug" / "test" / str(build) / "slurm"
    (logs / "100_3.out").write_text(
        "noise\n" + format_infra_line("license", "no msimhdlsim") + "\n"
    )
    before = env.slurm.count("squeue")
    states = ex.poll([*handles, single])
    assert env.slurm.count("squeue") == before + 1
    assert [states[h].state for h in handles] == ["running", "done", "pending", "infra_failed"]
    assert states[handles[2]].pending_reason == "licenses"
    assert states[handles[3]].infra_reason == "license"
    assert states[single].infra_reason == "input_verification"
    # Only the two unfinished tasks are queried next time.
    ex.poll([*handles, single])
    last = [c for c in env.slurm.calls if Path(c[0]).name == "squeue"][-1]
    assert _opt(last, "jobs") == "100"
    # Nothing unfinished: no SLURM call at all.
    env.slurm.set_state("100", "COMPLETED")
    ex.poll(handles)
    calls = len(env.slurm.calls)
    assert {s.state for s in ex.poll([*handles, single]).values()} == {"done", "infra_failed"}
    assert len(env.slurm.calls) == calls


def test_missing_log_means_other(env: Env) -> None:
    ex = env.executor()
    (h,) = ex.submit(batch("t[0]"))
    env.slurm.set_state("100", "FAILED", exit_code=75)
    status = ex.poll([h])[h]
    assert status.infra_reason == "other"


# R6 (unit view): one scancel; whole arrays by their id; finished jobs keep their state.
def test_cancel_one_call(env: Env) -> None:
    ids = [f"t[{i}]" for i in range(3)]
    ex = env.executor()
    arr = ex.submit(batch(*ids))
    part = ex.submit(batch("u[0]", "u[1]"))
    done = ex.submit(batch("d[0]"))[0]
    env.slurm.set_state("102", "COMPLETED")
    ex.cancel([*arr, part[0], done])
    (scancel,) = [c for c in env.slurm.calls if Path(c[0]).name == "scancel"]
    assert scancel[1:] == ["100", "101_0"]
    states = ex.poll([*arr, *part, done])
    assert [states[h].state for h in arr] == ["cancelled"] * 3
    assert states[part[0]].state == "cancelled"
    assert states[part[1]].state == "pending"
    assert states[done].state == "done"
    # Cancelling again (or only finished jobs) calls nothing.
    calls = len(env.slurm.calls)
    ex.cancel([*arr, done])
    assert len(env.slurm.calls) == calls


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ({"max_array_size": 0}, "max_array_size"),
        ({"array_throttle": "x"}, "array_throttle"),
        ({"max_pending_jobs": True}, "max_pending_jobs"),
        ({"max_submits_per_min": -1}, "max_submits_per_min"),
        ({"default_partition": 3}, "default_partition"),
        ({"bin_dir": "relative/bin"}, "bin_dir"),
        ({"poll_min_s": 0}, "poll_min_s"),
        ({"poll_min_s": 10, "poll_max_s": 5}, "poll_max_s"),
        ({"max_pendng_jobs": 3}, "unknown key"),
    ],
)
def test_settings_validation(section: dict[str, object], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        SlurmSettings.from_config({"slurm": section})


def test_settings_defaults_and_values() -> None:
    s = SlurmSettings.from_config({})
    assert (s.max_array_size, s.array_throttle, s.max_pending_jobs, s.max_submits_per_min) == (
        1000,
        200,
        2000,
        60,
    )
    assert s.default_partition is None
    assert s.debug_root == Path("/debug")
    assert s.bin_dir is None
    s = SlurmSettings.from_config(
        {
            "slurm": {
                "default_partition": "short",
                "bin_dir": "/opt/slurm/bin",
                "array_throttle": 8,
            },
            "debug": {"root": "/nfs/debug"},
        }
    )
    assert s.default_partition == "short"
    assert s.bin_dir == Path("/opt/slurm/bin")
    assert s.array_throttle == 8
    assert s.debug_root == Path("/nfs/debug")
    with pytest.raises(ConfigError, match=r"\[debug\].root"):
        SlurmSettings.from_config({"debug": {"root": "debug"}})


def test_default_env_is_minimal(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EBS_CONFIG", "/etc/ebs/site.toml")
    monkeypatch.setenv("SECRET_TOKEN", "x")
    ex = SlurmExecutor(
        cli=env.slurm.cli(), cas=env.cas, settings=SlurmSettings(debug_root=env.tmp_path / "d")
    )
    ex.submit(batch("t[0]"))
    (argv,) = _sbatches(env.slurm)
    data = Path(_opt(argv, "export-file") or "").read_bytes()
    assert b"EBS_CONFIG=/etc/ebs/site.toml\x00" in data
    assert f"PATH={os.environ['PATH']}\x00".encode() in data
    assert b"SECRET_TOKEN" not in data
    assert "exec ebs-runner " in env.slurm.scripts[0]


def _digest(text: str) -> Digest:
    return Digest.parse(text)


# R8: a job SLURM has forgotten (the driver gives up on it) stops counting as in flight, so the
# executor never refuses batches forever.
def test_lost_job_leaves_in_flight(env: Env) -> None:
    ex = env.executor(max_pending_jobs=1)
    (h,) = ex.submit(batch("t[0]"))
    env.slurm.jobs.clear()  # SLURM lost it (purged, controller state lost)
    with pytest.raises(ExecutorBusy):
        ex.submit(batch("t[1]"))
    for _ in range(LOST_AFTER_POLLS - 1):
        assert ex.poll([h]) == {}  # left out: the driver counts the misses
    final = ex.poll([h])[h]
    assert (final.state, final.infra_reason) == ("infra_failed", "other")
    assert ex.in_flight() == 0
    env.clock.advance(60)
    ex.submit(batch("t[1]"))


# R7: a job script that cannot be written (full or read-only debug area) after an earlier part
# was submitted: that part is cancelled and the OSError becomes an ExecutorError.
def test_script_write_failure_cancels_earlier_parts(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = [f"t[{i}]" for i in range(4)]
    ex = env.executor(max_array_size=2)
    real = tempfile.mkstemp
    calls = 0

    def mkstemp(*args: Any, **kwargs: Any) -> tuple[int, str]:
        nonlocal calls
        calls += 1
        if calls == 3:  # env file, script of part 1, script of part 2
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(*args, **kwargs)

    monkeypatch.setattr(tempfile, "mkstemp", mkstemp)
    with pytest.raises(ExecutorError, match=r"cannot write the job script.*No space left"):
        ex.submit(batch(*ids, build=env.build(*ids)))
    (scancel,) = [c for c in env.slurm.calls if Path(c[0]).name == "scancel"]
    assert scancel[1:] == ["100"]
    assert ex.in_flight() == 0


def test_env_file_write_failure(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    def mkstemp(*args: Any, **kwargs: Any) -> tuple[int, str]:
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(tempfile, "mkstemp", mkstemp)
    with pytest.raises(ExecutorError, match=r"cannot write the job env file.*Permission denied"):
        env.executor().submit(batch("t[0]"))
    assert env.slurm.count("sbatch") == 0


# R6: cancel still scancels everything when SLURM cannot be queried first.
def test_cancel_when_query_fails(env: Env) -> None:
    ex = env.executor()
    handles = ex.submit(batch("t[0]", "t[1]"))
    real = env.slurm._squeue

    def broken(argv: list[str]) -> CommandResult:
        return CommandResult(1, "", "squeue: error: Invalid user for SlurmUser")

    env.slurm._squeue = broken  # type: ignore[method-assign]
    ex.cancel(handles)
    env.slurm._squeue = real  # type: ignore[method-assign]
    (scancel,) = [c for c in env.slurm.calls if Path(c[0]).name == "scancel"]
    assert scancel[1:] == ["100"]
    assert {s.state for s in ex.poll(handles).values()} == {"cancelled"}
