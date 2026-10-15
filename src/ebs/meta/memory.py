"""In-memory MetadataStore: the reference implementation for tests (interfaces.md § 6).

Shipped code, not test-only: it must pass the same contract suite as the PostgreSQL store. One
lock makes each method atomic; a method validates and stages everything before it changes state.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ebs.core.clock import Clock, SystemClock
from ebs.core.digest import Digest
from ebs.core.errors import MetadataError
from ebs.meta._common import (
    attribution_digests,
    cache_refs,
    check_blobs,
    check_digest,
    check_final_status,
    check_lease_ttl,
    check_pin,
    check_result,
    check_run,
    check_toolchain,
    edge_rows,
    revalidate,
    stored_result,
    touch_cutoff,
    transition,
)
from ebs.meta.api import (
    ActionRow,
    ActionState,
    BlobRef,
    BuildCreate,
    BuildId,
    BuildStatus,
    BuildView,
    DomainPolicy,
    Event,
    GcAttribution,
    GcBlob,
    GcCacheEntry,
    GcRoots,
    GcRun,
    LeaseId,
    RegisterOutcome,
    ResultManifest,
    Tombstone,
    ToolchainCreate,
    ToolchainRecord,
    check_domain,
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
        self._events: list[Event] = []
        self._toolchains: dict[str, ToolchainRecord] = {}
        self._toolchain_manifests: dict[str, bytes] = {}
        self._leases: dict[int, _Lease] = {}
        self._next_lease = 1
        self._pins: dict[int, tuple[str, str, datetime]] = {}  # build -> by, reason, at
        self._tombstones: dict[tuple[str, str], Tombstone] = {}
        self._policies: dict[str, DomainPolicy] = {}
        self.gc_runs: list[GcRun] = []

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
        try:
            return ResultManifest.model_validate(payload)
        except ValueError as exc:
            raise MetadataError(
                f"action cache entry {domain}/{key} holds an invalid result manifest; delete "
                f"the entry so the action reruns: {exc}"
            ) from None

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

    def finish_build(self, build: BuildId, status: BuildStatus) -> None:
        check_final_status(status)
        with self._lock:
            b = self._build(build)
            if b.view.status != "running":
                raise MetadataError(f"build {build} is already finished ({b.view.status})")
            b.view = b.view.model_copy(update={"status": status, "finished_at": self._clock.now()})
            # A finished build holds no lease; a pin is a separate root.
            for lease in [i for i, x in self._leases.items() if x.build == build]:
                del self._leases[lease]

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

    def pin_build(self, build: BuildId, *, by: str, reason: str = "") -> None:
        by, reason = check_pin(by, reason)
        with self._lock:
            b = self._build(build)
            self._pins.setdefault(build, (by, reason, self._clock.now()))
            roots = {str(b.view.plan_digest)}
            for payload in b.results.values():
                roots |= {str(d) for d in attribution_digests(payload)}
            self._touch_rows(b.view.domain, roots)
            b.view = b.view.model_copy(update={"pinned": True})

    def unpin_build(self, build: BuildId) -> bool:
        with self._lock:
            b = self._build(build)
            b.view = b.view.model_copy(update={"pinned": False})
            return self._pins.pop(build, None) is not None

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
                pinned.add(b.view.plan_digest)
                for payload in b.results.values():
                    pinned.update(attribution_digests(payload))
            leased = {
                Digest.parse(k)
                for lease in self._leases.values()
                if lease.domain == domain and lease.expires_at > now
                for k in lease.digests
            }
        return GcRoots(frozenset(pinned), frozenset(leased), frozenset())

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

    # --- events ---------------------------------------------------------------------------------

    def emit(self, event: Event) -> None:
        checked = revalidate(event, Event, "emit")
        with self._lock:
            self._build(checked.build)
            self._events.append(checked)

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
