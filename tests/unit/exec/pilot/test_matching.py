"""Pilots take only actions that fit their allocation; licenses stay on the pilot job (P3-06 R3)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ebs.core.clock import FakeClock
from ebs.core.digest import hash_bytes
from ebs.exec.pilot.executor import PilotExecutor, PilotSettings
from ebs.exec.pilot.queue import Allocation, allocation_for, queue_item
from ebs.exec.slurm.executor import JOB_TIME_GRACE_S, SlurmSettings
from ebs.meta.api import PilotSlot, QueueItem, slot_fits
from ebs.meta.memory import InMemoryMetadataStore
from ebs.plan.types import ActionSpec
from tests.helpers.executor import PLAN, spec
from tests.helpers.plan import resources
from tests.helpers.runner import DOMAIN
from tests.helpers.slurm import ScriptedSlurm

PLAN_DIGEST = hash_bytes(b"plan")
MIB = 1 << 20


def action(
    action_id: str,
    *,
    cpus: int | None = None,
    mem: int | None = None,
    time: int | None = None,
    licenses: dict[str, int] | None = None,
    constraint: str | None = None,
) -> ActionSpec:
    base = spec(action_id)
    return dataclasses.replace(
        base,
        resources=resources(cpus, mem, time, constraint=constraint),
        licenses=licenses or {},
    )


def slot(**fields: object) -> PilotSlot:
    return PilotSlot.model_validate({"pilot": "7_0", "cpus": 1, "time_left_s": 600, **fields})


def item(**fields: object) -> QueueItem:
    return QueueItem.model_validate({"action_id": "a", "plan": str(PLAN_DIGEST), **fields})


# R3: each resource, license and feature is checked against the slot.
@pytest.mark.parametrize(
    ("slot_fields", "item_fields", "fits"),
    [
        ({}, {}, True),
        ({"cpus": 2}, {"cpus": 2}, True),
        ({"cpus": 2}, {"cpus": 3}, False),
        ({"mem_mb": 1024}, {"mem_mb": 1024}, True),
        ({"mem_mb": 1024}, {"mem_mb": 1025}, False),
        ({}, {"mem_mb": 1 << 20}, True),  # no memory limit on the pilot job
        ({"mem_mb": 1024}, {}, True),
        ({"time_left_s": 60}, {"time_s": 60}, True),
        ({"time_left_s": 59}, {"time_s": 60}, False),
        ({"time_left_s": 0}, {}, True),  # no time limit asked
        ({"licenses": {"simlic": 2}}, {"licenses": {"simlic": 2}}, True),
        ({"licenses": {"simlic": 1}}, {"licenses": {"simlic": 2}}, False),
        ({"licenses": {"other": 4}}, {"licenses": {"simlic": 1}}, False),
        ({}, {"licenses": {"simlic": 1}}, False),
        ({"licenses": {"simlic": 1}}, {}, True),  # held while idle: the documented trade-off
        ({"features": ("avx512", "ssd")}, {"features": ("avx512",)}, True),
        ({"features": ("ssd",)}, {"features": ("avx512",)}, False),
        ({}, {"features": ("ssd",)}, False),
        ({"node": "n1"}, {"exclude": ("n1", "n2")}, False),  # P1-05: a node that failed it
        ({"node": "n3"}, {"exclude": ("n1", "n2")}, True),
    ],
)
def test_slot_fits(
    slot_fields: dict[str, object], item_fields: dict[str, object], fits: bool
) -> None:
    assert slot_fits(slot(**slot_fields), item(**item_fields)) is fits


# R3: an action converts to the queue item the pilots match on.
def test_queue_item_from_action() -> None:
    a = action(
        "sim[1]",
        cpus=2,
        mem=3 * MIB + 1,
        time=90,
        licenses={"simlic": 1},
        constraint="ssd&avx512",
    )
    got = queue_item(a, plan=PLAN_DIGEST, build=None)
    assert got.action_id == "sim[1]"
    assert (got.cpus, got.mem_mb, got.time_s) == (2, 4, 90)  # memory rounds up to a MiB
    assert dict(got.licenses) == {"simlic": 1}
    assert got.features == ("avx512", "ssd")
    bare = queue_item(action("lint"), plan=PLAN_DIGEST, build=None)
    assert (bare.cpus, bare.mem_mb, bare.time_s, bare.features) == (1, None, None, ())


# R1, R3: a pilot is sized by the largest request of its step, so every action of it fits.
def test_allocation_sized_by_step() -> None:
    actions = [
        action("sim[0]", cpus=1, mem=2 * MIB, time=30, licenses={"simlic": 1}),
        action("sim[1]", cpus=2, mem=1 * MIB, time=600, licenses={"simlic": 1, "cov": 1}),
    ]
    alloc = allocation_for(actions, walltime_s=3600, os_constraint="el9")
    assert alloc == Allocation(
        cpus=2,
        mem_mb=2,
        licenses=(("cov", 1), ("simlic", 1)),
        features=("el9",),
        walltime_s=3600,
    )
    tiny = allocation_for(actions, walltime_s=60, os_constraint=None)
    assert tiny.walltime_s == 600 + 2 * JOB_TIME_GRACE_S  # the longest action still fits


@given(
    st.lists(
        st.tuples(
            st.none() | st.integers(1, 16),
            st.none() | st.integers(1, 64 * 1024),
            st.none() | st.integers(1, 7200),
            st.dictionaries(st.sampled_from(["simlic", "cov", "lint"]), st.integers(1, 3)),
            st.sampled_from([None, "ssd", "avx512&ssd"]),
        ),
        min_size=1,
        max_size=6,
    ),
    st.integers(60, 86_400),
)
def test_every_action_fits_its_allocation(
    rows: list[tuple[int | None, int | None, int | None, dict[str, int], str | None]],
    walltime_s: int,
) -> None:
    actions = [
        action(
            f"s[{i}]",
            cpus=c,
            mem=None if m is None else m * MIB,
            time=t,
            licenses=lic,
            constraint=con,
        )
        for i, (c, m, t, lic, con) in enumerate(rows)
    ]
    alloc = allocation_for(actions, walltime_s=walltime_s, os_constraint=None)
    fresh = alloc.slot("1_0", node=None, elapsed_s=0)
    for a in actions:
        assert slot_fits(fresh, queue_item(a, plan=PLAN_DIGEST, build=None))


# R1, R3: the slot shrinks with the walltime used; the runner's grace is kept back.
def test_slot_time_left() -> None:
    alloc = Allocation(cpus=1, mem_mb=None, licenses=(), features=(), walltime_s=1000)
    assert alloc.slot("1_0", node="n1", elapsed_s=0).time_left_s == 1000 - JOB_TIME_GRACE_S
    assert alloc.slot("1_0", node="n1", elapsed_s=900).time_left_s == 0
    assert alloc.slot("1_0", node="n1", elapsed_s=100.5).node == "n1"


# R3: the memory store leases by `slot_fits` (the PostgreSQL rule is in the contract suite).
def test_store_leases_only_matching() -> None:
    store = InMemoryMetadataStore(clock=FakeClock())
    from uuid import uuid4

    pool = uuid4()
    items = [item(action_id="lic", licenses={"simlic": 1}), item(action_id="plain")]
    store.queue_put(DOMAIN, pool, items)
    lease = store.queue_lease(DOMAIN, pool, slot(), 60)
    assert lease is not None
    assert lease.action_id == "plain"
    assert store.queue_lease(DOMAIN, pool, slot(), 60) is None


# R3: licenses are requested per pilot job with -L, and pilots get the step's walltime class.
def test_pilot_job_requests_licenses(tmp_path: Path) -> None:
    slurm = ScriptedSlurm()
    clock = FakeClock()
    store = InMemoryMetadataStore(clock=clock)
    ex = PilotExecutor(
        cli=slurm.cli(),
        store=store,
        domain=DOMAIN,
        settings=SlurmSettings(debug_root=tmp_path / "debug", license_server="lic.example"),
        pilot=PilotSettings(max_pilots=2, walltime_s=1800),
        pilot_argv=("/opt/ebs/bin/ebs-pilot",),
        runner_argv=("/opt/ebs/bin/ebs-runner",),
        env={},
        clock=clock,
    )
    from ebs.exec.api import SubmitBatch

    actions = tuple(
        action(f"sim[{i}]", cpus=2, mem=512 * MIB, time=20, licenses={"simlic": 1})
        for i in range(5)
    )
    ex.submit(SubmitBatch(plan=PLAN, domain=DOMAIN, build=None, actions=actions))
    (sbatch,) = [c for c in slurm.calls if c[0].endswith("sbatch")]
    assert "--licenses=simlic@lic.example:1" in sbatch
    assert "--cpus-per-task=2" in sbatch
    assert "--mem=512M" in sbatch
    assert "--array=0-1%2" in sbatch  # N = min(actions, max_pilots) pilots, one array
    assert any(a.startswith("--time=") and a != "--time=00:20:00" for a in sbatch)
    script = slurm.scripts[0]
    for arg in ("--cpus 2", "--mem-mb 512", "--license simlic=1", "--walltime-s 1800"):
        assert arg in script
