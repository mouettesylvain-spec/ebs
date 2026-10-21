"""The pilot loop with a FakeClock and scripted runner processes (P3-06 R1, R2, R3)."""

from __future__ import annotations

import signal
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from ebs.config import CONFIG_ENV
from ebs.core.clock import FakeClock
from ebs.core.digest import hash_bytes
from ebs.core.errors import MetadataError
from ebs.exec.pilot.queue import Allocation
from ebs.exec.pilot.worker import (
    PilotConfig,
    PilotWorker,
    main,
    parse_pilot_arguments,
    pilot_arguments,
    pilot_identity,
)
from ebs.exec.slurm.executor import JOB_TIME_GRACE_S
from ebs.meta.api import PilotSlot, QueueItem, QueueLease
from ebs.meta.memory import InMemoryMetadataStore
from ebs.runner.errors import format_infra_line
from tests.helpers.runner import DOMAIN

PLAN = hash_bytes(b"plan")
RUNNER = ("/opt/ebs/bin/ebs-runner",)


@dataclass
class FakeProc:
    """A runner that exits with `code` after `run_s` FakeClock seconds; SIGTERM ends it with
    75 unless `ignore_term` (then only SIGKILL does). `on_poll` runs at every poll."""

    clock: FakeClock
    log: Path
    code: int = 0
    run_s: float = 0.0
    ignore_term: bool = False
    output: str = ""
    on_poll: Callable[[], None] | None = None
    started: float = 0.0
    signals: list[int] = field(default_factory=list)
    exit: int | None = None

    def poll(self) -> int | None:
        if self.on_poll is not None:
            self.on_poll()
        if self.exit is None and self.clock.monotonic() - self.started >= self.run_s:
            self.log.write_text(self.output)
            self.exit = self.code
        return self.exit

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)
        if sig == signal.SIGTERM and not self.ignore_term and self.exit is None:
            self.exit = 75

    def kill(self) -> None:
        self.signals.append(signal.SIGKILL)
        self.exit = -9


@dataclass
class Rig:
    tmp_path: Path
    clock: FakeClock = field(default_factory=FakeClock)
    pool: UUID = field(default_factory=uuid4)
    procs: list[FakeProc] = field(default_factory=list)
    argvs: list[list[str]] = field(default_factory=list)
    behave: dict[str, dict[str, Any]] = field(default_factory=dict)  # FakeProc fields

    def __post_init__(self) -> None:
        self.store = InMemoryMetadataStore(clock=self.clock)

    def put(self, *items: QueueItem) -> list[int]:
        return self.store.queue_put(DOMAIN, self.pool, list(items))

    def spawn(self, argv: Sequence[str], log: Path, env: Mapping[str, str]) -> FakeProc:
        action = argv[list(argv).index("--action") + 1]
        log.parent.mkdir(parents=True, exist_ok=True)
        proc = FakeProc(
            self.clock, log, started=self.clock.monotonic(), **self.behave.get(action, {})
        )
        self.procs.append(proc)
        self.argvs.append(list(argv))
        return proc

    def worker(self, *, walltime_s: int = 3600, **cfg: object) -> PilotWorker:
        config = PilotConfig(
            domain=DOMAIN,
            pool=self.pool,
            allocation=Allocation(
                cpus=2, mem_mb=None, licenses=(("simlic", 1),), features=(), walltime_s=walltime_s
            ),
            runner_argv=RUNNER,
            log_dir=self.tmp_path / "logs",
            **{"idle_s": 10.0, "lease_ttl_s": 30, "poll_s": 1.0, "kill_grace_s": 5.0, **cfg},  # type: ignore[arg-type]
        )
        return PilotWorker(
            self.store, config, pilot="7_0", node="n1", clock=self.clock, spawn=self.spawn, env={}
        )

    def states(self, ids: Sequence[int]) -> list[tuple[str, int | None, str | None]]:
        return [
            (e.state, e.exit_code, e.infra_reason)
            for e in self.store.queue_status(DOMAIN, self.pool, ids)
        ]


def item(action_id: str, **fields: object) -> QueueItem:
    return QueueItem.model_validate({"action_id": action_id, "plan": str(PLAN), **fields})


# R1: the pilot runs every action it can, oldest first, then exits once idle for idle_s.
def test_runs_queue_then_idles_out(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    ids = rig.put(item("a", build=3), item("b"), item("c", cpus=4), item("d"))
    worker = rig.worker()
    start = rig.clock.monotonic()
    assert worker.run() == 3  # c needs 4 cpus: never leased by this 2-cpu pilot (R3)
    assert rig.states(ids) == [
        ("done", 0, None),
        ("done", 0, None),
        ("queued", None, None),
        ("done", 0, None),
    ]
    assert rig.clock.monotonic() - start >= 10.0  # waited idle_s for more work
    assert rig.argvs[0] == [
        *RUNNER, "--plan", str(PLAN), "--action", "a", "--domain", DOMAIN, "--build", "3"
    ]  # fmt: skip
    assert "--build" not in rig.argvs[1]
    entry = rig.store.queue_status(DOMAIN, rig.pool, ids[:1])[0]
    assert (entry.pilot, entry.node) == ("7_0", "n1")


# R1, R3: no lease once the walltime left cannot hold an action, and the pilot then exits.
def test_walltime(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.behave["a"] = {"run_s": 50.0}
    ids = rig.put(item("a", time_s=60), item("b", time_s=60))
    worker = rig.worker(walltime_s=JOB_TIME_GRACE_S + 100)
    assert worker.run() == 1  # after a (50 s), 50 s are left: b (60 s) no longer fits
    assert rig.states(ids)[1][0] == "queued"


# R2: the lease is renewed while the runner works, so a long action is never lost.
def test_renews_long_actions(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.behave["a"] = {
        "run_s": 300.0,
        "code": 75,
        "output": "tool output\n" + format_infra_line("preempted", "SIGTERM") + "\n",
    }
    (a,) = rig.put(item("a"))
    assert rig.worker().run() == 1
    assert rig.states([a]) == [("done", 75, "preempted")]  # the runner's own infra reason


# R2: a renewal refused (the action was withdrawn) stops the runner; nothing is reported.
def test_withdrawn_action_stops_runner(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    (a,) = rig.put(item("a"))
    cancelled: list[bool] = []

    def cancel_once() -> None:
        if not cancelled and rig.clock.monotonic() >= 5:
            rig.store.queue_cancel(DOMAIN, rig.pool, [a])
            cancelled.append(True)

    rig.behave["a"] = {"run_s": 1000.0, "on_poll": cancel_once}
    assert rig.worker().run() == 1
    (proc,) = rig.procs
    assert proc.signals == [signal.SIGTERM]  # it cleans up and exits 75
    assert rig.states([a]) == [("cancelled", None, None)]


# R2: a runner that ignores SIGTERM is killed after kill_grace_s.
def test_withdrawn_runner_killed_after_grace(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    (a,) = rig.put(item("a"))
    rig.behave["a"] = {
        "run_s": 1000.0,
        "ignore_term": True,
        "on_poll": lambda: rig.store.queue_cancel(DOMAIN, rig.pool, [a]),
    }
    rig.worker().run()
    assert rig.procs[0].signals == [signal.SIGTERM, signal.SIGKILL]


# R2: a pilot cut off from the store gives its runner up once its lease must have expired.
def test_store_unreachable_while_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path)
    rig.put(item("a"))
    rig.behave["a"] = {"run_s": 1000.0}

    def down(*args: object) -> bool:
        raise MetadataError("metadata service unreachable")

    monkeypatch.setattr(rig.store, "queue_renew", down)
    monkeypatch.setattr(rig.store, "queue_lease", _lease_once(rig.store.queue_lease))
    worker = rig.worker()
    worker.run()
    assert rig.procs[0].signals == [signal.SIGTERM]
    assert rig.clock.monotonic() >= 30  # kept trying for the lease's TTL


def _lease_once(
    real: Callable[[str, UUID, PilotSlot, int], QueueLease | None],
) -> Callable[[str, UUID, PilotSlot, int], QueueLease | None]:
    calls = []

    def lease(domain: str, pool: UUID, slot: PilotSlot, ttl_s: int) -> QueueLease | None:
        calls.append(1)
        if len(calls) > 1:
            raise MetadataError("metadata service unreachable")
        return real(domain, pool, slot, ttl_s)

    return lease


# R1: an unreachable store counts as idle time: the pilot does not hold its licenses forever.
def test_unreachable_store_idles_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path)

    def down(*args: object) -> None:
        raise MetadataError("metadata service unreachable")

    monkeypatch.setattr(rig.store, "queue_lease", down)
    assert rig.worker().run() == 0
    assert rig.clock.monotonic() >= 10


# R1: SIGTERM (scancel, time limit) reaches the runner; its exit is reported, nothing more runs.
def test_stop_forwards_sigterm(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    ids = rig.put(item("a"), item("b"))
    worker = rig.worker()
    rig.behave["a"] = {"run_s": 1000.0, "on_poll": worker.stop}
    assert worker.run() == 1
    assert rig.procs[0].signals[0] == signal.SIGTERM
    assert rig.states(ids) == [("done", 75, None), ("queued", None, None)]  # "other" downstream


def test_spawn_failure_is_an_infra_failure(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    a, b = rig.put(item("a"), item("b"))

    def broken(argv: Sequence[str], log: Path, env: Mapping[str, str]) -> FakeProc:
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    rig.spawn = broken  # type: ignore[method-assign]
    assert rig.worker().run() == 1
    # The pilot stops: one broken pilot must not fail the whole queue.
    assert rig.states([a, b]) == [("done", 75, "runner_crash"), ("queued", None, None)]


def test_identity() -> None:
    array = {"SLURM_ARRAY_JOB_ID": "12", "SLURM_ARRAY_TASK_ID": "3", "SLURM_JOB_ID": "15"}
    assert pilot_identity({**array, "SLURMD_NODENAME": "n7"}) == ("12_3", "n7")
    assert pilot_identity({"SLURM_JOB_ID": "15", "SLURMD_NODENAME": "n7"}) == ("15", "n7")
    name, node = pilot_identity({})
    assert node is not None
    assert name.startswith(f"{node}:")


def test_arguments_parse_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = PilotConfig(
        domain=DOMAIN,
        pool=uuid4(),
        allocation=Allocation(
            cpus=2, mem_mb=512, licenses=(("simlic", 1),), features=("ssd",), walltime_s=600
        ),
        runner_argv=RUNNER,
        log_dir=tmp_path / "logs",
        idle_s=5.0,
        lease_ttl_s=30,
        poll_s=0.5,
    )
    argv = pilot_arguments(("ebs-pilot",), config)
    assert parse_pilot_arguments(argv[1:]) == config  # the pilot gets exactly its allocation
    assert argv[argv.index("--") + 1 :] == list(RUNNER)
    for pair in (["--mem-mb", "512"], ["--license", "simlic=1"], ["--feature", "ssd"]):
        assert pair[1] == argv[argv.index(pair[0]) + 1]
    monkeypatch.setenv(CONFIG_ENV, str(tmp_path / "missing.toml"))
    assert main(argv[1:]) == 1  # parsed; the config file named by $EBS_CONFIG is missing
    assert main(["--domain", DOMAIN]) == 2
    assert main([*argv[1:3], "--pool", "not-a-uuid"]) == 2
    bad = [*argv[1:]]
    bad[bad.index("--license") + 1] = "simlic"
    assert main(bad) == 2


# R1, R3: with no walltime left beyond the runner's grace, not even an untimed action is leased.
def test_no_walltime_left(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    (a,) = rig.put(item("a"))
    assert rig.worker(walltime_s=JOB_TIME_GRACE_S).run() == 0
    assert rig.states([a]) == [("queued", None, None)]


# R1: idle time counts from the end of the last action, not from the pilot's start.
def test_idle_counts_from_last_action(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.put(item("a"))
    late: list[int] = []

    def queue_b_later() -> None:
        if not late and rig.clock.monotonic() >= 50:
            late.extend(rig.put(item("b")))

    rig.behave["a"] = {"run_s": 50.0}
    store_lease = rig.store.queue_lease

    def lease(*args: Any) -> Any:
        if rig.clock.monotonic() >= 55:
            queue_b_later()
        return store_lease(*args)

    rig.store.queue_lease = lease  # type: ignore[method-assign,assignment]
    assert rig.worker(idle_s=10.0).run() == 2
    assert rig.states(late) == [("done", 0, None)]
