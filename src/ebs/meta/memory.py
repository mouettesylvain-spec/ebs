"""In-memory MetadataStore: the reference implementation for tests (interfaces.md § 6).

Shipped code, not test-only: it must pass the same contract suite as the PostgreSQL store. One
lock makes each method atomic; a method validates and stages everything before it changes state.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from ebs.core.clock import Clock, SystemClock
from ebs.core.digest import Digest
from ebs.core.errors import MetadataError
from ebs.meta._common import (
    check_digest,
    check_final_status,
    check_result,
    edge_rows,
    revalidate,
    stored_result,
    touch_cutoff,
    transition,
)
from ebs.meta.api import (
    ActionRow,
    ActionState,
    BuildCreate,
    BuildId,
    BuildStatus,
    BuildView,
    Event,
    ResultManifest,
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
                self._blobs.setdefault(k, blob)
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

    # --- access tracking, events ----------------------------------------------------------------

    def touch(self, domain: str, digests: Iterable[Digest]) -> None:
        check_domain(domain)
        keys = [str(check_digest(d, "touch digest")) for d in digests]
        with self._lock:
            now = self._clock.now()
            for k in keys:
                blob = self._blobs.get((domain, k))
                if blob is not None and blob.last_access <= touch_cutoff(now):
                    blob.last_access = now

    def emit(self, event: Event) -> None:
        checked = revalidate(event, Event, "emit")
        with self._lock:
            self._build(checked.build)
            self._events.append(checked)
