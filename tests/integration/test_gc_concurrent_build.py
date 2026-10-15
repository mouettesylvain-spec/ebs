"""GC runs safely while builds run (P1-09 R7, invariant I15), on PostgreSQL.

A build that starts in the middle of a sweep (between the snapshot and the deletions) must find
valid cache hits: every output a hit names is still in the CAS, then and after later GC runs.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy.engine import URL

from ebs.cas.fs import FsCAS
from ebs.core.digest import Digest
from ebs.driver.build import BuildOutcome, run_build
from ebs.driver.scheduler import DriverConfig
from ebs.gc.run import collect
from ebs.meta.api import BlobRef, CacheMode
from ebs.meta.pg import PgMetadataStore
from ebs.plan.types import Plan
from tests.helpers.driver import DOMAIN, Node, ScriptedExecutor, SleepRecorder, make_plan, refiner
from tests.helpers.pg import _pg_truncate_engine, pg_migrated, pg_server, pg_url, url_string

__all__ = ["_pg_truncate_engine", "pg_migrated", "pg_server", "pg_url"]

DAY = 86_400.0


class HookedStore(PgMetadataStore):
    """The PostgreSQL store, running `before[method]` once just before that GC write."""

    before: dict[str, Callable[[], None]]

    def _hook(self, name: str) -> None:
        hook = self.before.pop(name, None)
        if hook is not None:
            hook()

    def gc_delete_cache_entries(
        self, domain: str, keys: Iterable[Digest], *, unused_before: datetime
    ) -> list[Digest]:
        self._hook("gc_delete_cache_entries")
        return super().gc_delete_cache_entries(domain, keys, unused_before=unused_before)

    def gc_claim(self, domain: str, digest: Digest, now: datetime) -> bool:
        self._hook("gc_claim")
        return super().gc_claim(domain, digest, now)


@dataclass
class Env:
    cas: FsCAS
    store: HookedStore
    clock: SleepRecorder
    workdir: Path

    def run(self, mode: CacheMode) -> BuildOutcome:
        """One build of NODES with a scripted runner (every action passes)."""
        return run_build(
            make_plan(self.cas, NODES),
            refiner=refiner(self.cas),
            cas=self.cas,
            store=self.store,
            executor=ScriptedExecutor(self.store, self.cas),
            workdir=self.workdir,
            user="alice",
            config=DriverConfig(cache_mode=mode, poll_min_s=1.0, poll_max_s=4.0),
            clock=self.clock,
        )


@pytest.fixture
def env(tmp_path: Path, pg_url: URL) -> Iterator[Env]:
    clock = SleepRecorder()
    store = HookedStore.from_config({"url": url_string(pg_url)}, clock=clock)
    assert isinstance(store, HookedStore)
    store.before = {}
    workdir = tmp_path / "work"
    workdir.mkdir()
    try:
        yield Env(FsCAS(tmp_path / "cas", DOMAIN, clock=clock), store, clock, workdir)
    finally:
        store.close()


NODES = {"a": Node(), "b": Node(deps=(("a", "out"),)), "c": Node()}


def stored_outputs(env: Env, plan: Plan) -> dict[str, Digest]:
    """Put the bytes the scripted runner reports for each output into the CAS, registered."""
    found = {}
    for spec in plan.actions:
        data = f"{spec.action_id}/out".encode()
        found[spec.action_id] = env.cas.put_bytes(data)
        env.store.register_blobs(
            DOMAIN, [BlobRef(digest=found[spec.action_id], size=len(data), kind="file")]
        )
    return found


def assert_hits_valid(env: Env, outcome: BuildOutcome) -> None:
    for action_id, state in outcome.states.items():
        assert state == "cached", (action_id, state)
        result = env.store.get_result(outcome.build, action_id)
        assert result is not None
        for out in result.outputs.values():
            assert env.cas.has(out.digest), f"{action_id}: hit names a collected object"
            with env.cas.open(out.digest) as f:
                assert f.read() == f"{action_id}/out".encode()


# R7 (I15): a build that starts after the snapshot and before the row deletions keeps the rows
# it hits, and so their objects.
def test_build_during_row_deletion(env: Env) -> None:
    plan = make_plan(env.cas, NODES)
    outputs = stored_outputs(env, plan)
    assert env.run("write").status == "passed"
    env.clock.advance(31 * DAY)  # every cache row is stale

    started: list[BuildOutcome] = []
    env.store.before["gc_delete_cache_entries"] = lambda: started.append(env.run("read"))
    report = collect(env.store, env.cas, clock=env.clock)
    (concurrent,) = started
    assert_hits_valid(env, concurrent)
    assert report.cache_rows_deleted == 0  # every row was used since the snapshot
    # Only the plan blobs may be tombstoned: the concurrent build touched them, so they stay.
    assert not {t.digest for t in env.store.gc_tombstones(DOMAIN)} & set(outputs.values())

    for _ in range(3):  # later runs: still all there, the hit renewed the rows
        env.clock.advance(DAY)
        collect(env.store, env.cas, clock=env.clock)
    assert_hits_valid(env, env.run("read"))


# R7 (I15): rows deleted, objects tombstoned; when the grace period is over, a build that
# rebuilds the same bytes (a CAS put registers them) during the byte sweep keeps them.
def test_build_during_byte_deletion(env: Env) -> None:
    plan = make_plan(env.cas, NODES)
    outputs = stored_outputs(env, plan)
    assert env.run("write").status == "passed"
    env.clock.advance(31 * DAY)
    first = collect(env.store, env.cas, clock=env.clock)
    assert first.cache_rows_deleted == 3
    assert first.deleted == 0
    assert all(env.cas.has(d) for d in outputs.values())  # bytes wait for the grace period
    env.clock.advance(25 * 3600)

    def rebuild() -> None:  # the runner stores and registers its outputs, then posts results
        stored_outputs(env, plan)
        assert env.run("write").status == ("passed")

    env.store.before["gc_claim"] = rebuild
    second = collect(env.store, env.cas, clock=env.clock)
    assert second.deleted == 0  # the rebuild touched every object, plans included
    assert all(env.cas.has(d) for d in outputs.values())
    assert_hits_valid(env, env.run("read"))
