"""Executor contract (interfaces.md § 8): every executor passes this suite.

Each case provides an executor whose runner is the scripted fake (`tests/helpers/fake_runner.py`).
The SLURM executor runs against FakeSlurm (P1-02), with a cluster of CAPACITY cpus; so does the
pilot executor (P3-06), whose pilot jobs pull the actions from a queue in PostgreSQL.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from ebs.cas.fs import FsCAS
from ebs.config import CONFIG_ENV
from ebs.exec.api import Executor
from ebs.exec.local import LocalExecutor
from ebs.exec.pilot.executor import PilotExecutor, PilotSettings
from ebs.exec.slurm.executor import SlurmExecutor, SlurmSettings
from ebs.meta.pg import PgMetadataStore
from ebs.runner.errors import format_infra_line
from tests.fakes.slurm import FakeSlurm
from tests.helpers.executor import PILOT_ARGV, FakeRunner, batch, wait_states
from tests.helpers.pg import url_string
from tests.helpers.runner import DOMAIN
from tests.helpers.wait import wait_until

CAPACITY = 2  # each case runs at most this many single-cpu actions at once


@dataclass
class Case:
    fake: FakeRunner
    start: Callable[[], Executor]


def _local(
    tmp_path: Path, fake: FakeRunner, request: pytest.FixtureRequest
) -> Iterator[Callable[[], Executor]]:
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


def _slurm(
    tmp_path: Path, fake: FakeRunner, request: pytest.FixtureRequest
) -> Iterator[Callable[[], Executor]]:
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


def _pilot(
    tmp_path: Path, fake: FakeRunner, request: pytest.FixtureRequest
) -> Iterator[Callable[[], Executor]]:
    """P3-06 R5: pilots under FakeSlurm pull the actions from a PostgreSQL queue."""
    slurm = FakeSlurm.install(tmp_path / "fakeslurm", cpus=CAPACITY)
    url = url_string(request.getfixturevalue("pg_url"))
    config = tmp_path / "ebs.toml"
    config.write_text(f"[metadata]\nurl = {json.dumps(url)}\n")
    store = PgMetadataStore.from_config({"url": url})
    created: list[PilotExecutor] = []

    def start() -> Executor:
        ex = PilotExecutor(
            cli=slurm.cli(),
            store=store,
            domain=DOMAIN,
            settings=SlurmSettings(debug_root=tmp_path / "debug", poll_min_s=0.2),
            pilot=PilotSettings(max_pilots=CAPACITY + 2, idle_s=2.0, lease_ttl_s=3, poll_s=0.05),
            pilot_argv=PILOT_ARGV,
            runner_argv=fake.argv,
            env={**fake.env(), CONFIG_ENV: str(config)},
        )
        created.append(ex)
        return ex

    yield start
    for ex in created:
        ex.close()
    slurm.shutdown()
    store.close()


FACTORIES: dict[
    str, Callable[[Path, FakeRunner, pytest.FixtureRequest], Iterator[Callable[[], Executor]]]
] = {
    "local": _local,
    "slurm": _slurm,
    "pilot": _pilot,
}


@pytest.fixture(
    params=[
        "local",
        # FakeSlurm runs jobs through detached supervisor processes.
        pytest.param("slurm", marks=pytest.mark.integration),
        # P3-06 R5: pilot processes share the queue through PostgreSQL.
        pytest.param("pilot", marks=pytest.mark.integration),
    ]
)
def case(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Case]:
    fake = FakeRunner(tmp_path / "fake")
    gen = FACTORIES[request.param](tmp_path, fake, request)
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
