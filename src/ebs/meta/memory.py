"""In-memory MetadataStore: the reference implementation for tests (interfaces.md § 6).

Shipped code, not test-only: it must pass the same contract suite as the PostgreSQL store. One
lock makes each method atomic; a method validates and stages everything before it changes state.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from ebs.core.clock import Clock, SystemClock
from ebs.core.digest import Digest
from ebs.core.errors import MetadataError
from ebs.meta._common import (
    aggregate_stats,
    attribution_digests,
    build_matches,
    build_plans,
    cache_refs,
    check_archive,
    check_blobs,
    check_build_filter,
    check_by,
    check_digest,
    check_driver_owner,
    check_event_query,
    check_exportable,
    check_final_plan,
    check_final_status,
    check_lease_ttl,
    check_pin,
    check_queue,
    check_queue_exit,
    check_queue_ids,
    check_queue_items,
    check_release,
    check_release_imports,
    check_result,
    check_run,
    check_set_channel,
    check_slot,
    check_stats_window,
    check_toolchain,
    check_walk,
    edge_models,
    edge_rows,
    missing_queue_items,
    month_name,
    month_start,
    next_month,
    provenance_graph,
    release_matches,
    result_summary,
    revalidate,
    stored_result,
    touch_cutoff,
    transition,
    walk_levels,
)
from ebs.meta.api import (
    EVENT_PAGE_MAX,
    ActionRow,
    ActionState,
    ArchiveRecord,
    BlobRef,
    BuildCreate,
    BuildFilter,
    BuildId,
    BuildStatus,
    BuildView,
    ChannelMove,
    ChannelView,
    DomainPolicy,
    DomainStats,
    DriverLease,
    Event,
    ExportRecord,
    GateCheck,
    GcAttribution,
    GcBlob,
    GcCacheEntry,
    GcLock,
    GcRoots,
    GcRun,
    InfraReason,
    LeaseId,
    PilotSlot,
    ProvenanceAction,
    ProvenanceGraph,
    QueueEntry,
    QueueItem,
    QueueLease,
    QueueState,
    RegisterOutcome,
    ReleaseCreate,
    ReleaseId,
    ReleaseImport,
    ReleaseView,
    ResultManifest,
    ResultSummary,
    StoredEvent,
    Tombstone,
    ToolchainCreate,
    ToolchainRecord,
    check_domain,
    slot_fits,
)

__all__ = ["InMemoryMetadataStore"]

_EdgeKey = tuple[str, str, str, str]  # domain, action_key, direction, logical_path


@dataclass
class _CacheEntry:
    result: dict[str, object]  # JSON form, validated again on read like a database row
    created_at: datetime
    last_access: datetime
    hits: int


@dataclass
class _Blob:
    size: int
    kind: str
    created_at: datetime
    last_access: datetime


@dataclass
class _Lease:
    domain: str
    build: int
    expires_at: datetime
    digests: set[str]


@dataclass
class _QueueRow:
    domain: str
    pool: UUID
    item: QueueItem
    state: QueueState = "queued"
    pilot: str | None = None
    node: str | None = None
    expires_at: datetime | None = None
    exit_code: int | None = None
    infra_reason: InfraReason | None = None


@dataclass
class _Build:
    view: BuildView
    actions: dict[str, ActionRow]
    results: dict[str, dict[str, object]] = field(default_factory=dict)  # JSON, like a jsonb row


class InMemoryMetadataStore:
    """A MetadataStore kept in process memory."""

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()
        self._lock = threading.Lock()
        self._domains: set[str] = set()
        self._cache: dict[tuple[str, str], _CacheEntry] = {}
        self._builds: dict[int, _Build] = {}
        self._uuids: set[str] = set()
        self._edges: dict[_EdgeKey, tuple[str, str | None]] = {}  # -> object_id, content
        self._blobs: dict[tuple[str, str], _Blob] = {}
        self._events: list[StoredEvent] = []
        self._stored_at: dict[int, datetime] = {}  # id -> store clock (the PG partition key)
        self._last_event = 0
        self._toolchains: dict[str, ToolchainRecord] = {}
        self._toolchain_manifests: dict[str, bytes] = {}
        self._leases: dict[int, _Lease] = {}
        self._next_lease = 1
        self._pins: dict[int, tuple[str, str, datetime]] = {}  # build -> by, reason, at
        self._drivers: dict[int, DriverLease] = {}  # P2-08: build -> its driver lease
        self._queue: dict[int, _QueueRow] = {}  # P3-06: pilot queue items by id
        self._next_item = 1
        self._gc_locks: dict[str, GcLock] = {}  # domain -> the GC run holding it
        self._tombstones: dict[tuple[str, str], Tombstone] = {}
        self._policies: dict[str, DomainPolicy] = {}
        self.gc_runs: list[GcRun] = []
        self._releases: dict[int, dict[str, object]] = {}  # JSON, like a database row
        self._channels: dict[tuple[str, str, str], int] = {}  # -> last history entry
        self._history: list[ChannelMove] = []
        self._exports: list[ExportRecord] = []
        self._archives: list[ArchiveRecord] = []
        self._release_imports: dict[int, dict[str, ReleaseImport]] = {}  # build -> alias -> row

    # --- action cache ---------------------------------------------------------------------------

    def cache_get(self, domain: str, key: Digest) -> ResultManifest | None:
        check_domain(domain)
        check_digest(key, "cache_get key")
        with self._lock:
            entry = self._cache.get((domain, str(key)))
            if entry is None:
                return None
            now = self._clock.now()
            entry.hits += 1
            if entry.last_access <= touch_cutoff(now):
                entry.last_access = now
            payload = entry.result
        return _cached_result(domain, key, payload)

    def cache_peek(self, domain: str, key: Digest) -> ResultManifest | None:
        check_domain(domain)
        check_digest(key, "cache_peek key")
        with self._lock:
            entry = self._cache.get((domain, str(key)))
            payload = None if entry is None else entry.result
        return None if payload is None else _cached_result(domain, key, payload)

    def cache_put(self, domain: str, key: Digest, result: ResultManifest) -> bool:
        check_domain(domain)
        check_digest(key, "cache_put key")
        payload = check_result(key, result, f"cache_put {domain}/{key}").to_json()
        with self._lock:
            if (domain, str(key)) in self._cache:
                return False
            now = self._clock.now()
            self._domains.add(domain)
            self._cache[(domain, str(key))] = _CacheEntry(payload, now, now, 0)
            return True

    def cache_replace_failed(self, domain: str, key: Digest, result: ResultManifest) -> bool:
        check_domain(domain)
        check_digest(key, "cache_replace_failed key")
        payload = check_result(key, result, f"cache_replace_failed {domain}/{key}").to_json()
        with self._lock:
            entry = self._cache.get((domain, str(key)))
            if entry is None or entry.result.get("status") != "failed":
                return False
            now = self._clock.now()
            entry.result, entry.created_at, entry.last_access = payload, now, now
            return True

    # --- builds ---------------------------------------------------------------------------------

    def create_build(self, b: BuildCreate) -> BuildId:
        spec = revalidate(b, BuildCreate, "create_build")
        with self._lock:
            if str(spec.uuid) in self._uuids:
                raise MetadataError(f"a build with uuid {spec.uuid} already exists")
            build_id = BuildId(len(self._builds) + 1)
            view = BuildView(
                **dict(spec),
                id=build_id,
                status="running",
                created_at=self._clock.now(),
                finished_at=None,
                pinned=False,
                action_counts={},
            )
            self._domains.add(spec.domain)
            self._uuids.add(str(spec.uuid))
            self._builds[build_id] = _Build(view, {})
            return build_id

    def _build(self, build: BuildId) -> _Build:
        found = self._builds.get(build)
        if found is None:
            raise MetadataError(f"no build with id {build}")
        return found

    def _action(self, build: BuildId, action_id: str) -> tuple[_Build, ActionRow]:
        b = self._build(build)
        row = b.actions.get(action_id)
        if row is None:
            raise MetadataError(f"build {build} has no action {action_id!r}")
        return b, row

    def add_actions(self, build: BuildId, actions: Sequence[ActionRow]) -> None:
        rows = [revalidate(a, ActionRow, f"add_actions to build {build}") for a in actions]
        with self._lock:
            b = self._build(build)
            if b.view.status != "running":
                raise MetadataError(f"build {build} is finished ({b.view.status}); cannot add")
            now = self._clock.now()
            staged: dict[str, ActionRow] = {}
            for row in rows:
                if row.action_id in b.actions or row.action_id in staged:
                    raise MetadataError(
                        f"build {build} already has an action {row.action_id!r}; action ids "
                        "must be unique within a build"
                    )
                staged[row.action_id] = (
                    row if row.queued_at is not None else row.model_copy(update={"queued_at": now})
                )
            b.actions.update(staged)

    def set_action_state(
        self, build: BuildId, action_id: str, state: ActionState, **fields: object
    ) -> None:
        with self._lock:
            b, row = self._action(build, action_id)
            b.actions[action_id] = transition(row, state, fields, self._clock.now())

    def record_result(self, build: BuildId, action_id: str, result: ResultManifest) -> None:
        with self._lock:
            b, row = self._action(build, action_id)
            key = row.key if row.key is not None else result.action_key
            checked = check_result(key, result, f"record_result for action {action_id!r}")
            domain, now = b.view.domain, self._clock.now()
            new_row = row.model_copy(update={"key": key, "result_key": checked.action_key})
            edges = {
                (domain, str(key), direction, path): (object_id, content)
                for direction, path, object_id, content in edge_rows(checked)
            }
            blobs = {
                (domain, str(out.digest)): _Blob(out.size, out.type, now, now)
                for out in checked.outputs.values()
            }
            self._write_edges(edges)
            for k, blob in blobs.items():
                self._register(k, blob, now)
            b.actions[action_id] = new_row
            b.results[action_id] = checked.to_json()

    def get_result(self, build: BuildId, action_id: str) -> ResultManifest | None:
        with self._lock:
            b, _ = self._action(build, action_id)
            payload = b.results.get(action_id)
        return None if payload is None else stored_result(payload, build, action_id)

    def _write_edges(self, edges: dict[_EdgeKey, tuple[str, str | None]]) -> None:
        for k, v in edges.items():
            self._edges.setdefault(k, v)  # first writer wins, like ON CONFLICT DO NOTHING

    def resolve_output(self, domain: str, object_id: Digest) -> Digest | None:
        check_domain(domain)
        wanted = str(check_digest(object_id, "resolve_output object_id"))
        with self._lock:
            # An `out` edge is keyed by (producer key, output name) and never overwritten, so a
            # nondeterministic id has one row: the first recorded bytes.
            for (d, _key, direction, _path), (oid, content) in self._edges.items():
                if d == domain and direction == "out" and oid == wanted and content is not None:
                    return Digest.parse(content)
        return None

    def finish_build(
        self, build: BuildId, status: BuildStatus, *, final_plan: Digest | None = None
    ) -> None:
        check_final_status(status)
        final_plan = check_final_plan(final_plan)
        with self._lock:
            b = self._build(build)
            if b.view.status != "running":
                raise MetadataError(f"build {build} is already finished ({b.view.status})")
            b.view = b.view.model_copy(
                update={
                    "status": status,
                    "finished_at": self._clock.now(),
                    "final_plan_digest": final_plan,
                }
            )
            # A finished build holds no lease; a pin is a separate root.
            for lease in [i for i, x in self._leases.items() if x.build == build]:
                del self._leases[lease]

    def record_release_imports(self, build: BuildId, imports: Sequence[ReleaseImport]) -> None:
        with self._lock:
            b = self._build(build)
            recorded = self._release_imports.get(build, {})
            ids = {int(r.release) for r in imports}
            new = check_release_imports(
                build, b.view.domain, imports, {i: self._release(i) for i in ids},
                {alias: int(r.release) for alias, r in recorded.items()},
            )  # fmt: skip
            self._release_imports.setdefault(build, {}).update((r.alias, r) for r in new)

    def list_release_imports(self, build: BuildId) -> list[ReleaseImport]:
        with self._lock:
            self._build(build)
            rows = self._release_imports.get(build, {})
            return [rows[alias] for alias in sorted(rows)]

    def get_build(self, build: BuildId) -> BuildView:
        with self._lock:
            b = self._build(build)
            counts: dict[str, int] = {}
            for row in b.actions.values():
                counts[row.state] = counts.get(row.state, 0) + 1
            return b.view.model_copy(update={"action_counts": counts})

    def list_actions(self, build: BuildId, *, state: ActionState | None = None) -> list[ActionRow]:
        with self._lock:
            rows = self._build(build).actions.values()
            return sorted(
                (r for r in rows if state is None or r.state == state), key=lambda r: r.action_id
            )

    # --- dashboard reads (P2-06) ----------------------------------------------------------------

    def list_builds(self, domain: str, flt: BuildFilter) -> list[BuildView]:
        checked = check_build_filter(domain, flt)
        with self._lock:
            ids = [
                i
                for i, b in self._builds.items()
                if b.view.domain == domain and build_matches(b.view, checked)
            ]
        return [self.get_build(BuildId(i)) for i in sorted(ids, reverse=True)]

    def result_summaries(self, build: BuildId, *, step: str | None = None) -> list[ResultSummary]:
        with self._lock:
            b = self._build(build)
            found = [
                result_summary(a, b.results[a])
                for a, row in b.actions.items()
                if a in b.results and (step is None or row.step == step)
            ]
        return sorted(found, key=lambda r: r.action_id)

    def domain_stats(self, domain: str, *, since: datetime, until: datetime) -> DomainStats:
        check_stats_window(domain, since, until)
        with self._lock:
            chosen = {
                i: b
                for i, b in self._builds.items()
                if b.view.domain == domain and since <= b.view.created_at < until
            }
            rows = [
                (row.step, row.state, b.results.get(a))
                for b in chosen.values()
                for a, row in b.actions.items()
            ]
            evs = [e.event for e in self._events if e.event.build in chosen]
        return aggregate_stats(
            domain,
            since=since,
            until=until,
            now=self._clock.now(),
            builds=len(chosen),
            actions=rows,
            events=evs,
        )

    # --- access tracking -------------------------------------------------------------------------

    def touch(self, domain: str, digests: Iterable[Digest]) -> None:
        check_domain(domain)
        keys = [str(check_digest(d, "touch digest")) for d in digests]
        with self._lock:
            now = self._clock.now()
            for k in keys:
                blob = self._blobs.get((domain, k))
                if blob is not None and blob.last_access <= touch_cutoff(now):
                    blob.last_access = now

    def _register(self, k: tuple[str, str], blob: _Blob, now: datetime) -> None:
        """Insert a blob row, or touch the known one: storing an object again is a use (R1)."""
        known = self._blobs.get(k)
        if known is None:
            self._blobs[k] = blob
        elif known.last_access <= touch_cutoff(now):
            known.last_access = now

    def _touch_rows(self, domain: str, keys: Iterable[str]) -> None:
        """Touch known blob rows (lock held): a new lease or pin is a use, so a GC claim racing
        with it fails (I15)."""
        now = self._clock.now()
        for k in keys:
            blob = self._blobs.get((domain, k))
            if blob is not None and blob.last_access <= touch_cutoff(now):
                blob.last_access = now

    def register_blobs(self, domain: str, blobs: Iterable[BlobRef]) -> None:
        check_domain(domain)
        refs = check_blobs(blobs)
        with self._lock:
            now = self._clock.now()
            self._domains.add(domain)
            for ref in refs:
                self._register((domain, str(ref.digest)), _Blob(ref.size, ref.kind, now, now), now)

    # --- leases and pins ------------------------------------------------------------------------

    def acquire_lease(
        self, domain: str, build: BuildId, digests: Iterable[Digest], ttl_s: int
    ) -> LeaseId:
        check_domain(domain)
        check_lease_ttl(ttl_s)
        keys = {str(check_digest(d, "acquire_lease digest")) for d in digests}
        with self._lock:
            b = self._build(build)
            if b.view.domain != domain:
                raise MetadataError(
                    f"acquire_lease: build {build} is in domain {b.view.domain!r}, not {domain!r}"
                )
            if b.view.status != "running":
                raise MetadataError(
                    f"acquire_lease: build {build} is finished ({b.view.status}); only running "
                    "builds hold leases"
                )
            lease = self._next_lease
            self._next_lease += 1
            expires = self._clock.now() + timedelta(seconds=ttl_s)
            self._leases[lease] = _Lease(domain, build, expires, keys)
            self._touch_rows(domain, keys)
            return LeaseId(lease)

    def renew_lease(
        self, build: BuildId, lease: LeaseId, ttl_s: int, *, add: Iterable[Digest] = ()
    ) -> None:
        check_lease_ttl(ttl_s)
        keys = {str(check_digest(d, "renew_lease digest")) for d in add}
        with self._lock:
            now = self._clock.now()
            found = self._leases.get(lease)
            if found is None or found.build != build or found.expires_at <= now:
                raise MetadataError(
                    f"renew_lease: build {build} holds no live lease {lease} (it expired or was "
                    "released); acquire a new lease"
                )
            found.expires_at = now + timedelta(seconds=ttl_s)
            found.digests |= keys
            self._touch_rows(found.domain, keys)

    def claim_driver(self, build: BuildId, owner: str, ttl_s: int) -> DriverLease:
        check_driver_owner(owner, "claim_driver")
        check_lease_ttl(ttl_s)
        with self._lock:
            b = self._build(build)
            if b.view.status != "running":
                raise MetadataError(
                    f"claim_driver: build {build} is finished ({b.view.status}); only running "
                    "builds have a driver"
                )
            now = self._clock.now()
            held = self._drivers.get(build)
            if held is None or held.expires_at <= now or held.owner == owner:
                held = DriverLease(
                    build=build, owner=owner, expires_at=now + timedelta(seconds=ttl_s)
                )
                self._drivers[build] = held
            return held

    def release_driver(self, build: BuildId, owner: str) -> None:
        check_driver_owner(owner, "release_driver")
        with self._lock:
            held = self._drivers.get(build)
            if held is not None and held.owner == owner:
                del self._drivers[build]

    # --- pilot queues (P3-06) ----------------------------------------------------------------

    def queue_put(self, domain: str, pool: UUID, items: Sequence[QueueItem]) -> list[int]:
        check_queue(domain, pool, "queue_put")
        checked = check_queue_items(items)
        with self._lock:
            ids = []
            for item in checked:
                ids.append(self._next_item)
                self._queue[self._next_item] = _QueueRow(domain, pool, item)
                self._next_item += 1
            return ids

    def queue_lease(
        self, domain: str, pool: UUID, slot: PilotSlot, ttl_s: int
    ) -> QueueLease | None:
        check_queue(domain, pool, "queue_lease")
        slot = check_slot(slot, ttl_s)
        with self._lock:
            now = self._clock.now()
            for item_id, row in self._queue.items():  # insertion order: oldest first
                if (row.domain, row.pool, row.state) != (domain, pool, "queued"):
                    continue
                if not slot_fits(slot, row.item):
                    continue
                row.state, row.pilot, row.node = "leased", slot.pilot, slot.node
                row.expires_at = now + timedelta(seconds=ttl_s)
                return QueueLease(
                    item=item_id,
                    action_id=row.item.action_id,
                    plan=row.item.plan,
                    build=row.item.build,
                    expires_at=row.expires_at,
                )
            return None

    def queue_renew(self, domain: str, pool: UUID, item: int, pilot: str, ttl_s: int) -> bool:
        check_queue(domain, pool, "queue_renew")
        check_queue_ids([item], "queue_renew")
        check_lease_ttl(ttl_s)
        with self._lock:
            row = self._live_lease(domain, pool, item, pilot)
            if row is None:
                return False
            row.expires_at = self._clock.now() + timedelta(seconds=ttl_s)
            return True

    def queue_finish(
        self,
        domain: str,
        pool: UUID,
        item: int,
        pilot: str,
        exit_code: int,
        infra_reason: InfraReason | None = None,
    ) -> bool:
        check_queue(domain, pool, "queue_finish")
        check_queue_ids([item], "queue_finish")
        check_queue_exit(exit_code, infra_reason)
        with self._lock:
            row = self._live_lease(domain, pool, item, pilot)
            if row is None:
                return False
            row.state, row.exit_code, row.infra_reason = "done", exit_code, infra_reason
            row.expires_at = None
            return True

    def queue_status(self, domain: str, pool: UUID, items: Sequence[int]) -> list[QueueEntry]:
        check_queue(domain, pool, "queue_status")
        ids = check_queue_ids(items, "queue_status")
        with self._lock:
            rows = {i: self._queue.get(i) for i in ids}
            missing = [
                i for i, r in rows.items() if r is None or (r.domain, r.pool) != (domain, pool)
            ]
            if missing:
                raise missing_queue_items(missing, pool)
            now = self._clock.now()
            out = []
            for i in ids:
                row = rows[i]
                assert row is not None  # checked above
                if row.state == "leased" and row.expires_at is not None and row.expires_at <= now:
                    row.state, row.expires_at = "lost", None
                out.append(
                    QueueEntry(
                        item=i,
                        action_id=row.item.action_id,
                        state=row.state,
                        pilot=row.pilot,
                        node=row.node,
                        exit_code=row.exit_code,
                        infra_reason=row.infra_reason,
                    )
                )
            return out

    def queue_cancel(self, domain: str, pool: UUID, items: Sequence[int]) -> None:
        check_queue(domain, pool, "queue_cancel")
        ids = check_queue_ids(items, "queue_cancel")
        with self._lock:
            for i in ids:
                row = self._queue.get(i)
                if row is None or (row.domain, row.pool) != (domain, pool):
                    continue
                if row.state in ("queued", "leased"):
                    row.state, row.expires_at = "cancelled", None

    def _live_lease(self, domain: str, pool: UUID, item: int, pilot: str) -> _QueueRow | None:
        """`pilot`'s lease of `item` if still live; an expired one becomes `lost`."""
        row = self._queue.get(item)
        if row is None or (row.domain, row.pool, row.state, row.pilot) != (
            domain,
            pool,
            "leased",
            pilot,
        ):
            return None
        assert row.expires_at is not None  # set while leased
        if row.expires_at <= self._clock.now():
            row.state, row.expires_at = "lost", None
            return None
        return row

    def pin_build(self, build: BuildId, *, by: str, reason: str = "") -> None:
        by, reason = check_pin(by, reason)
        with self._lock:
            b = self._build(build)
            self._pins.setdefault(build, (by, reason, self._clock.now()))
            roots = {str(d) for d in build_plans(b.view)}
            for payload in b.results.values():
                roots |= {str(d) for d in attribution_digests(payload)}
            self._touch_rows(b.view.domain, roots)
            b.view = b.view.model_copy(update={"pinned": True})

    def unpin_build(self, build: BuildId) -> bool:
        with self._lock:
            b = self._build(build)
            b.view = b.view.model_copy(update={"pinned": False})
            return self._pins.pop(build, None) is not None

    # --- releases and channels (P2-02) -----------------------------------------------------------

    def _release(self, release: int) -> ReleaseView | None:
        row = self._releases.get(release)
        return None if row is None else ReleaseView.model_validate_json(json.dumps(row))

    def create_release(self, spec: ReleaseCreate, *, by: str) -> ReleaseView:
        checked = revalidate(spec, ReleaseCreate, "create_release")
        by = check_by(by, "create_release")
        with self._lock:
            b = self._build(checked.build)
            results = {a: stored_result(p, checked.build, a) for a, p in b.results.items()}
            check_release(checked, b.view, b.actions, results)
            return self._insert_release(checked, by, exported_from=None)

    def _insert_release(
        self, spec: ReleaseCreate, by: str, *, exported_from: int | None
    ) -> ReleaseView:
        key = (spec.domain, spec.name, spec.version)
        if any((r["domain"], r["name"], r["version"]) == key for r in self._releases.values()):
            raise MetadataError(
                f"release {spec.name} {spec.version} already exists in domain {spec.domain!r}; "
                "releases are immutable, so publish a new version"
            )
        release_id = ReleaseId(len(self._releases) + 1)
        view = ReleaseView(
            **dict(spec),
            id=release_id,
            created_at=self._clock.now(),
            created_by=by,
            exported_from=None if exported_from is None else ReleaseId(exported_from),
            yanked_at=None,
            yanked_by=None,
            yank_reason=None,
        )
        self._domains.add(spec.domain)
        self._releases[release_id] = view.model_dump(mode="json")
        return view.model_copy(deep=True)

    def get_release(self, domain: str, name: str, version: str) -> ReleaseView | None:
        check_domain(domain)
        with self._lock:
            for rid, r in self._releases.items():
                if (r["domain"], r["name"], r["version"]) == (domain, name, version):
                    return self._release(rid)
        return None

    def get_release_by_id(self, release: ReleaseId) -> ReleaseView | None:
        with self._lock:
            return self._release(release)

    def list_releases(self, domain: str, *, name: str | None = None) -> list[ReleaseView]:
        check_domain(domain)
        with self._lock:
            found = [
                v
                for rid in self._releases
                if (v := self._release(rid)) is not None
                and v.domain == domain
                and (name is None or v.name == name)
            ]
        return sorted(found, key=lambda v: (v.name, v.created_at, v.id))

    def yank_release(self, release: ReleaseId, *, by: str, reason: str) -> ReleaseView:
        by = check_by(by, "yank_release")
        if not isinstance(reason, str) or not reason or len(reason) > 4096:
            raise MetadataError("yank_release: give a reason (1-4096 characters)")
        with self._lock:
            view = self._release(release)
            if view is None:
                raise MetadataError(f"yank_release: no release with id {release}")
            if view.yanked_at is None:
                view = view.model_copy(
                    update={"yanked_at": self._clock.now(), "yanked_by": by, "yank_reason": reason}
                )
                self._releases[release] = view.model_dump(mode="json")
            return view

    def set_channel(
        self,
        domain: str,
        name: str,
        channel: str,
        release: ReleaseId,
        *,
        by: str,
        gate: GateCheck | None = None,
    ) -> ChannelMove:
        by = check_by(by, "set_channel")
        checked_gate = None if gate is None else revalidate(gate, GateCheck, "set_channel gate")
        with self._lock:
            view = self._release(release)
            check_set_channel(view, domain, name, channel)
            assert view is not None
            move = ChannelMove(
                id=len(self._history) + 1,
                domain=domain,
                name=name,
                channel=channel,
                release=view.id,
                version=view.version,
                moved_at=self._clock.now(),
                moved_by=by,
                gate=checked_gate,
            )
            self._history.append(move)
            self._channels[(domain, name, channel)] = move.id
            return move

    def get_channel(self, domain: str, name: str, channel: str) -> ChannelView | None:
        check_domain(domain)
        with self._lock:
            last = self._channels.get((domain, name, channel))
            if last is None:
                return None
            move = self._history[last - 1]
            view = self._release(move.release)
            assert view is not None
            return ChannelView(
                domain=domain, name=name, channel=channel, release=view,
                updated_at=move.moved_at, updated_by=move.moved_by,
            )  # fmt: skip

    def channel_history(self, domain: str, name: str, channel: str) -> list[ChannelMove]:
        check_domain(domain)
        with self._lock:
            return [m for m in self._history if (m.domain, m.name, m.channel) == (
                domain, name, channel,
            )]  # fmt: skip

    def gate_builds(self, domain: str, flow: str, ids: Iterable[Digest]) -> list[BuildId]:
        check_domain(domain)
        wanted = {str(check_digest(d, "gate_builds id")) for d in ids}
        with self._lock:
            keys = {
                key
                for (d, key, direction, _path), (oid, _content) in self._edges.items()
                if d == domain and direction == "in" and oid in wanted
            }
            found = [
                BuildId(bid)
                for bid, b in self._builds.items()
                if b.view.domain == domain
                and b.view.project == flow
                and b.view.status == "passed"
                and any(r.key is not None and str(r.key) in keys for r in b.actions.values())
            ]
        return sorted(found, reverse=True)

    # --- provenance (ProvenanceStore, P2-04) -----------------------------------------------------

    def provenance_upstream(
        self, domain: str, *, ids: Iterable[Digest], keys: Iterable[Digest], depth: int
    ) -> ProvenanceGraph:
        return self._walk("upstream", domain, ids, keys, depth)

    def provenance_downstream(
        self, domain: str, *, ids: Iterable[Digest], keys: Iterable[Digest], depth: int
    ) -> ProvenanceGraph:
        return self._walk("downstream", domain, ids, keys, depth)

    def _walk(
        self, way: str, domain: str, ids: Iterable[Digest], keys: Iterable[Digest], depth: object
    ) -> ProvenanceGraph:
        domain, id_list, key_list, levels = check_walk(
            domain, ids, keys, depth, f"provenance_{way}"
        )
        with self._lock:
            edges = edge_models(
                (
                    (key, direction, path, oid, content)
                    for (d, key, direction, path), (oid, content) in self._edges.items()
                    if d == domain
                ),
                f"provenance_{way} {domain}",
            )
            # follow: the direction whose ids lead to the next level (the other one leads back).
            follow = "in" if way == "upstream" else "out"
            ahead: dict[str, set[str]] = {}  # key -> ids on its `follow` edges
            behind: dict[str, set[str]] = {}  # id -> keys holding it on a `back` edge
            by_content: dict[str, set[str]] = {}  # upstream only: content digest -> producers
            for e in edges:
                k, oid = str(e.action_key), str(e.object_id)
                if e.direction == follow:
                    ahead.setdefault(k, set()).add(oid)
                    continue
                behind.setdefault(oid, set()).add(k)
                if e.content_digest is not None and way == "upstream":
                    by_content.setdefault(str(e.content_digest), set()).add(k)

            def step(level: set[str]) -> set[str]:
                return {n for k in level for i in ahead.get(k, ()) for n in behind.get(i, ())}

            first = set(key_list) | {
                k for i in id_list for k in behind.get(i, set()) | by_content.get(i, set())
            }
            walked, truncated = walk_levels(first, step, levels)
            kept = [e for e in edges if str(e.action_key) in walked]
            objects = set(id_list) | {str(e.object_id) for e in kept}
            rows = [
                self._provenance_action(b, row)
                for b in self._builds.values()
                if b.view.domain == domain
                for row in b.actions.values()
                if row.key is not None and str(row.key) in walked
            ]
            found = [
                match
                for rid in sorted(self._releases)
                if (rel := self._release(rid)) is not None and rel.domain == domain
                for match in release_matches(rel, objects)
            ]
        return provenance_graph(domain, kept, rows, found, truncated)

    @staticmethod
    def _provenance_action(b: _Build, row: ActionRow) -> ProvenanceAction:
        assert row.key is not None
        result = b.results.get(row.action_id)
        status = None if result is None else result.get("status")
        return ProvenanceAction(
            key=row.key, build=b.view.id, action_id=row.action_id, step=row.step, rule=row.rule,
            params=row.params, toolchain=row.toolchain, state=row.state, cached=row.cached,
            status=status, finished_at=row.finished_at,  # type: ignore[arg-type]
            project=b.view.project, flow_repo=b.view.flow_repo, flow_commit=b.view.flow_commit,
            flow_dirty=b.view.flow_dirty, user_name=b.view.user_name,
            build_created_at=b.view.created_at,
        )  # fmt: skip

    def record_export(self, release: ReleaseId, to_domain: str, *, by: str) -> ReleaseView:
        check_domain(to_domain)
        by = check_by(by, "record_export")
        with self._lock:
            source = self._release(release)
            if source is None:
                raise MetadataError(f"record_export: no release with id {release}")
            check_exportable(source)
            if source.domain == to_domain:
                raise MetadataError(
                    f"record_export: release {release} is already in domain {to_domain!r}"
                )
            spec = ReleaseCreate.model_validate(
                {**source.model_dump(include=set(ReleaseCreate.model_fields)), "domain": to_domain}
            )
            exported = self._insert_release(spec, by, exported_from=source.id)
            self._exports.append(
                ExportRecord(
                    id=len(self._exports) + 1, release=source.id, exported=exported.id,
                    to_domain=to_domain, exported_at=self._clock.now(), exported_by=by,
                )
            )  # fmt: skip
            return exported

    def list_exports(self, release: ReleaseId) -> list[ExportRecord]:
        with self._lock:
            return [e for e in self._exports if release in (e.release, e.exported)]

    def record_archive(
        self, release: ReleaseId, bundle: Digest, location: str, *, by: str
    ) -> ArchiveRecord:
        bundle, location = check_archive(bundle, location)
        by = check_by(by, "record_archive")
        with self._lock:
            if self._release(release) is None:
                raise MetadataError(f"record_archive: no release with id {release}")
            for known in self._archives:
                if (known.release, known.bundle, known.location) == (release, bundle, location):
                    return known
            record = ArchiveRecord(
                id=len(self._archives) + 1, release=release, bundle=bundle, location=location,
                created_at=self._clock.now(), created_by=by,
            )  # fmt: skip
            self._archives.append(record)
            return record

    def list_archives(self, release: ReleaseId) -> list[ArchiveRecord]:
        with self._lock:
            return [a for a in self._archives if a.release == release]

    # --- garbage collection (GcStore) -----------------------------------------------------------

    def set_policy(self, domain: str, policy: DomainPolicy) -> None:
        """Test hook for the `domains` row settings (P2-01 adds the admin side)."""
        with self._lock:
            self._policies[check_domain(domain)] = policy

    def gc_policy(self, domain: str) -> DomainPolicy:
        check_domain(domain)
        with self._lock:
            return self._policies.get(domain, DomainPolicy())

    def gc_blobs(self, domain: str) -> list[GcBlob]:
        check_domain(domain)
        with self._lock:
            return [
                GcBlob(Digest.parse(k), b.size, b.kind, b.created_at, b.last_access)
                for (d, k), b in sorted(self._blobs.items())
                if d == domain
            ]

    def gc_cache_entries(self, domain: str) -> list[GcCacheEntry]:
        check_domain(domain)
        with self._lock:
            rows = [
                (k, e.last_access, e.result) for (d, k), e in self._cache.items() if d == domain
            ]
        return [
            GcCacheEntry(Digest.parse(k), at, *cache_refs(payload))
            for k, at, payload in sorted(rows, key=lambda r: r[0])
        ]

    def gc_roots(self, domain: str, now: datetime) -> GcRoots:
        check_domain(domain)
        pinned: set[Digest] = set()
        with self._lock:
            for build_id in self._pins:
                b = self._builds[build_id]
                if b.view.domain != domain:
                    continue
                pinned.update(build_plans(b.view))
                for payload in b.results.values():
                    pinned.update(attribution_digests(payload))
            leased = {
                Digest.parse(k)
                for lease in self._leases.values()
                if lease.domain == domain and lease.expires_at > now
                for k in lease.digests
            }
            released = {
                Digest.parse(str(o["digest"]))
                for r in self._releases.values()
                if r["domain"] == domain
                for o in r["outputs"].values()  # type: ignore[attr-defined]
            }
        return GcRoots(frozenset(pinned), frozenset(leased), frozenset(released))

    def gc_tombstones(self, domain: str) -> list[Tombstone]:
        check_domain(domain)
        with self._lock:
            return [t for (d, _), t in sorted(self._tombstones.items()) if d == domain]

    def gc_attribution(self, domain: str) -> list[GcAttribution]:
        check_domain(domain)
        found: list[GcAttribution] = []
        with self._lock:
            for build_id, b in sorted(self._builds.items()):
                if b.view.domain != domain:
                    continue
                project, build = b.view.project, BuildId(build_id)
                for action_id, payload in sorted(b.results.items()):
                    step = b.actions[action_id].step
                    found.extend(
                        GcAttribution(d, build, project, step) for d in attribution_digests(payload)
                    )
        return found

    def gc_expire_leases(self, domain: str, now: datetime) -> int:
        check_domain(domain)
        with self._lock:
            gone = [
                i for i, x in self._leases.items() if x.domain == domain and x.expires_at <= now
            ]
            for i in gone:
                del self._leases[i]
            return len(gone)

    def gc_delete_cache_entries(
        self, domain: str, keys: Iterable[Digest], *, unused_before: datetime
    ) -> list[Digest]:
        check_domain(domain)
        wanted = [check_digest(k, "gc_delete_cache_entries key") for k in keys]
        deleted: list[Digest] = []
        with self._lock:
            for key in wanted:
                entry = self._cache.get((domain, str(key)))
                if entry is not None and entry.last_access <= unused_before:
                    del self._cache[(domain, str(key))]
                    deleted.append(key)
        return deleted

    def gc_tombstone(
        self, domain: str, digests: Iterable[Digest], *, now: datetime, delete_after: datetime
    ) -> None:
        check_domain(domain)
        wanted = [check_digest(d, "gc_tombstone digest") for d in digests]
        with self._lock:
            self._domains.add(domain)
            for d in wanted:
                self._tombstones.setdefault((domain, str(d)), Tombstone(d, now, delete_after))

    def gc_drop_tombstones(self, domain: str, digests: Iterable[Digest]) -> None:
        check_domain(domain)
        wanted = [str(check_digest(d, "gc_drop_tombstones digest")) for d in digests]
        with self._lock:
            for k in wanted:
                self._tombstones.pop((domain, k), None)

    def gc_claim(self, domain: str, digest: Digest, now: datetime) -> bool:
        check_domain(domain)
        k = (domain, str(check_digest(digest, "gc_claim digest")))
        with self._lock:
            stone = self._tombstones.get(k)
            if stone is None or stone.blob_delete_after > now:
                return False
            blob = self._blobs.get(k)
            if blob is not None and blob.last_access >= stone.deleted_rows_at:
                return False
            del self._tombstones[k]
            self._blobs.pop(k, None)
            return True

    def gc_record_run(self, run: GcRun) -> None:
        checked = check_run(run)
        with self._lock:
            self._domains.add(checked.domain)
            self.gc_runs.append(checked)

    def gc_lock(self, domain: str, owner: str, *, now: datetime, ttl_s: int) -> GcLock:
        check_domain(domain)
        check_driver_owner(owner, "gc_lock")
        check_lease_ttl(ttl_s)
        with self._lock:
            held = self._gc_locks.get(domain)
            if held is None or held.expires_at <= now or held.owner == owner:
                held = GcLock(domain, owner, now + timedelta(seconds=ttl_s))
                self._gc_locks[domain] = held
            return held

    def gc_unlock(self, domain: str, owner: str) -> None:
        check_domain(domain)
        with self._lock:
            held = self._gc_locks.get(domain)
            if held is not None and held.owner == owner:
                del self._gc_locks[domain]

    # --- events ---------------------------------------------------------------------------------

    def emit(self, event: Event) -> None:
        checked = revalidate(event, Event, "emit")
        with self._lock:
            self._build(checked.build)
            self._last_event += 1
            self._events.append(StoredEvent(id=self._last_event, event=checked))
            self._stored_at[self._last_event] = self._clock.now()

    def events_after(
        self,
        after: int,
        *,
        build: BuildId | None = None,
        domain: str | None = None,
        limit: int = EVENT_PAGE_MAX,
    ) -> list[StoredEvent]:
        check_event_query(build, domain, limit)
        with self._lock:
            found = [
                e
                for e in self._events
                if e.id > after
                and (
                    e.event.build == build
                    if build is not None
                    else self._builds[e.event.build].view.domain == domain
                )
            ]
        return found[:limit]

    def ensure_event_partitions(self, at: datetime, *, months_ahead: int = 1) -> list[str]:
        """Nothing to create in memory: the months `PgMetadataStore` would partition."""
        month, names = month_start(at), []
        for _ in range(months_ahead + 1):
            names.append(f"events_y{month.year:04}m{month.month:02}")
            month = next_month(month)
        return names

    def prune_events(self, before: datetime, *, dry_run: bool = False) -> list[str]:
        with self._lock:
            months = sorted(
                {
                    start
                    for e in self._events
                    if next_month(start := month_start(self._stored_at[e.id])) <= before
                }
            )
            if not dry_run:
                gone = set(months)
                # Ids are kept: the list stays in id order, and new ids continue after the last.
                kept = [e for e in self._events if month_start(self._stored_at[e.id]) not in gone]
                for e in self._events:
                    if month_start(self._stored_at[e.id]) in gone:
                        del self._stored_at[e.id]
                self._events = kept
        return [month_name(m) for m in months]

    # --- toolchain registry ---------------------------------------------------------------------

    def register_toolchain(self, spec: ToolchainCreate, manifest: bytes) -> RegisterOutcome:
        checked = check_toolchain(spec, manifest)
        new = str(checked.id)
        with self._lock:
            now = self._clock.now()
            existing = self._toolchains.get(new)
            if existing is not None and existing.superseded_by is None:
                return RegisterOutcome(inserted=False, superseded=())
            if existing is None:
                self._toolchains[new] = ToolchainRecord(
                    **dict(checked), registered_at=now, superseded_by=None, last_used_at=None
                )
                self._toolchain_manifests[new] = bytes(manifest)
            else:
                self._toolchains[new] = existing.model_copy(update={"superseded_by": None})
            superseded = []
            for key, row in self._toolchains.items():
                if row.module == checked.module and row.superseded_by is None and key != new:
                    self._toolchains[key] = row.model_copy(update={"superseded_by": checked.id})
                    superseded.append(row.id)
            return RegisterOutcome(
                inserted=existing is None, superseded=tuple(sorted(superseded, key=str))
            )

    def get_toolchain(self, toolchain_id: Digest) -> ToolchainRecord | None:
        key = str(check_digest(toolchain_id, "get_toolchain id"))
        with self._lock:
            return self._toolchains.get(key)

    def current_toolchain(self, module: str) -> ToolchainRecord | None:
        rows = self.list_toolchains(module=module, current_only=True)
        return rows[-1] if rows else None

    def list_toolchains(
        self, *, module: str | None = None, current_only: bool = False
    ) -> list[ToolchainRecord]:
        with self._lock:
            rows = list(self._toolchains.values())
        return sorted(
            (
                r
                for r in rows
                if (module is None or r.module == module)
                and (not current_only or r.superseded_by is None)
            ),
            key=lambda r: (r.module, r.registered_at, str(r.id)),
        )

    def toolchain_manifest(self, toolchain_id: Digest) -> bytes | None:
        key = str(check_digest(toolchain_id, "toolchain_manifest id"))
        with self._lock:
            return self._toolchain_manifests.get(key)

    def touch_toolchain(self, toolchain_id: Digest) -> None:
        key = str(check_digest(toolchain_id, "touch_toolchain id"))
        with self._lock:
            row = self._toolchains.get(key)
            now = self._clock.now()
            if row is not None and (
                row.last_used_at is None or row.last_used_at <= touch_cutoff(now)
            ):
                self._toolchains[key] = row.model_copy(update={"last_used_at": now})


def _cached_result(domain: str, key: Digest, payload: object) -> ResultManifest:
    try:
        return ResultManifest.model_validate(payload)
    except ValueError as exc:
        raise MetadataError(
            f"action cache entry {domain}/{key} holds an invalid result manifest; delete "
            f"the entry so the action reruns: {exc}"
        ) from None
