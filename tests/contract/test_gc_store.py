"""GcStore contract suite (P1-09): `memory` as a unit test, `pg` as an integration test.

GC talks to the database directly as a service account, so only these two implement it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import URL

from ebs.core.clock import FakeClock
from ebs.core.digest import Digest, hash_bytes
from ebs.core.errors import MetadataError
from ebs.meta.api import (
    ActionRow,
    BlobRef,
    BuildCreate,
    BuildId,
    DomainPolicy,
    GcRun,
    GcStore,
    MetadataStore,
    OutputResult,
    ResourceUsage,
    ResultManifest,
    RunnerInfo,
)
from ebs.meta.memory import InMemoryMetadataStore
from ebs.meta.pg import PgMetadataStore
from tests.helpers.pg import url_string

D = "test"


class Store(GcStore, MetadataStore):
    """Both protocols, as the memory and PostgreSQL stores implement them."""


@dataclass
class G:
    store: Store
    clock: FakeClock
    set_policy: Callable[[DomainPolicy], None]


@pytest.fixture(params=["memory", pytest.param("pg", marks=pytest.mark.integration)])
def g(request: pytest.FixtureRequest) -> Iterator[G]:
    clock = FakeClock()
    if request.param == "memory":
        mem = InMemoryMetadataStore(clock=clock)
        yield G(mem, clock, lambda p: mem.set_policy(D, p))  # type: ignore[arg-type]
        return
    url: URL = request.getfixturevalue("pg_url")
    pg = PgMetadataStore.from_config({"url": url_string(url)}, clock=clock)

    def set_policy(p: DomainPolicy) -> None:
        with pg.engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO ebs.domains (name, ttl_days, quota_bytes, high_water, low_water) "
                    "VALUES (:d, :t, :q, :h, :l) ON CONFLICT (name) DO UPDATE SET "
                    "ttl_days = :t, quota_bytes = :q, high_water = :h, low_water = :l"
                ),
                {"d": D, "t": p.ttl_days, "q": p.quota_bytes, "h": p.high_water, "l": p.low_water},
            )

    try:
        yield G(pg, clock, set_policy)  # type: ignore[arg-type]
    finally:
        pg.close()


def dg(label: str) -> Digest:
    return hash_bytes(label.encode())


def result(key: Digest, outputs: dict[str, Digest], log: Digest | None = None) -> ResultManifest:
    return ResultManifest(
        action_key=key,
        status="passed",
        exit_code=0,
        inputs={},
        outputs={n: OutputResult(digest=d, id=d, type="file", size=1) for n, d in outputs.items()},
        log=log,
        summary={},
        resources=ResourceUsage(max_rss_kb=0, cpu_s=0, wall_s=0),
        runner=RunnerInfo(version="0", host="node-a"),
    )


def build(g: G, project: str = "demo", domain: str = D) -> BuildId:
    return g.store.create_build(
        BuildCreate(
            uuid=uuid4(), domain=domain, project=project, plan_digest=dg("plan"), flow_repo="",
            flow_commit="", flow_dirty=False, user_name="alice", cache_mode="write",
        )
    )  # fmt: skip


def test_policy(g: G) -> None:
    assert g.store.gc_policy(D) == DomainPolicy()
    g.set_policy(DomainPolicy(ttl_days=7, quota_bytes=1000, high_water=0.9, low_water=0.5))
    assert g.store.gc_policy(D) == DomainPolicy(7, 1000, 0.9, 0.5)
    with pytest.raises(MetadataError):
        g.store.gc_policy("bad domain")


def test_reads(g: G) -> None:
    g.store.register_blobs(D, [BlobRef(digest=dg("b"), size=5, kind="file")])
    g.store.register_blobs("other", [BlobRef(digest=dg("x"), size=5, kind="file")])
    (blob,) = g.store.gc_blobs(D)
    assert (blob.digest, blob.size, blob.kind, blob.last_access) == (
        dg("b"),
        5,
        "file",
        g.clock.now(),
    )

    key = dg("k")
    g.store.cache_put(D, key, result(key, {"b": dg("o2"), "a": dg("o1")}, log=dg("log")))
    (entry,) = g.store.gc_cache_entries(D)
    assert entry.key == key
    assert entry.outputs == tuple(sorted((dg("o1"), dg("o2")), key=str))
    assert entry.log == dg("log")
    assert g.store.gc_cache_entries("other") == []

    b = build(g, project="cpu")
    g.store.add_actions(b, [ActionRow(action_id="sim[t=1]", step="sim")])
    g.store.record_result(b, "sim[t=1]", result(dg("k2"), {"r": dg("res")}, log=dg("l2")))
    assert {(a.digest, a.build, a.project, a.step) for a in g.store.gc_attribution(D)} == {
        (dg("res"), b, "cpu", "sim"),
        (dg("l2"), b, "cpu", "sim"),
    }
    g.store.pin_build(b, by="alice")
    roots = g.store.gc_roots(D, g.clock.now())
    assert roots.pinned == {dg("plan"), dg("res"), dg("l2")}
    assert roots.released == frozenset()
    assert g.store.gc_roots("other", g.clock.now()).pinned == frozenset()


# I15: a pinned build without any recorded result still roots its plan.
def test_pinned_build_without_results(g: G) -> None:
    b = build(g)
    g.store.pin_build(b, by="alice")
    assert g.store.gc_roots(D, g.clock.now()).pinned == {dg("plan")}


# P1-09 follow-up: a pinned build also roots the final plan it finished with, so `ebs outputs`
# and `plan --diff` of an old pinned build keep their paths after the TTL.
def test_pinned_build_roots_final_plan(g: G) -> None:
    b = build(g)
    g.store.finish_build(b, "passed", final_plan=dg("final"))
    g.store.pin_build(b, by="alice")
    assert g.store.gc_roots(D, g.clock.now()).pinned == {dg("plan"), dg("final")}
    g.store.unpin_build(b)
    assert g.store.gc_roots(D, g.clock.now()).pinned == frozenset()


def test_leases_expire(g: G) -> None:
    b = build(g)
    g.store.acquire_lease(D, b, [dg("a")], 60)
    g.store.acquire_lease(D, b, [dg("b")], 900)
    g.clock.advance(61)
    assert g.store.gc_roots(D, g.clock.now()).leased == {dg("b")}
    assert g.store.gc_expire_leases(D, g.clock.now()) == 1
    assert g.store.gc_expire_leases(D, g.clock.now()) == 0
    assert g.store.gc_roots(D, g.clock.now()).leased == {dg("b")}


# I15: a cache row is deleted only if still unused since the cutoff.
def test_delete_cache_entries_rechecks(g: G) -> None:
    old, hit = dg("old"), dg("hit")
    for key in (old, hit):
        g.store.cache_put(D, key, result(key, {"o": dg(f"o-{key}")}))
    cutoff = g.clock.now()
    g.clock.advance(2 * 3600)
    g.store.cache_get(D, hit)  # used since the snapshot
    deleted = g.store.gc_delete_cache_entries(D, [old, hit, dg("absent")], unused_before=cutoff)
    assert deleted == [old]
    assert [e.key for e in g.store.gc_cache_entries(D)] == [hit]
    assert g.store.gc_delete_cache_entries(D, [], unused_before=cutoff) == []


# I15: tombstones, and the claim that lets GC delete bytes only when due and untouched.
def test_tombstones_and_claim(g: G) -> None:
    now = g.clock.now()
    later = now + timedelta(hours=24)
    a, b, c = dg("a"), dg("b"), dg("c")
    g.store.register_blobs(D, [BlobRef(digest=d, size=1, kind="file") for d in (a, b)])
    g.clock.advance(2 * 3600)
    now = g.clock.now()
    g.store.gc_tombstone(D, [a, b, c], now=now, delete_after=later)
    g.store.gc_tombstone(D, [a], now=later, delete_after=later + timedelta(days=9))  # kept
    stones = {t.digest: t for t in g.store.gc_tombstones(D)}
    assert set(stones) == {a, b, c}
    assert stones[a].deleted_rows_at == now
    assert stones[a].blob_delete_after == later
    assert g.store.gc_claim(D, a, now) is False  # not due yet
    g.clock.advance(3 * 3600)
    g.store.touch(D, [b])  # used after the tombstone
    assert g.store.gc_claim(D, a, later) is True
    assert g.store.gc_claim(D, a, later) is False  # claimed once
    assert g.store.gc_claim(D, b, later) is False
    assert g.store.gc_claim(D, c, later) is True  # no blob row at all
    assert {x.digest for x in g.store.gc_blobs(D)} == {b}
    g.store.gc_drop_tombstones(D, [b, dg("unknown")])
    g.store.gc_drop_tombstones(D, [])
    g.store.gc_tombstone(D, [], now=now, delete_after=later)
    assert g.store.gc_tombstones(D) == []


def test_record_run(g: G) -> None:
    now = g.clock.now()
    run = GcRun(D, now, now, 1, 2, 3, True, {"v": 1, "domain": D})
    g.store.gc_record_run(run)
    with pytest.raises(MetadataError, match="canonical"):
        g.store.gc_record_run(GcRun(D, now, now, 1, 2, 3, False, {"x": 1.5}))
    with pytest.raises(MetadataError, match="negative"):
        g.store.gc_record_run(GcRun(D, now, now, -1, 2, 3, False, {}))
