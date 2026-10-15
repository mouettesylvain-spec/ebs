"""Executor contract (interfaces.md § 8): every executor passes this suite.

Each case provides an executor whose runner is the scripted fake (`tests/helpers/fake_runner.py`).
The SLURM executor runs against FakeSlurm (P1-02), with a cluster of CAPACITY cpus.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from ebs.cas.fs import FsCAS
from ebs.exec.api import Executor
from ebs.exec.local import LocalExecutor
from ebs.exec.slurm.executor import SlurmExecutor, SlurmSettings
from ebs.runner.errors import format_infra_line
from tests.fakes.slurm import FakeSlurm
from tests.helpers.executor import FakeRunner, batch, wait_states
from tests.helpers.runner import DOMAIN
from tests.helpers.wait import wait_until

CAPACITY = 2  # each case runs at most this many single-cpu actions at once


@dataclass
class Case:
    fake: FakeRunner
    start: Callable[[], Executor]


def _local(tmp_path: Path, fake: FakeRunner) -> Iterator[Callable[[], Executor]]:
    created: list[LocalExecutor] = []

    def start() -> Executor:
        ex = LocalExecutor(
            log_dir=tmp_path / "logs", runner_argv=fake.argv, max_parallel=CAPACITY, env=fake.env()
        )
        created.append(ex)
        return ex

    yield start
    for ex in created:
        ex.close(grace_s=2)


def _slurm(tmp_path: Path, fake: FakeRunner) -> Iterator[Callable[[], Executor]]:
    slurm = FakeSlurm.install(tmp_path / "fakeslurm", cpus=CAPACITY)
    fake.cas_root = tmp_path / "cas"
    cas = FsCAS(fake.cas_root, DOMAIN)

    def start() -> Executor:
        return SlurmExecutor(
            cli=slurm.cli(),
            cas=cas,
            settings=SlurmSettings(debug_root=tmp_path / "debug"),
            runner_argv=fake.argv,
            env=fake.env(),
        )

    yield start
    slurm.shutdown()


FACTORIES: dict[str, Callable[[Path, FakeRunner], Iterator[Callable[[], Executor]]]] = {
    "local": _local,
    "slurm": _slurm,
}


@pytest.fixture(
    params=[
        "local",
        # FakeSlurm runs jobs through detached supervisor processes.
        pytest.param("slurm", marks=pytest.mark.integration),
    ]
)
def case(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Case]:
    fake = FakeRunner(tmp_path / "fake")
    gen = FACTORIES[request.param](tmp_path, fake)
    yield Case(fake, next(gen))
    next(gen, None)


# R1, R2: one handle per action in order; everything is reported, queued work as pending.
def test_submit_and_poll(case: Case) -> None:
    ids = [f"t[{i}]" for i in range(CAPACITY + 2)]
    for a in ids:
        case.fake.set(a, hold=True)
    ex = case.start()
    handles = ex.submit(batch(*ids))
    assert [h.action_id for h in handles] == ids
    assert len(set(handles)) == len(handles)
    assert all(h.executor == ex.name for h in handles)
    wait_until(lambda: len(case.fake.started()) == CAPACITY, what="capacity reached")
    states = ex.poll(handles)
    assert set(states) == set(handles)
    pending = [h for h in handles if states[h].state == "pending"]
    assert len(pending) == 2
    assert all(states[h].pending_reason is not None for h in pending)
    for a in ids:
        case.fake.release(a)
    final = wait_states(ex, handles)
    assert all(s.state == "done" for s in final.values())
    assert sorted(case.fake.started()) == sorted(ids)


# R3: runner exit codes mean the same on every executor.
@pytest.mark.parametrize(
    ("behaviour", "reason"),
    [
        ({"exit": 0}, None),
        ({"exit": 75, "stderr": format_infra_line("preempted", "SIGTERM")}, "preempted"),
        ({"exit": 75}, "other"),
        ({"exit": 76}, "input_verification"),
        ({"exit": 70}, "runner_crash"),
    ],
)
def test_exit_codes(case: Case, behaviour: dict[str, object], reason: str | None) -> None:
    case.fake.set("t[0]", **behaviour)
    ex = case.start()
    (h,) = ex.submit(batch("t[0]"))
    status = wait_states(ex, [h])[h]
    assert status.state == ("done" if reason is None else "infra_failed")
    assert status.infra_reason == reason


# R4: cancel stops running and queued work; the runner is told to clean up.
def test_cancel(case: Case) -> None:
    ids = [f"t[{i}]" for i in range(CAPACITY + 1)]
    for a in ids:
        case.fake.set(a, hold=True)
    ex = case.start()
    handles = ex.submit(batch(*ids))
    wait_until(lambda: len(case.fake.started()) == CAPACITY, what="capacity reached")
    ex.cancel(handles)
    assert all(s.state == "cancelled" for s in ex.poll(handles).values())
    wait_until(lambda: all(case.fake.cleaned(a) for a in ids[:CAPACITY]), what="cleanup")
    assert len(case.fake.started()) == CAPACITY  # the queued one never ran
