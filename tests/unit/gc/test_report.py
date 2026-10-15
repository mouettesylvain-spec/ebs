"""GC report and `--dry-run` (P1-09 R6)."""

from __future__ import annotations

import os
from datetime import timedelta

from ebs.gc.report import render
from ebs.gc.run import collect
from ebs.meta.api import DomainPolicy
from tests.helpers.gc import DOMAIN, World, present


def state(world: World) -> tuple[object, ...]:
    s = world.store
    return (
        present(world.cas),
        s.gc_blobs(DOMAIN),
        s.gc_cache_entries(DOMAIN),
        s.gc_tombstones(DOMAIN),
        s.gc_roots(DOMAIN, world.clock.now()),
    )


# R6: a dry run reports exactly what the real run then does, and changes nothing.
def test_dry_run_equivalence(world: World) -> None:
    lint_out, lint_log = world.blob(b"lint report " * 10), world.blob(b"lint log")
    sim_out = world.blob(b"sim result " * 20)
    model = world.tree({"top.v": b"module top;", "core/alu.v": b"module alu;"}, register="all")
    b1 = world.build(project="demo")
    lint_key = world.record(b1, "lint", {"report": lint_out}, log=lint_log)
    world.record(b1, "sim[t=1]", {"result": sim_out})
    b2 = world.build(project="other")
    world.record(b2, "gen", {"model": model})
    world.entry("lint", {"report": lint_out}, log=lint_log)
    world.entry("gen", {"model": model})
    assert lint_key is not None

    world.advance(days=31)
    collect(world.store, world.cas, clock=world.clock)  # rows deleted, objects tombstoned
    b3 = world.build(project="demo")
    fresh = world.blob(b"fresh lint report")
    world.record(b3, "lint", {"report": fresh})
    world.advance(hours=25)  # the tombstones are due; `fresh` is past the grace period
    world.store.set_policy(DOMAIN, DomainPolicy(quota_bytes=10, high_water=0.8, low_water=0.5))
    tmp = world.tmp / "cas" / DOMAIN / "tmp"
    (tmp / "stale.tmp").write_bytes(b"x")
    stamp = (world.clock.now() - timedelta(hours=48)).timestamp()
    os.utime(tmp / "stale.tmp", (stamp, stamp))

    before = state(world)
    dry = collect(world.store, world.cas, clock=world.clock, dry_run=True)
    assert state(world) == before
    assert (tmp / "stale.tmp").exists()
    real = collect(world.store, world.cas, clock=world.clock)

    assert dry.dry_run is True
    assert real.dry_run is False
    assert dry.model_copy(update={"dry_run": False}) == real
    assert real.deleted > 0
    assert real.quota is not None
    assert real.quota.evicted == [str(fresh)]
    assert real.tmp_removed == 1

    # Reclaimable bytes per step, project and build (an object counts for each that used it).
    tree_bytes = sum(len(b) for b in (b"module top;", b"module alu;"))
    manifests = real.reclaimable.bytes - (120 + 8 + 220 + len(b"fresh lint report") + tree_bytes)
    assert manifests > 0  # the model's two tree manifests
    assert real.reclaimable.by_step == {
        "lint": 120 + 8 + len(b"fresh lint report"),
        "sim": 220,
        "gen": tree_bytes + manifests,
    }
    assert real.reclaimable.by_project == {
        "demo": 120 + 8 + 220 + len(b"fresh lint report"),
        "other": tree_bytes + manifests,
    }
    assert real.reclaimable.by_build == {
        str(b1): 120 + 8 + 220,
        str(b2): tree_bytes + manifests,
        str(b3): len(b"fresh lint report"),
    }
    runs = world.store.gc_runs
    assert [r.dry_run for r in runs] == [False, True, False]
    assert runs[-1].report == real.model_dump(mode="json")
    assert runs[-1].swept_bytes == real.deleted_bytes

    text = "\n".join(render(real))
    for word in ("reclaimable", "lint", "demo", "evicted", "deleted"):
        assert word in text
