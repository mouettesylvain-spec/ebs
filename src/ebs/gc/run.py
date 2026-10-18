"""One GC run over one domain (P1-09 R4-R8): snapshot, plan, apply, report.

GC runs as a service account that belongs to the domain's group (R7), one domain per run
(`ebs gc --domain D`, systemd `ebs-gc@D.timer`). It is safe to run while builds run, because
every destructive step re-checks its condition in the database at the moment it acts:

- a cache row is deleted only if it is still unused since the cutoff (a hit since the snapshot
  keeps it, and the objects it references are then not tombstoned);
- bytes go only for a tombstone that is due and whose blob row nobody touched since: builds
  touch what they hit, fetch or stage, leases and pins touch what they protect, and every CAS
  put registers (touches) its objects, so a build that starts using an object during the sweep
  keeps it.

One run per domain at a time: a run that changes anything takes the domain's GC lock
(`GcStore.gc_lock`, a row with an expiry, since PgBouncer's transaction pooling rules out
session advisory locks) and renews it between phases; a second run fails with GcBusyError.
A dry run takes no lock.

A put of bytes that already exist keeps the stored object, which GC may claim between the put
and its registration. Writers therefore check the CAS again after registering (after which no
claim can succeed): the driver stores a vanished plan again (`ebs.gc.access.put_registered`),
the runner fails the attempt as an infrastructure error and the retry stores its outputs again.
"""

from __future__ import annotations

import os
import secrets
import socket
from collections.abc import Iterator
from datetime import UTC, timedelta
from typing import Final, Protocol

from ebs.cas.api import ObjectKind
from ebs.core.clock import Clock
from ebs.core.digest import Digest
from ebs.core.errors import GcBusyError
from ebs.core.log import get_logger
from ebs.core.tree import TreeManifest
from ebs.gc.report import GcReport, QuotaReport, reclaimable
from ebs.gc.roots import TreeGraph, read_graph
from ebs.gc.sweep import GcConfig, Snapshot, SweepPlan, entry_refs, plan_sweep
from ebs.meta.api import TOUCH_INTERVAL_S, BlobRef, GcRun, GcStore

__all__ = ["GC_LOCK_TTL_S", "GcCas", "collect", "take_snapshot"]

_log = get_logger(__name__)

GC_LOCK_TTL_S: Final = 6 * 3600
"""How long a GC run holds its domain's lock without renewing it (a crashed run frees it then)."""


class GcCas(Protocol):
    """What GC needs from a CAS backend (the fs backend has it all)."""

    @property
    def domain(self) -> str: ...

    def iter_objects(self) -> Iterator[tuple[Digest, int, ObjectKind]]: ...

    def get_tree(self, d: Digest) -> TreeManifest: ...

    def delete(self, d: Digest) -> None: ...

    def cleanup_tmp(self, max_age: timedelta = ..., *, dry_run: bool = False) -> int: ...


def take_snapshot(store: GcStore, cas: GcCas, clock: Clock) -> Snapshot:
    """The CAS first (the source of truth for bytes), then the database."""
    domain = cas.domain
    sizes: dict[Digest, int] = {}
    trees: set[Digest] = set()
    for digest, size, kind in cas.iter_objects():
        sizes[digest] = sizes.get(digest, 0) + size
        if kind == "tree":
            trees.add(digest)
    graph: TreeGraph = read_graph(cas, sorted(trees, key=str))
    now = clock.now()
    return Snapshot(
        domain=domain,
        now=now,
        policy=store.gc_policy(domain),
        sizes=sizes,
        trees=frozenset(trees),
        graph=graph,
        blobs={b.digest: b for b in store.gc_blobs(domain)},
        entries=tuple(store.gc_cache_entries(domain)),
        roots=store.gc_roots(domain, now),
        tombstones={t.digest: t for t in store.gc_tombstones(domain)},
    )


def collect(
    store: GcStore,
    cas: GcCas,
    *,
    clock: Clock,
    config: GcConfig | None = None,
    dry_run: bool = False,
) -> GcReport:
    """Run GC once over `cas.domain`; with `dry_run`, report without changing anything.
    GcBusyError if another run holds the domain's GC lock."""
    if dry_run:
        return _collect(store, cas, clock=clock, config=config or GcConfig(), dry_run=True)
    lock = _Lock(store, cas.domain, clock)
    lock.take()
    try:
        return _collect(store, cas, clock=clock, config=config or GcConfig(), lock=lock)
    finally:
        lock.release()


class _Lock:
    def __init__(self, store: GcStore, domain: str, clock: Clock) -> None:
        self._store, self._domain, self._clock = store, domain, clock
        self._owner = f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(4)}"

    def take(self) -> None:
        """Take or renew the lock; GcBusyError while another run holds it."""
        held = self._store.gc_lock(
            self._domain, self._owner, now=self._clock.now(), ttl_s=GC_LOCK_TTL_S
        )
        if held.owner != self._owner:
            raise GcBusyError(
                f"another ebs gc run ({held.owner}) is collecting domain {self._domain!r} "
                f"until {held.expires_at.isoformat()} at the latest; wait for it to finish"
            )

    def release(self) -> None:
        try:
            self._store.gc_unlock(self._domain, self._owner)
        except Exception as exc:  # the lock expires on its own
            _log.warning("gc.unlock_failed", domain=self._domain, error=str(exc))


def _collect(
    store: GcStore,
    cas: GcCas,
    *,
    clock: Clock,
    config: GcConfig,
    dry_run: bool = False,
    lock: _Lock | None = None,
) -> GcReport:
    snap = take_snapshot(store, cas, clock)
    plan = plan_sweep(snap, config)
    domain, now = snap.domain, snap.now
    tmp_removed = cas.cleanup_tmp(config.tmp_max_age, dry_run=dry_run)
    if dry_run:
        rows_deleted = len(plan.delete_rows) + (len(plan.quota.rows) if plan.quota else 0)
        tombstoned = plan.tombstone
        deleted = plan.due
    else:
        assert lock is not None
        lock.take()  # renewed after the snapshot, which can take long on a big CAS
        rows_deleted, tombstoned, deleted = _apply(store, cas, snap, plan, config)
    report = GcReport(
        domain=domain,
        dry_run=dry_run,
        started_at=now.astimezone(UTC).isoformat(),
        objects=len(snap.sizes),
        bytes=sum(snap.sizes.values()),
        marked=len(plan.marked.reachable & snap.sizes.keys()),
        kept=len(plan.keep & snap.sizes.keys()),
        adopted=len(plan.adopt),
        cache_rows_deleted=rows_deleted,
        tombstoned=len(tombstoned),
        tombstoned_bytes=sum(snap.sizes[d] for d in tombstoned),
        resurrected=len(plan.resurrect),
        deleted=len(deleted),
        deleted_bytes=sum(snap.sizes[d] for d in deleted),
        tmp_removed=tmp_removed,
        quota=None
        if plan.quota is None
        else QuotaReport(
            quota_bytes=plan.quota.quota_bytes,
            usage_bytes=plan.quota.usage_bytes,
            pending_bytes=plan.quota.pending_bytes,
            high_water_bytes=plan.quota.high_water_bytes,
            low_water_bytes=plan.quota.low_water_bytes,
            evicted=[str(d) for d in plan.quota.evicted],
            evicted_bytes=plan.quota.evicted_bytes,
        ),
        reclaimable=reclaimable(
            snap.sizes, snap.sizes.keys() - plan.keep, store.gc_attribution(domain), snap.graph
        ),
    )
    store.gc_record_run(
        GcRun(
            domain=domain,
            started_at=now,
            finished_at=clock.now(),
            marked=report.marked,
            swept_rows=report.cache_rows_deleted,
            swept_bytes=report.deleted_bytes,
            dry_run=dry_run,
            report=report.model_dump(mode="json"),
        )
    )
    _log.info(
        "gc.finished",
        domain=domain,
        dry_run=dry_run,
        rows=report.cache_rows_deleted,
        tombstoned=report.tombstoned,
        deleted=report.deleted,
        deleted_bytes=report.deleted_bytes,
    )
    return report


def _apply(
    store: GcStore, cas: GcCas, snap: Snapshot, plan: SweepPlan, config: GcConfig
) -> tuple[int, tuple[Digest, ...], tuple[Digest, ...]]:
    """Carry out `plan` in I15 order; returns (rows deleted, tombstoned, bytes deleted)."""
    domain, now, graph = snap.domain, snap.now, snap.graph
    expired = store.gc_expire_leases(domain, now)
    if expired:
        _log.info("gc.leases_expired", domain=domain, count=expired)
    if plan.adopt:
        kinds = {d: "tree" if d in snap.trees else "file" for d in plan.adopt}
        store.register_blobs(
            domain,
            [BlobRef(digest=d, size=snap.sizes[d], kind=kinds[d]) for d in plan.adopt],  # type: ignore[arg-type]
        )

    # 1. Cache rows first (I15), each re-checked against its cutoff.
    cutoff = now - timedelta(days=snap.policy.ttl_days)
    deleted_rows = set(
        store.gc_delete_cache_entries(domain, plan.delete_rows, unused_before=cutoff)
    )
    planned = set(plan.delete_rows)
    if plan.quota is not None and plan.quota.rows:
        deleted_rows |= set(
            # A hit since the snapshot moves last_access past this (touches are throttled to
            # one per TOUCH_INTERVAL_S), so such a row stays.
            store.gc_delete_cache_entries(
                domain, plan.quota.rows, unused_before=now - timedelta(seconds=TOUCH_INTERVAL_S)
            )
        )
        planned |= set(plan.quota.rows)
    # A row used since the snapshot stays, and so does everything it references.
    held: set[Digest] = set()
    for entry in snap.entries:
        if entry.key in planned - deleted_rows:
            held |= graph.closure(entry_refs(entry))
    if held:
        _log.info("gc.rows_kept_by_concurrent_use", domain=domain, objects=len(held))

    # 2. Tombstones: the bytes wait for the grace period.
    tombstoned = tuple(d for d in plan.tombstone if d not in held)
    store.gc_tombstone(domain, tombstoned, now=now, delete_after=now + config.grace)
    store.gc_drop_tombstones(domain, plan.resurrect)

    # 3. Bytes of due tombstones, parents first; a claim fails if the object was touched since.
    skipped: set[Digest] = set()
    deleted: list[Digest] = []
    for d in plan.due:
        if d in skipped:
            continue
        if not store.gc_claim(domain, d, now):
            skipped |= graph.closure([d])  # a tree still in use keeps its children
            continue
        cas.delete(d)
        deleted.append(d)
    return len(deleted_rows), tombstoned, tuple(deleted)
