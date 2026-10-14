"""SlurmExecutor against FakeSlurm: polling, cancellation, a full fake regression (P1-02 R5, R6)."""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from pathlib import Path

import pytest

from ebs.cas.fs import FsCAS
from ebs.exec.slurm.cli import CommandResult, SlurmCli, SubprocessRunner
from ebs.exec.slurm.executor import SlurmExecutor, SlurmSettings
from ebs.exec.slurm.models import JobRef, SlurmJobInfo
from tests.fakes.slurm import FakeSlurm
from tests.helpers.executor import FakeRunner, batch, wait_states
from tests.helpers.runner import DOMAIN
from tests.helpers.wait import wait_until


class CountingCli(SlurmCli):
    """The real adapter on FakeSlurm, counting `query` calls and every command it runs."""

    def __init__(self, fake: FakeSlurm) -> None:
        self.commands: list[str] = []
        self.queries = 0
        runner = SubprocessRunner()

        def run(argv: Sequence[str], *, timeout_s: float) -> CommandResult:
            self.commands.append(Path(argv[0]).name)
            return runner(argv, timeout_s=timeout_s)

        super().__init__(runner=run, bin_dir=fake.bin_dir)

    def query(self, job_ids: Iterable[JobRef]) -> dict[JobRef, SlurmJobInfo]:
        self.queries += 1
        return super().query(job_ids)


def _executor(
    tmp_path: Path, slurm: FakeSlurm, runner: FakeRunner, cli: SlurmCli, **settings: object
) -> SlurmExecutor:
    runner.cas_root = tmp_path / "cas"
    return SlurmExecutor(
        cli=cli,
        cas=FsCAS(runner.cas_root, DOMAIN),
        settings=SlurmSettings(debug_root=tmp_path / "debug", **settings),  # type: ignore[arg-type]
        runner_argv=runner.argv,
        env=runner.env(),
    )


# R5: one query per poll covers every in-flight job; array tasks are reported one by one.
def test_single_query_per_poll(tmp_path: Path, fake_slurm: FakeSlurm) -> None:
    fake_slurm.configure(cpus=2)
    runner = FakeRunner(tmp_path / "runner")
    ids = [f"t[{i}]" for i in range(5)]
    for a in ids:
        runner.set(a, hold=True)
    runner.set("one", exit=0)
    cli = CountingCli(fake_slurm)
    ex = _executor(tmp_path, fake_slurm, runner, cli)
    handles = [*ex.submit(batch(*ids)), *ex.submit(batch("one"))]
    wait_until(lambda: len(runner.started()) == 2, what="two tasks running")

    queries, commands = cli.queries, len(cli.commands)
    states = ex.poll(handles)
    assert cli.queries == queries + 1
    # squeue answers live and recently ended jobs (--states=all): no sacct needed.
    assert cli.commands[commands:] == ["squeue"]
    assert set(states) == set(handles)
    # Two tasks run (2 cpus); the other three tasks and the single job wait.
    assert sorted(s.state for s in states.values()) == ["pending"] * 4 + ["running"] * 2
    pending = [h for h in handles if states[h].state == "pending"]
    assert all(states[h].pending_reason is not None for h in pending)

    for a in ids:
        runner.release(a)
    polls = 0

    def all_done() -> bool:
        nonlocal polls
        before = cli.queries
        final = ex.poll(handles)
        polls += 1
        assert cli.queries - before <= 1
        return all(s.state == "done" for s in final.values())

    wait_until(all_done, timeout=30, what="all tasks done")
    assert sorted(runner.started()) == sorted([*ids, "one"])
    # Once every job is final the executor stops asking SLURM.
    queries = cli.queries
    ex.poll(handles)
    assert cli.queries == queries


# R6: one scancel; running tasks are told to clean up, pending ones report `cancelled`.
def test_cancel_all(tmp_path: Path, fake_slurm: FakeSlurm) -> None:
    fake_slurm.configure(cpus=2)
    runner = FakeRunner(tmp_path / "runner")
    ids = [f"t[{i}]" for i in range(4)]
    for a in [*ids, "solo"]:
        runner.set(a, hold=True)
    cli = CountingCli(fake_slurm)
    ex = _executor(tmp_path, fake_slurm, runner, cli)
    handles = [*ex.submit(batch(*ids)), *ex.submit(batch("solo"))]
    wait_until(lambda: len(runner.started()) == 2, what="two jobs running")

    before = cli.commands.count("scancel")
    ex.cancel(handles)
    assert cli.commands.count("scancel") == before + 1
    assert all(s.state == "cancelled" for s in ex.poll(handles).values())

    running = runner.started()
    wait_until(lambda: all(runner.cleaned(a) for a in running), what="runners cleaned up")
    # SLURM agrees: nothing of ours is left to run, and the pending tasks never started.
    refs = [ex.job_ref(h) for h in handles]
    wait_until(
        lambda: all(i.state == "CANCELLED" for i in fake_slurm.cli().query(refs).values()),
        what="SLURM shows every job cancelled",
    )
    assert sorted(runner.started()) == sorted(running)


# Done-when: a 2,000-action fake regression completes through FakeSlurm in < 60 s.
@pytest.mark.slow
def test_fake_regression_2000(tmp_path: Path, fake_slurm: FakeSlurm) -> None:
    fake_slurm.configure(cpus=64, poll_s=0.2)
    runner = FakeRunner(tmp_path / "runner")
    ids = [f"sim[test=t{i},seed={i}]" for i in range(2000)]
    cli = CountingCli(fake_slurm)
    ex = _executor(tmp_path, fake_slurm, runner, cli, array_throttle=64)
    start = time.monotonic()
    handles = ex.submit(batch(*ids))
    assert cli.commands.count("sbatch") == 2  # max_array_size = 1000
    # Poll like the driver does (seconds apart), not in a tight loop.
    final = wait_states(ex, handles, timeout=60, interval=2.0)
    elapsed = time.monotonic() - start
    assert all(s.state == "done" for s in final.values()), {s.state for s in final.values()}
    assert sorted(runner.started()) == sorted(ids)
    assert elapsed < 60, f"2,000 actions took {elapsed:.1f} s"
