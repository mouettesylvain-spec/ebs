"""PilotExecutor against a scripted SLURM and the in-memory queue (P3-06 R1, R2, R3, R5).

The pilots themselves are played by the test, through the store's queue methods; real pilots
under FakeSlurm are in tests/integration/slurm/test_pilot.py and the executor contract suite.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from ebs.core.clock import FakeClock
from ebs.core.errors import ConfigError, ExecutorError
from ebs.exec.api import JobHandle
from ebs.exec.pilot.executor import PilotExecutor, PilotSettings
from ebs.exec.slurm.executor import SlurmSettings
from ebs.meta.api import PilotSlot
from ebs.meta.memory import InMemoryMetadataStore
from tests.helpers.executor import batch
from tests.helpers.runner import DOMAIN, new_build
from tests.helpers.slurm import ScriptedSlurm


@dataclass
class Rig:
    tmp_path: Path
    clock: FakeClock = field(default_factory=FakeClock)
    slurm: ScriptedSlurm = field(default_factory=ScriptedSlurm)

    def __post_init__(self) -> None:
        self.store = InMemoryMetadataStore(clock=self.clock)

    def executor(self, **pilot: object) -> PilotExecutor:
        return PilotExecutor(
            cli=self.slurm.cli(),
            store=self.store,
            domain=DOMAIN,
            settings=SlurmSettings(debug_root=self.tmp_path / "debug", poll_min_s=5.0),
            pilot=PilotSettings(**{"max_pilots": 2, **pilot}),  # type: ignore[arg-type]
            pilot_argv=("/opt/ebs/bin/ebs-pilot",),
            runner_argv=("/opt/ebs/bin/ebs-runner",),
            env={"PATH": "/usr/bin"},
            clock=self.clock,
        )

    def lease(self, ex: PilotExecutor, pilot: str = "100_0") -> int:
        slot = PilotSlot(pilot=pilot, node="n9", cpus=1, time_left_s=3600)
        lease = self.store.queue_lease(DOMAIN, ex.pool, slot, 60)
        assert lease is not None
        return lease.item

    def tick(self, ex: PilotExecutor, handles: list[JobHandle]) -> dict[JobHandle, str]:
        self.clock.advance(5.0)  # past poll_min_s: the pilots are refreshed too
        return {h: s.state for h, s in ex.poll(handles).items()}


# R1: one sbatch, an array of min(actions, max_pilots) pilots; actions report pending.
def test_submit_starts_one_array(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    ex = rig.executor()
    handles = ex.submit(batch("t[0]", "t[1]", "t[2]"))
    assert [h.executor for h in handles] == ["pilot"] * 3
    assert rig.slurm.count("sbatch") == 1
    assert len(rig.slurm.jobs) == 2
    assert rig.slurm.jobs[0].name == "ebs:local:pilot"
    states = ex.poll(handles)
    assert all(s.state == "pending" and s.pending_reason for s in states.values())
    ex.submit(batch("u[0]"))  # same shape: the two live pilots serve it too
    assert rig.slurm.count("sbatch") == 1


# R1: queue states map to job states; the node is the pilot's.
def test_states(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    ex = rig.executor()
    handles = ex.submit(batch("t[a]", "t[b]", "t[c]", "t[d]"))
    a = rig.lease(ex)
    b = rig.lease(ex, "100_1")
    c = rig.lease(ex, "100_0")
    assert rig.store.queue_finish(DOMAIN, ex.pool, b, "100_1", 75, "preempted")
    assert rig.store.queue_finish(DOMAIN, ex.pool, c, "100_0", 70)
    statuses = ex.poll(handles)
    assert (statuses[handles[0]].state, statuses[handles[0]].node) == ("running", "n9")
    assert (statuses[handles[1]].state, statuses[handles[1]].infra_reason) == (
        "infra_failed", "preempted"
    )  # fmt: skip
    assert statuses[handles[2]].infra_reason == "runner_crash"
    assert statuses[handles[3]].state == "pending"
    assert rig.store.queue_finish(DOMAIN, ex.pool, a, "100_0", 0)
    final = ex.poll(handles[:1])[handles[0]]
    assert (final.state, final.exit_code, final.node) == ("done", 0, "n9")


# R2: an unrenewed lease is an infrastructure failure on the pilot's node.
def test_lost_lease_is_node_fail(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    ex = rig.executor()
    (h,) = ex.submit(batch("a"))
    rig.lease(ex)
    rig.clock.advance(61)
    status = ex.poll([h])[h]
    assert (status.state, status.infra_reason, status.node) == ("infra_failed", "node_fail", "n9")


# R3: the pending reason is the pilots': licenses when SLURM holds them for licenses.
def test_pending_reason_from_pilots(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    ex = rig.executor()
    (h,) = ex.submit(batch("a"))
    rig.slurm.set_state("100", "PENDING", reason="Licenses")
    rig.clock.advance(5.0)
    assert ex.poll([h])[h].pending_reason == "licenses"
    rig.slurm.set_state("100", "RUNNING", node="n9")
    rig.clock.advance(5.0)
    assert ex.poll([h])[h].pending_reason == "resources"  # waiting for a busy pilot


# R1: pilots that ended (idle, walltime) are replaced while actions wait.
def test_top_up_after_pilots_end(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    ex = rig.executor()
    handles = ex.submit(batch("t[a]", "t[b]", "t[c]"))
    rig.slurm.set_state("100", "COMPLETED")
    assert set(rig.tick(ex, handles).values()) == {"pending"}
    assert rig.slurm.count("sbatch") == 2
    assert len(ex.pilot_refs()) == 4


# Pilots that keep failing do not loop: the waiting actions fail, a retry gets new pilots.
def test_failing_pilots_give_up(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    ex = rig.executor(max_pilots=1, max_pilot_failures=2)
    (h,) = ex.submit(batch("a"))
    rig.slurm.set_state("100", "FAILED", exit_code=1)
    assert rig.tick(ex, [h]) == {h: "pending"}  # one failure: a new pilot
    rig.slurm.set_state("101", "FAILED", exit_code=1)
    assert rig.tick(ex, [h]) == {h: "infra_failed"}
    assert rig.slurm.count("sbatch") == 2
    (retry,) = ex.submit(batch("a"))
    assert rig.slurm.count("sbatch") == 3
    assert ex.poll([retry])[retry].state == "pending"


# R5: cancel withdraws queued and running actions; idle pilots are scancelled.
def test_cancel(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    ex = rig.executor()
    handles = ex.submit(batch("t[a]", "t[b]", "t[c]"))
    rig.lease(ex)
    ex.cancel(handles[:2])
    assert {h: s.state for h, s in ex.poll(handles).items()} == {
        handles[0]: "cancelled",
        handles[1]: "cancelled",
        handles[2]: "pending",
    }
    assert rig.slurm.count("scancel") == 0  # c still waits for the pilots
    ex.cancel(handles)
    assert rig.slurm.count("scancel") == 1
    assert {j.state for j in rig.slurm.jobs} == {"CANCELLED"}
    ex.cancel(handles)  # already final: nothing to do
    assert rig.slurm.count("scancel") == 1
    entries = rig.store.queue_status(DOMAIN, ex.pool, [int(h.job_id) for h in handles])
    assert {e.state for e in entries} == {"cancelled"}


def test_close_cancels_pilots(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    ex = rig.executor()
    ex.submit(batch("a"))
    ex.close()
    assert rig.slurm.jobs[0].state == "CANCELLED"


# R1: pilots of a build are named after it; a failed sbatch withdraws the actions.
def test_build_and_sbatch_failure(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    build = new_build(rig.store, "a", "b", cache_mode="write")
    ex = rig.executor()
    ex.submit(batch("a", build=build))
    uuid = rig.store.get_build(build).uuid
    assert rig.slurm.jobs[0].name == f"ebs:{uuid.hex[:8]}:pilot"
    assert rig.slurm.jobs[0].comment.startswith(str(uuid))
    rig.slurm.sbatch_error = "sbatch: error: Batch job submission failed: QOS limit"
    other = rig.executor()
    with pytest.raises(ExecutorError, match="withdrawn"):
        other.submit(batch("b", build=build))
    (b,) = rig.store.queue_status(DOMAIN, other.pool, [2])
    assert b.state == "cancelled"


def test_errors(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    ex = rig.executor()
    elsewhere = PilotExecutor(
        cli=rig.slurm.cli(), store=rig.store, domain="other", settings=SlurmSettings()
    )
    with pytest.raises(ExecutorError, match="domain"):
        elsewhere.submit(batch("a"))
    with pytest.raises(ExecutorError, match="unknown job handle"):
        ex.poll([JobHandle("pilot", "1", "a")])
    with pytest.raises(ExecutorError, match="pilot_argv"):
        PilotExecutor(
            cli=rig.slurm.cli(),
            store=rig.store,
            domain=DOMAIN,
            settings=SlurmSettings(),
            pilot_argv=(),
        )


def test_settings() -> None:
    assert PilotSettings.from_config({}) == PilotSettings()
    s = PilotSettings.from_config({"pilot": {"max_pilots": 8, "poll_s": 0.5, "walltime_s": 600}})
    assert (s.max_pilots, s.poll_s, s.walltime_s) == (8, 0.5, 600)
    for bad in ({"max_pilots": 0}, {"idle_s": -1}, {"walltime_s": 1.5}, {"poll_s": True}):
        with pytest.raises(ConfigError, match=r"\[pilot\]"):
            PilotSettings.from_config({"pilot": bad})
    with pytest.raises(ConfigError):
        PilotSettings(max_pilots=0)


# P1-05 with pilots: a retry excluding a node is never leased by a pilot on that node.
def test_excluded_nodes_follow_the_action(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    ex = rig.executor()
    retry = dataclasses.replace(batch("t[a]"), exclude_nodes=("n9",))
    ex.submit(retry)
    on_bad = PilotSlot(pilot="100_0", node="n9", cpus=1, time_left_s=3600)
    assert rig.store.queue_lease(DOMAIN, ex.pool, on_bad, 60) is None
    on_good = PilotSlot(pilot="100_1", node="n8", cpus=1, time_left_s=3600)
    assert rig.store.queue_lease(DOMAIN, ex.pool, on_good, 60) is not None
    assert "--exclude=n9" in [a for c in rig.slurm.calls for a in c]


# A pilot that ends normally resets the failure count: failures must come in a row.
def test_failures_must_be_consecutive(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    ex = rig.executor(max_pilots=1, max_pilot_failures=2)
    (h,) = ex.submit(batch("t[a]"))
    rig.slurm.set_state("100", "FAILED", exit_code=1)
    assert rig.tick(ex, [h]) == {h: "pending"}
    rig.slurm.set_state("101", "COMPLETED")
    assert rig.tick(ex, [h]) == {h: "pending"}
    rig.slurm.set_state("102", "FAILED", exit_code=1)
    assert rig.tick(ex, [h]) == {h: "pending"}  # 1 in a row, not 2
    assert rig.slurm.count("sbatch") == 4


# R5: cancelling one action keeps the pilots while another of the shape still runs.
def test_cancel_keeps_busy_pilots(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    ex = rig.executor()
    a, b = ex.submit(batch("t[a]", "t[b]"))
    rig.lease(ex)
    ex.cancel([b])
    assert rig.slurm.count("scancel") == 0
    assert ex.poll([a])[a].state == "running"


# R5: an action that finished before the cancel keeps its result.
def test_cancel_after_finish_keeps_done(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    ex = rig.executor()
    (h,) = ex.submit(batch("t[a]"))
    item = rig.lease(ex)
    assert rig.store.queue_finish(DOMAIN, ex.pool, item, "100_0", 0)
    ex.cancel([h])  # no poll in between
    assert ex.poll([h])[h].state == "done"


# A pilot SLURM forgot for 10 polls in a row is gone and is replaced; 9 are not enough.
def test_forgotten_pilot_replaced(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    ex = rig.executor(max_pilots=1)
    (h,) = ex.submit(batch("t[a]"))
    rig.slurm.jobs.clear()
    for _ in range(9):
        rig.tick(ex, [h])
    assert rig.slurm.count("sbatch") == 1
    rig.tick(ex, [h])
    assert rig.slurm.count("sbatch") == 2


def test_default_threshold_below_batch_cap() -> None:
    from ebs.driver.scheduler import DriverConfig

    assert PilotSettings().threshold == 200 < DriverConfig().max_batch
