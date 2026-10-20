"""MetadataStore contract suite (docs/design/interfaces.md § 6).

Every implementation must pass it: `memory` runs as a unit test, `pg` as an integration test.
`http` (P1-06 R1) is `HttpMetadataStore` talking to the FastAPI service in-process over a memory
backing store (unit); `http-pg` serves the PostgreSQL store (integration). Their `Inspector` is the
backing store's. The inspector reads state the protocol does not expose (hit counters, edges) and
injects faults.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol, get_args
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import URL

from ebs.core.clock import FakeClock
from ebs.core.digest import Digest, hash_bytes
from ebs.core.errors import MetadataError
from ebs.meta.api import (
    ActionRow,
    ActionState,
    BlobRef,
    BuildCreate,
    BuildId,
    Event,
    InfraReason,
    MetadataBackend,
    OutputResult,
    PilotSlot,
    QueueItem,
    ResourceUsage,
    ResultManifest,
    RunnerInfo,
    ToolchainCreate,
)
from ebs.meta.memory import InMemoryMetadataStore
from ebs.meta.pg import PgMetadataStore
from ebs.toolchain.model import toolchain_id
from tests.helpers.meta_http import make_service
from tests.helpers.pg import url_string

Edge = tuple[str, str, str, str, str | None]  # action_key, direction, logical_path, id, content


class Inspector(Protocol):
    def cache_stats(self, domain: str, key: Digest) -> tuple[int, datetime] | None: ...
    def cache_count(self) -> int: ...
    def set_raw_cache_result(self, domain: str, key: Digest, payload: dict[str, Any]) -> None: ...
    def edges(self, domain: str) -> set[Edge]: ...
    def blob_last_access(self, domain: str, digest: Digest) -> datetime | None: ...
    def event_types(self, build: BuildId) -> list[tuple[str, str | None]]: ...
    def fail_edge_writes(self) -> AbstractContextManager[None]: ...
    def leased(self, domain: str, now: datetime) -> set[Digest]: ...
    def pinned(self, domain: str) -> set[Digest]: ...
    def blob_size(self, domain: str, digest: Digest) -> int | None: ...


@dataclass
class Harness:
    store: MetadataBackend
    clock: FakeClock
    inspect: Inspector


# --- memory -------------------------------------------------------------------------------------


class MemoryInspector:
    def __init__(self, store: InMemoryMetadataStore, monkeypatch: pytest.MonkeyPatch) -> None:
        self._s = store
        self._mp = monkeypatch

    def cache_stats(self, domain: str, key: Digest) -> tuple[int, datetime] | None:
        entry = self._s._cache.get((domain, str(key)))
        return None if entry is None else (entry.hits, entry.last_access)

    def cache_count(self) -> int:
        return len(self._s._cache)

    def set_raw_cache_result(self, domain: str, key: Digest, payload: dict[str, Any]) -> None:
        self._s._cache[(domain, str(key))].result = payload

    def edges(self, domain: str) -> set[Edge]:
        return {(k[1], k[2], k[3], v[0], v[1]) for k, v in self._s._edges.items() if k[0] == domain}

    def blob_last_access(self, domain: str, digest: Digest) -> datetime | None:
        blob = self._s._blobs.get((domain, str(digest)))
        return None if blob is None else blob.last_access

    def event_types(self, build: BuildId) -> list[tuple[str, str | None]]:
        return [(e.event.type, e.event.action_id) for e in self._s.events_after(0, build=build)]

    def leased(self, domain: str, now: datetime) -> set[Digest]:
        return set(self._s.gc_roots(domain, now).leased)

    def pinned(self, domain: str) -> set[Digest]:
        return {
            self._s._builds[b].view.plan_digest
            for b in self._s._pins
            if self._s._builds[b].view.domain == domain
        }

    def blob_size(self, domain: str, digest: Digest) -> int | None:
        blob = self._s._blobs.get((domain, str(digest)))
        return None if blob is None else blob.size

    @contextmanager
    def fail_edge_writes(self) -> Iterator[None]:
        def boom(*args: object) -> None:
            raise MetadataError("injected edge write failure")

        with self._mp.context() as mp:
            mp.setattr(self._s, "_write_edges", boom)
            yield


# --- pg -----------------------------------------------------------------------------------------


class PgInspector:
    def __init__(self, url: URL) -> None:
        self._engine = sa.create_engine(url, poolclass=sa.pool.NullPool)

    def _rows(self, sql: str, **params: object) -> list[tuple[Any, ...]]:
        with self._engine.begin() as conn:
            return [tuple(r) for r in conn.execute(sa.text(sql), params)]

    def cache_stats(self, domain: str, key: Digest) -> tuple[int, datetime] | None:
        rows = self._rows(
            "SELECT hits, last_access FROM ebs.action_cache WHERE domain = :d AND key = :k",
            d=domain,
            k=str(key),
        )
        return None if not rows else (rows[0][0], rows[0][1])

    def cache_count(self) -> int:
        return int(self._rows("SELECT count(*) FROM ebs.action_cache")[0][0])

    def set_raw_cache_result(self, domain: str, key: Digest, payload: dict[str, Any]) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE ebs.action_cache SET result = CAST(:p AS jsonb) "
                    "WHERE domain = :d AND key = :k"
                ),
                {"p": json.dumps(payload), "d": domain, "k": str(key)},
            )

    def edges(self, domain: str) -> set[Edge]:
        rows = self._rows(
            "SELECT action_key, direction, logical_path, object_id, content_digest "
            "FROM ebs.provenance_edges WHERE domain = :d",
            d=domain,
        )
        return {(r[0], r[1], r[2], r[3], r[4]) for r in rows}

    def blob_last_access(self, domain: str, digest: Digest) -> datetime | None:
        rows = self._rows(
            "SELECT last_access FROM ebs.blobs WHERE domain = :d AND digest = :g",
            d=domain,
            g=str(digest),
        )
        return None if not rows else rows[0][0]

    def event_types(self, build: BuildId) -> list[tuple[str, str | None]]:
        rows = self._rows(
            "SELECT type, action_id FROM ebs.events WHERE build_id = :b ORDER BY id", b=build
        )
        return [(r[0], r[1]) for r in rows]

    def leased(self, domain: str, now: datetime) -> set[Digest]:
        rows = self._rows(
            "SELECT o.digest FROM ebs.lease_objects o JOIN ebs.leases l ON l.id = o.lease_id "
            "WHERE l.domain = :d AND l.expires_at > :now",
            d=domain,
            now=now,
        )
        return {Digest.parse(r[0]) for r in rows}

    def pinned(self, domain: str) -> set[Digest]:
        rows = self._rows(
            "SELECT b.plan_digest FROM ebs.pins p JOIN ebs.builds b ON b.id = p.build_id "
            "WHERE p.domain = :d",
            d=domain,
        )
        return {Digest.parse(r[0]) for r in rows}

    def blob_size(self, domain: str, digest: Digest) -> int | None:
        rows = self._rows(
            "SELECT size FROM ebs.blobs WHERE domain = :d AND digest = :g", d=domain, g=str(digest)
        )
        return None if not rows else int(rows[0][0])

    @contextmanager
    def fail_edge_writes(self) -> Iterator[None]:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "CREATE FUNCTION ebs.test_fail_edges() RETURNS trigger LANGUAGE plpgsql AS "
                    "$$ BEGIN RAISE EXCEPTION 'injected edge write failure'; END $$"
                )
            )
            conn.execute(
                sa.text(
                    "CREATE TRIGGER test_fail_edges BEFORE INSERT ON ebs.provenance_edges "
                    "FOR EACH ROW EXECUTE FUNCTION ebs.test_fail_edges()"
                )
            )
        try:
            yield
        finally:
            with self._engine.begin() as conn:
                conn.execute(sa.text("DROP TRIGGER test_fail_edges ON ebs.provenance_edges"))
                conn.execute(sa.text("DROP FUNCTION ebs.test_fail_edges()"))

    def close(self) -> None:
        self._engine.dispose()


BACKENDS = [
    pytest.param("memory", id="memory"),
    pytest.param("pg", id="pg", marks=pytest.mark.integration),
    pytest.param("http", id="http"),  # R1 (P1-06)
    pytest.param("http-pg", id="http-pg", marks=pytest.mark.integration),  # R1 (P1-06)
]


@pytest.fixture(params=BACKENDS)
def h(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    clock = FakeClock()
    backend: str = request.param
    if backend in ("memory", "http"):
        mem = InMemoryMetadataStore(clock=clock)
        harness = Harness(mem, clock, MemoryInspector(mem, monkeypatch))
        if backend == "memory":
            yield harness
        else:
            yield _over_http(harness)
        return
    url: URL = request.getfixturevalue("pg_url")
    pg = PgMetadataStore.from_config({"url": url_string(url)}, clock=clock)
    inspector = PgInspector(url)
    try:
        harness = Harness(pg, clock, inspector)
        yield harness if backend == "pg" else _over_http(harness)
    finally:
        pg.close()
        inspector.close()


def _over_http(backing: Harness) -> Harness:
    """The same harness with the store replaced by an HTTP client of a service over it."""
    # alice registers the toolchains of the P1-07 tests: make her a toolchain admin.
    store = backing.store
    assert isinstance(store, InMemoryMetadataStore | PgMetadataStore)  # the service's backends
    service = make_service(store, toolchain_admins=("alice",))
    return Harness(service.store(), backing.clock, backing.inspect)


@pytest.fixture
def pg_store(pg_url: URL) -> Iterator[PgMetadataStore]:
    store = PgMetadataStore.from_config({"url": url_string(pg_url)}, clock=FakeClock())
    yield store
    store.close()


# --- sample data --------------------------------------------------------------------------------


def dg(label: str) -> Digest:
    return hash_bytes(label.encode())


def make_result(
    key: Digest,
    *,
    status: str = "passed",
    inputs: dict[str, Digest] | None = None,
    outputs: dict[str, OutputResult] | None = None,
    exit_code: int = 0,
) -> ResultManifest:
    return ResultManifest(
        action_key=key,
        status=status,  # type: ignore[arg-type]
        exit_code=exit_code,
        inputs=inputs if inputs is not None else {"src/a.sv": dg("a.sv")},
        outputs=outputs
        if outputs is not None
        else {"report": OutputResult(digest=dg("rep"), id=dg("rep"), type="file", size=12)},
        log=dg("log"),
        summary={"errors": 0, "first_error": ""},
        resources=ResourceUsage(max_rss_kb=1024, cpu_s=3, wall_s=4),
        runner=RunnerInfo(version="0.1.0", host="node-a", slurm_job_id=None),
    )


def make_build(domain: str = "test") -> BuildCreate:
    return BuildCreate(
        uuid=uuid4(),
        domain=domain,
        project="demo",
        plan_digest=dg("plan"),
        flow_repo="git@example.invalid:demo/flow.git",
        flow_commit="0" * 40,
        flow_dirty=False,
        user_name="alice",
        ci_job=None,
        cache_mode="write",
    )


def new_build(h: Harness, *action_ids: str, domain: str = "test") -> BuildId:
    build = h.store.create_build(make_build(domain))
    h.store.add_actions(build, [ActionRow(action_id=a, step="lint") for a in action_ids])
    return build


def action(h: Harness, build: BuildId, action_id: str) -> ActionRow:
    (row,) = [a for a in h.store.list_actions(build) if a.action_id == action_id]
    return row


# --- R2: cache_put ------------------------------------------------------------------------------


# R2
def test_cache_put_idempotent(h: Harness) -> None:
    key = dg("k1")
    first = make_result(key)
    assert h.store.cache_put("test", key, first) is True
    assert h.store.cache_put("test", key, make_result(key, status="failed", exit_code=1)) is False
    assert h.store.cache_get("test", key) == first
    assert h.inspect.cache_count() == 1


# P0-15: --rerun-failed in `write` mode replaces a cached test failure with the rerun's result.
def test_cache_replace_failed(h: Harness) -> None:
    key = dg("k1")
    failed = make_result(key, status="failed", exit_code=1)
    h.store.cache_put("test", key, failed)
    rerun = make_result(key)
    h.clock.advance(10)
    assert h.store.cache_replace_failed("test", key, rerun) is True
    assert h.store.cache_get("test", key) == rerun
    assert h.inspect.cache_count() == 1
    # A failing rerun may replace a failure too (fresh log and summary).
    other = dg("k2")
    h.store.cache_put("test", other, make_result(other, status="failed", exit_code=1))
    again = make_result(other, status="failed", exit_code=2)
    assert h.store.cache_replace_failed("test", other, again) is True
    assert h.store.cache_get("test", other) == again


# P0-15: a passing entry is never replaced, and nothing is inserted where nothing was cached.
def test_cache_replace_failed_leaves_passed_and_absent_alone(h: Harness) -> None:
    key = dg("k1")
    passed = make_result(key)
    h.store.cache_put("test", key, passed)
    assert h.store.cache_replace_failed("test", key, make_result(key, status="failed")) is False
    assert h.store.cache_get("test", key) == passed
    absent = dg("never")
    assert h.store.cache_replace_failed("test", absent, make_result(absent)) is False
    assert h.store.cache_get("test", absent) is None


# P0-15 / I13: replacing is scoped by domain and checks the manifest's key.
def test_cache_replace_failed_scoped_and_checked(h: Harness) -> None:
    key = dg("k1")
    h.store.cache_put("nda", key, make_result(key, status="failed", exit_code=1))
    assert h.store.cache_replace_failed("test", key, make_result(key)) is False
    assert h.store.cache_get("nda", key) == make_result(key, status="failed", exit_code=1)
    with pytest.raises(MetadataError, match="action key"):
        h.store.cache_replace_failed("nda", key, make_result(dg("other")))
    with pytest.raises(MetadataError, match="domain"):
        h.store.cache_replace_failed("no/such", key, make_result(key))


# R2
def test_cache_put_concurrent(h: Harness) -> None:
    key = dg("race")
    barrier = threading.Barrier(10)
    outcomes: list[bool] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        try:
            barrier.wait(timeout=10)
            inserted = h.store.cache_put("test", key, make_result(key, exit_code=i))
            with lock:
                outcomes.append(inserted)
        except BaseException as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == []
    assert sorted(outcomes) == [False] * 9 + [True]
    assert h.inspect.cache_count() == 1


def test_cache_get_miss_returns_none(h: Harness) -> None:
    assert h.store.cache_get("test", dg("absent")) is None


# --- R3: cache_get ------------------------------------------------------------------------------


# R3
def test_cache_get_touch_throttled(h: Harness) -> None:
    key = dg("k")
    t0 = h.clock.now()
    h.store.cache_put("test", key, make_result(key))
    assert h.inspect.cache_stats("test", key) == (0, t0)

    h.clock.advance(59 * 60)
    got = h.store.cache_get("test", key)
    assert isinstance(got, ResultManifest)
    assert got == make_result(key)
    assert h.inspect.cache_stats("test", key) == (1, t0)  # within the hour: no touch

    h.clock.advance(2 * 60)
    t1 = h.clock.now()
    h.store.cache_get("test", key)
    assert h.inspect.cache_stats("test", key) == (2, t1)  # > 1 h since last touch

    h.clock.advance(30 * 60)
    h.store.cache_get("test", key)
    assert h.inspect.cache_stats("test", key) == (3, t1)


# `cache_peek` (`ebs plan`): the same answer as cache_get, scoped by domain, but no hit is
# counted and last_access is not touched.
def test_cache_peek_has_no_side_effects(h: Harness) -> None:
    key = dg("k")
    t0 = h.clock.now()
    h.store.cache_put("test", key, make_result(key))
    h.clock.advance(2 * 3600)
    assert h.store.cache_peek("test", key) == make_result(key)
    assert h.store.cache_peek("test", dg("absent")) is None
    assert h.store.cache_peek("other", key) is None
    assert h.inspect.cache_stats("test", key) == (0, t0)
    h.inspect.set_raw_cache_result("test", key, {"v": 1, "action_key": "nope"})
    with pytest.raises(MetadataError, match="action cache entry"):
        h.store.cache_peek("test", key)


# R3
def test_cache_get_rejects_invalid_stored_result(h: Harness) -> None:
    key = dg("k")
    h.store.cache_put("test", key, make_result(key))
    h.inspect.set_raw_cache_result("test", key, {"v": 1, "action_key": "nope"})
    with pytest.raises(MetadataError, match="action cache entry"):
        h.store.cache_get("test", key)


# --- R4 / I13 -----------------------------------------------------------------------------------


# R4 (I13)
def test_domain_scoping(h: Harness) -> None:
    key = dg("shared-key")
    a_result = make_result(key)
    assert h.store.cache_put("alpha", key, a_result) is True
    assert h.store.cache_get("beta", key) is None
    assert h.inspect.cache_stats("beta", key) is None
    b_result = make_result(key, status="failed", exit_code=3)
    assert h.store.cache_put("beta", key, b_result) is True  # separate entry, not a duplicate
    assert h.store.cache_get("alpha", key) == a_result
    assert h.store.cache_get("beta", key) == b_result


def test_invalid_domain_rejected(h: Harness) -> None:
    with pytest.raises(MetadataError, match="domain"):
        h.store.cache_get("../escape", dg("k"))


# --- R5: record_result --------------------------------------------------------------------------


def _nd_result(key: Digest) -> ResultManifest:
    return make_result(
        key,
        inputs={"rtl/top.sv": dg("top"), "lib": dg("libtree")},
        outputs={
            "report": OutputResult(digest=dg("rep"), id=dg("rep"), type="file", size=10),
            "worklib": OutputResult(digest=dg("wl-bytes"), id=dg("wl-nd-id"), type="tree", size=99),
        },
    )


# R5
def test_record_result_writes_row_and_edges(h: Harness) -> None:
    build = new_build(h, "compile[lib=core]", domain="nda")
    key = dg("key")
    h.store.record_result(build, "compile[lib=core]", _nd_result(key))

    row = action(h, build, "compile[lib=core]")
    assert row.key == key
    assert row.result_key == key
    k = str(key)
    assert h.inspect.edges("nda") == {
        (k, "in", "lib", str(dg("libtree")), None),
        (k, "in", "rtl/top.sv", str(dg("top")), None),
        (k, "out", "report", str(dg("rep")), str(dg("rep"))),
        (k, "out", "worklib", str(dg("wl-nd-id")), str(dg("wl-bytes"))),
    }
    assert h.inspect.edges("test") == set()
    assert h.inspect.blob_last_access("nda", dg("wl-bytes")) == h.clock.now()


# R5
def test_record_result_edges_atomic(h: Harness) -> None:
    build = new_build(h, "a")
    key = dg("key")
    with h.inspect.fail_edge_writes(), pytest.raises(MetadataError):
        h.store.record_result(build, "a", _nd_result(key))
    row = action(h, build, "a")
    assert row.key is None
    assert row.result_key is None
    assert h.inspect.edges("test") == set()
    assert h.inspect.blob_last_access("test", dg("wl-bytes")) is None


# R5
def test_record_result_is_idempotent(h: Harness) -> None:
    build = new_build(h, "a")
    key = dg("key")
    h.store.record_result(build, "a", _nd_result(key))
    h.store.record_result(build, "a", _nd_result(key))  # client retry (P1-06 R5)
    assert len(h.inspect.edges("test")) == 4


# R5
def test_record_result_key_mismatch(h: Harness) -> None:
    build = h.store.create_build(make_build())
    h.store.add_actions(build, [ActionRow(action_id="a", step="lint", key=dg("planned"))])
    with pytest.raises(MetadataError, match="action key"):
        h.store.record_result(build, "a", make_result(dg("other")))
    assert h.inspect.edges("test") == set()


# P0-13: the runner stages a nondeterministic input by resolving its id to the stored bytes.
def test_resolve_output(h: Harness) -> None:
    build = new_build(h, "compile[lib=core]", domain="nda")
    h.store.record_result(build, "compile[lib=core]", _nd_result(dg("key")))
    assert h.store.resolve_output("nda", dg("wl-nd-id")) == dg("wl-bytes")
    assert h.store.resolve_output("nda", dg("rep")) == dg("rep")  # deterministic: id is content
    assert h.store.resolve_output("nda", dg("libtree")) is None  # an input id, not an output
    assert h.store.resolve_output("nda", dg("unknown")) is None
    assert h.store.resolve_output("test", dg("wl-nd-id")) is None  # scoped by domain (I13)


def test_resolve_output_first_writer_wins(h: Harness) -> None:
    # A rerun of the same nondeterministic producer (cache off) records other bytes for the same
    # id; consumers keep seeing the first run's bytes.
    key = dg("key")
    first, second = new_build(h, "a"), new_build(h, "a")
    h.store.record_result(first, "a", _nd_result(key))
    rerun = _nd_result(key).model_copy(
        update={
            "outputs": {
                "worklib": OutputResult(
                    digest=dg("other-bytes"), id=dg("wl-nd-id"), type="tree", size=1
                )
            }
        }
    )
    h.store.record_result(second, "a", rerun)
    assert h.store.resolve_output("test", dg("wl-nd-id")) == dg("wl-bytes")


def test_resolve_output_invalid_domain(h: Harness) -> None:
    with pytest.raises(MetadataError, match="domain"):
        h.store.resolve_output("../escape", dg("k"))


def test_record_result_unknown_action(h: Harness) -> None:
    build = new_build(h, "a")
    with pytest.raises(MetadataError, match="no action 'b'"):
        h.store.record_result(build, "b", make_result(dg("k")))


# --- R7: state machine --------------------------------------------------------------------------

STATES: tuple[ActionState, ...] = (
    "queued",
    "pending",
    "running",
    "done",
    "failed",
    "infra_failed",
    "cached",
    "cancelled",
    "skipped",
)
# Written independently of the implementation's table on purpose.
LEGAL: dict[str, set[str]] = {
    "queued": {"pending", "running", "cached", "cancelled", "infra_failed", "skipped"},
    "pending": {"pending", "running", "infra_failed", "cancelled"},
    "running": {"done", "failed", "infra_failed", "cancelled"},
    "infra_failed": {"queued"},
    "done": set(),
    "failed": set(),
    "cached": set(),
    "cancelled": set(),
    "skipped": set(),  # P0-15: blocked by a failed dependency, or never started after a failure
}
PATH_TO: dict[str, list[ActionState]] = {
    "queued": [],
    "pending": ["pending"],
    "running": ["running"],
    "done": ["running", "done"],
    "failed": ["running", "failed"],
    "infra_failed": ["running", "infra_failed"],
    "cached": ["cached"],
    "cancelled": ["cancelled"],
    "skipped": ["skipped"],
}


# R7
@pytest.mark.parametrize("dst", STATES)
@pytest.mark.parametrize("src", STATES)
def test_state_machine(h: Harness, src: ActionState, dst: ActionState) -> None:
    build = new_build(h, "a")
    for step in PATH_TO[src]:
        h.store.set_action_state(build, "a", step)
    assert action(h, build, "a").state == src
    if dst in LEGAL[src]:
        h.store.set_action_state(build, "a", dst)
        assert action(h, build, "a").state == dst
    else:
        with pytest.raises(MetadataError, match=f"{src} -> {dst}"):
            h.store.set_action_state(build, "a", dst)
        assert action(h, build, "a").state == src


# R7
def test_retry_cycle_counts_attempts_and_stamps_times(h: Harness) -> None:
    build = new_build(h, "a")
    t0 = h.clock.now()
    assert action(h, build, "a").queued_at == t0
    h.store.set_action_state(build, "a", "pending", pending_reason="licenses", slurm_job_id="17")
    row = action(h, build, "a")
    assert (row.pending_reason, row.slurm_job_id) == ("licenses", "17")
    h.clock.advance(5)
    h.store.set_action_state(build, "a", "running")
    row = action(h, build, "a")
    assert (row.attempts, row.started_at, row.pending_reason) == (1, h.clock.now(), None)
    h.clock.advance(5)
    h.store.set_action_state(build, "a", "infra_failed", infra_reason="oom")
    row = action(h, build, "a")
    assert (row.infra_reason, row.finished_at) == ("oom", h.clock.now())
    h.clock.advance(5)
    h.store.set_action_state(build, "a", "queued")
    row = action(h, build, "a")
    assert row.queued_at == h.clock.now()
    assert (row.started_at, row.finished_at, row.infra_reason, row.slurm_job_id) == (
        None,
        None,
        None,
        None,
    )
    h.store.set_action_state(build, "a", "running")
    h.store.set_action_state(build, "a", "done")
    assert action(h, build, "a").attempts == 2


# P0-14: every executor infra reason, including runner exit 76's `input_verification`, is storable.
@pytest.mark.parametrize("reason", get_args(InfraReason))
def test_every_infra_reason_is_storable(h: Harness, reason: str) -> None:
    build = new_build(h, "a")
    h.store.set_action_state(build, "a", "running")
    h.store.set_action_state(build, "a", "infra_failed", infra_reason=reason)
    assert action(h, build, "a").infra_reason == reason


# P0-15: the driver reads a finished action's manifest back, in every cache mode.
def test_get_result_returns_recorded_manifest(h: Harness) -> None:
    build = new_build(h, "a", "b")
    key = dg("key")
    assert h.store.get_result(build, "a") is None
    result = make_result(key, status="failed", exit_code=3)
    h.store.record_result(build, "a", result)
    assert h.store.get_result(build, "a") == result
    assert h.store.get_result(build, "b") is None  # per action, not per build


# P0-15
def test_get_result_latest_record_wins(h: Harness) -> None:
    build = new_build(h, "a")
    key = dg("key")
    h.store.record_result(build, "a", make_result(key, status="failed", exit_code=1))
    second = make_result(key)  # e.g. the runner posted again after a retry
    h.store.record_result(build, "a", second)
    assert h.store.get_result(build, "a") == second


# P0-15
def test_get_result_is_scoped_to_its_build(h: Harness) -> None:
    first = new_build(h, "a")
    second = new_build(h, "a")
    h.store.record_result(first, "a", make_result(dg("key")))
    assert h.store.get_result(second, "a") is None


# P0-15
def test_get_result_unknown_action_or_build(h: Harness) -> None:
    build = new_build(h, "a")
    with pytest.raises(MetadataError, match="no action 'b'"):
        h.store.get_result(build, "b")
    with pytest.raises(MetadataError):
        h.store.get_result(BuildId(build + 1000), "a")


# P0-15
def test_skipped_state_is_final_and_counted(h: Harness) -> None:
    build = new_build(h, "a", "b")
    h.store.set_action_state(build, "a", "skipped")
    assert action(h, build, "a").state == "skipped"
    assert h.store.get_build(build).action_counts == {"queued": 1, "skipped": 1}


def test_cached_state_sets_flag_and_result_key(h: Harness) -> None:
    build = new_build(h, "a")
    h.store.set_action_state(build, "a", "cached", key=dg("k"), result_key=dg("k"))
    row = action(h, build, "a")
    assert (row.cached, row.key, row.result_key) == (True, dg("k"), dg("k"))


@pytest.mark.parametrize(
    ("state", "fields", "match"),
    [
        ("running", {"colour": "red"}, "colour"),
        ("pending", {"pending_reason": "coffee"}, "pending_reason"),
        ("running", {"pending_reason": "licenses"}, "pending_reason"),
        ("pending", {"infra_reason": "oom"}, "infra_reason"),
        ("pending", {"slurm_job_id": 17}, "slurm_job_id"),
        ("cached", {"key": "sha256:xyz"}, "key"),
        ("sleeping", {}, "sleeping"),
    ],
)
def test_set_action_state_validates_fields(
    h: Harness, state: str, fields: dict[str, object], match: str
) -> None:
    build = new_build(h, "a")
    with pytest.raises(MetadataError, match=match):
        h.store.set_action_state(build, "a", state, **fields)  # type: ignore[arg-type]
    assert action(h, build, "a").state == "queued"


def test_set_action_state_refuses_key_change(h: Harness) -> None:
    build = new_build(h, "a")
    h.store.set_action_state(build, "a", "pending", key=dg("k1"))
    h.store.set_action_state(build, "a", "pending", key=dg("k1"))  # same key: fine
    with pytest.raises(MetadataError, match="action key"):
        h.store.set_action_state(build, "a", "running", key=dg("k2"))
    row = action(h, build, "a")
    assert (row.state, row.key) == ("pending", dg("k1"))


def test_set_action_state_unknown_action(h: Harness) -> None:
    build = new_build(h, "a")
    with pytest.raises(MetadataError, match="no action 'zzz'"):
        h.store.set_action_state(build, "zzz", "running")


# --- builds, actions, events, touch -------------------------------------------------------------


def test_build_lifecycle(h: Harness) -> None:
    spec = make_build()
    build = h.store.create_build(spec)
    h.store.add_actions(
        build, [ActionRow(action_id=a, step="sim") for a in ("s[2]", "s[1]", "s[3]")]
    )
    h.store.set_action_state(build, "s[1]", "cached")
    view = h.store.get_build(build)
    assert view.id == build
    assert (view.uuid, view.domain, view.plan_digest, view.cache_mode) == (
        spec.uuid,
        "test",
        spec.plan_digest,
        "write",
    )
    assert (view.status, view.created_at, view.finished_at, view.pinned) == (
        "running",
        h.clock.now(),
        None,
        False,
    )
    assert view.action_counts == {"queued": 2, "cached": 1}
    assert [a.action_id for a in h.store.list_actions(build)] == ["s[1]", "s[2]", "s[3]"]
    assert [a.action_id for a in h.store.list_actions(build, state="cached")] == ["s[1]"]

    h.clock.advance(60)
    h.store.finish_build(build, "passed")
    view = h.store.get_build(build)
    assert (view.status, view.finished_at) == ("passed", h.clock.now())
    with pytest.raises(MetadataError, match="already finished"):
        h.store.finish_build(build, "failed")
    with pytest.raises(MetadataError, match="finished"):
        h.store.add_actions(build, [ActionRow(action_id="late", step="sim")])


def test_finish_build_rejects_running_status(h: Harness) -> None:
    build = new_build(h)
    with pytest.raises(MetadataError, match="running"):
        h.store.finish_build(build, "running")


def test_unknown_build(h: Harness) -> None:
    missing = BuildId(987654)
    for call in (
        lambda: h.store.get_build(missing),
        lambda: h.store.list_actions(missing),
        lambda: h.store.finish_build(missing, "passed"),
        lambda: h.store.add_actions(missing, [ActionRow(action_id="a", step="x")]),
    ):
        with pytest.raises(MetadataError, match="987654"):
            call()


def test_add_actions_duplicate_rejected_atomically(h: Harness) -> None:
    build = new_build(h, "a")
    with pytest.raises(MetadataError, match="'a'"):
        h.store.add_actions(
            build, [ActionRow(action_id="b", step="x"), ActionRow(action_id="a", step="x")]
        )
    assert [a.action_id for a in h.store.list_actions(build)] == ["a"]
    h.store.add_actions(build, [])


def test_add_actions_concurrent_duplicates(h: Harness) -> None:
    build = new_build(h)
    rows = [ActionRow(action_id=f"sim[{i}]", step="sim") for i in range(2_000)]
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait(timeout=10)
        try:
            h.store.add_actions(build, rows)
            outcome = "ok"
        except MetadataError:
            outcome = "MetadataError"
        except BaseException as exc:
            outcome = f"leaked {type(exc).__name__}"
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(outcomes) == ["MetadataError", "ok"]
    assert h.store.get_build(build).action_counts == {"queued": 2_000}


def test_duplicate_build_uuid_rejected(h: Harness) -> None:
    spec = make_build()
    h.store.create_build(spec)
    with pytest.raises(MetadataError, match=str(spec.uuid)):
        h.store.create_build(spec)


def test_emit_events(h: Harness) -> None:
    build = new_build(h, "a")
    h.store.emit(Event(ts=h.clock.now(), build=build, type="build_started", data={}))
    h.store.emit(
        Event(ts=h.clock.now(), build=build, type="submitted", action_id="a", data={"job": "1"})
    )
    assert h.inspect.event_types(build) == [("build_started", None), ("submitted", "a")]
    with pytest.raises(MetadataError, match="555"):
        h.store.emit(Event(ts=h.clock.now(), build=BuildId(555), type="plan_ready", data={}))


def test_touch_throttled(h: Harness) -> None:
    build = new_build(h, "a")
    h.store.record_result(build, "a", _nd_result(dg("k")))
    t0 = h.clock.now()
    h.clock.advance(30 * 60)
    h.store.touch("test", [dg("rep"), dg("unknown")])
    assert h.inspect.blob_last_access("test", dg("rep")) == t0
    h.clock.advance(31 * 60)
    h.store.touch("test", [dg("rep")])
    assert h.inspect.blob_last_access("test", dg("rep")) == h.clock.now()
    assert h.inspect.blob_last_access("test", dg("wl-bytes")) == t0
    assert h.inspect.blob_last_access("test", dg("unknown")) is None


# --- toolchain registry (P1-07) -----------------------------------------------------------------


def make_toolchain(
    module: str = "questa/2025.2", label: str = "fp1", **env: str
) -> ToolchainCreate:
    version = module.rpartition("/")[2]
    fingerprint = dg(label)
    env = env or {"PATH": "/opt/eda/bin:/usr/bin:/bin"}
    return ToolchainCreate(
        id=toolchain_id(module, version, fingerprint, env),
        name=module.rpartition("/")[0],
        module=module,
        version=version,
        fingerprint=fingerprint,
        env=env,
        install_roots=["/opt/eda"],
        registered_by="alice",
    )


def test_register_toolchain_insert_and_read_back(h: Harness) -> None:
    spec = make_toolchain()
    outcome = h.store.register_toolchain(spec, b"manifest-1")
    assert (outcome.inserted, outcome.superseded) == (True, ())
    row = h.store.get_toolchain(spec.id)
    assert row is not None
    assert row.model_dump(include=set(ToolchainCreate.model_fields)) == spec.model_dump()
    assert (row.registered_at, row.superseded_by, row.last_used_at) == (h.clock.now(), None, None)
    assert h.store.toolchain_manifest(spec.id) == b"manifest-1"
    assert h.store.current_toolchain("questa/2025.2") == row
    assert h.store.get_toolchain(dg("unknown")) is None
    assert h.store.toolchain_manifest(dg("unknown")) is None
    assert h.store.current_toolchain("questa/9.9") is None


def test_register_toolchain_idempotent(h: Harness) -> None:
    spec = make_toolchain()
    h.store.register_toolchain(spec, b"m1")
    row = h.store.get_toolchain(spec.id)
    h.clock.advance(60)
    again = h.store.register_toolchain(spec.model_copy(update={"registered_by": "bob"}), b"m2")
    assert (again.inserted, again.superseded) == (False, ())
    assert h.store.list_toolchains() == [row]
    assert h.store.toolchain_manifest(spec.id) == b"m1"


def test_register_toolchain_supersedes_and_reinstates(h: Harness) -> None:
    a, b = make_toolchain(label="fp-a"), make_toolchain(label="fp-b")
    other = make_toolchain("lint/1.0")
    h.store.register_toolchain(a, b"a")
    h.store.register_toolchain(other, b"o")
    h.clock.advance(60)
    out = h.store.register_toolchain(b, b"b")
    assert (out.inserted, out.superseded) == (True, (a.id,))
    row_a = h.store.get_toolchain(a.id)
    assert row_a is not None
    assert row_a.superseded_by == b.id
    current = h.store.current_toolchain("questa/2025.2")
    assert current is not None
    assert current.id == b.id
    assert [r.id for r in h.store.list_toolchains(module="questa/2025.2")] == [a.id, b.id]
    assert {r.id for r in h.store.list_toolchains(current_only=True)} == {b.id, other.id}

    back = h.store.register_toolchain(a, b"ignored")
    assert (back.inserted, back.superseded) == (False, (b.id,))
    row_a, row_b = h.store.get_toolchain(a.id), h.store.get_toolchain(b.id)
    assert row_a is not None
    assert row_a.superseded_by is None
    assert row_b is not None
    assert row_b.superseded_by == a.id
    assert h.store.toolchain_manifest(a.id) == b"a"


def test_register_toolchain_concurrent_versions(h: Harness) -> None:
    specs = [make_toolchain(label=f"fp{i}") for i in range(6)]
    errors: list[BaseException] = []

    def worker(spec: ToolchainCreate) -> None:
        try:
            h.store.register_toolchain(spec, b"m")
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(s,)) for s in specs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(h.store.list_toolchains(module="questa/2025.2", current_only=True)) == 1


def test_touch_toolchain_throttled(h: Harness) -> None:
    spec = make_toolchain()
    h.store.register_toolchain(spec, b"m")
    h.store.touch_toolchain(spec.id)
    t0 = h.clock.now()
    h.clock.advance(30 * 60)
    h.store.touch_toolchain(spec.id)
    h.store.touch_toolchain(dg("unknown"))  # ignored
    row = h.store.get_toolchain(spec.id)
    assert row is not None
    assert row.last_used_at == t0
    h.clock.advance(31 * 60)
    h.store.touch_toolchain(spec.id)
    row = h.store.get_toolchain(spec.id)
    assert row is not None
    assert row.last_used_at == h.clock.now()


@pytest.mark.parametrize("case", ["bad id", "relative root", "no roots", "bad env name"])
def test_register_toolchain_validates(h: Harness, case: str) -> None:
    spec = make_toolchain()
    cases: dict[str, dict[str, Any]] = {
        "bad id": {"id": "sha256:nothex"},
        "relative root": {"install_roots": ["opt/eda"]},
        "no roots": {"install_roots": []},
        "bad env name": {"env": {"BAD NAME": "x"}},
    }
    bad = cases[case]
    with pytest.raises(MetadataError):
        h.store.register_toolchain(ToolchainCreate.model_construct(**{**dict(spec), **bad}), b"m")
    assert h.store.list_toolchains() == []
    with pytest.raises(MetadataError, match="bytes"):
        h.store.register_toolchain(spec, "text")  # type: ignore[arg-type]


# --- PG only: R6, R9 ----------------------------------------------------------------------------


# R6
@pytest.mark.integration
@pytest.mark.slow
def test_add_actions_bulk(pg_store: PgMetadataStore) -> None:
    build = pg_store.create_build(make_build())
    rows = [
        ActionRow(action_id=f"sim[test=t{i},seed={i}]", step="sim", key=dg(str(i)))
        for i in range(20_000)
    ]
    start = time.perf_counter()
    pg_store.add_actions(build, rows)
    elapsed = time.perf_counter() - start
    assert elapsed < 5.0, f"add_actions(20,000) took {elapsed:.2f} s"
    assert pg_store.get_build(build).action_counts == {"queued": 20_000}


# R9
@pytest.mark.integration
def test_short_transactions(pg_url: URL) -> None:
    clock = FakeClock()
    store = PgMetadataStore.from_config(
        {"url": url_string(pg_url), "pool_size": 2, "max_overflow": 0}, clock=clock
    )
    observer = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    try:
        assert store.pool_size == 2
        key = dg("k")
        store.cache_put("test", key, make_result(key))
        store.cache_get("test", key)
        build = store.create_build(make_build())
        store.add_actions(build, [ActionRow(action_id="a", step="lint")])
        store.set_action_state(build, "a", "running")
        store.record_result(build, "a", make_result(key))
        store.set_action_state(build, "a", "done")
        store.touch("test", [dg("rep")])
        store.emit(Event(ts=clock.now(), build=build, type="finished", action_id="a", data={}))
        store.list_actions(build)
        store.finish_build(build, "passed")
        store.get_build(build)
        with pytest.raises(MetadataError):
            store.set_action_state(build, "a", "running")  # error path must roll back too

        with observer.connect() as conn:
            rows = list(
                conn.execute(
                    sa.text(
                        "SELECT state FROM pg_stat_activity "
                        "WHERE application_name = :app AND datname = current_database()"
                    ),
                    {"app": PgMetadataStore.APPLICATION_NAME},
                )
            )
        states = [r[0] for r in rows]
        assert states, "the store's pooled connections should be visible"
        assert len(states) <= 2
        assert all(s == "idle" for s in states), states
    finally:
        store.close()
        observer.dispose()


# --- R8: validation on write, for every backend --------------------------------------------------


def _unchecked(result: ResultManifest, **fields: object) -> ResultManifest:
    return ResultManifest.model_construct(**{**dict(result), **fields})


def _invalid_write(h: Harness, build: BuildId, case: str) -> None:
    key = dg("k")
    if case == "float in summary":
        h.store.cache_put("test", key, _unchecked(make_result(key), summary={"r": 0.5}))
    elif case == "bad digest string":
        h.store.record_result(build, "a", _unchecked(make_result(key), log="sha256:nothex"))
    elif case == "unknown event type":
        h.store.emit(
            Event.model_construct(
                v=1, ts=h.clock.now(), build=build, type="exploded", action_id=None, data={}
            )
        )
    else:
        h.store.add_actions(
            build, [ActionRow.model_construct(action_id="z", step="x", key="sha256:0")]
        )


# R8
@pytest.mark.parametrize(
    "case", ["float in summary", "bad digest string", "unknown event type", "bad action key"]
)
def test_validation_on_write(h: Harness, case: str) -> None:
    build = new_build(h, "a")
    with pytest.raises(MetadataError):
        _invalid_write(h, build, case)
    assert h.inspect.cache_count() == 0
    assert h.inspect.edges("test") == set()
    assert h.inspect.event_types(build) == []
    assert [a.action_id for a in h.store.list_actions(build)] == ["a"]


# --- GC: blob registration, leases, pins (P1-09) ------------------------------------------------


# P1-09 R1: register_blobs inserts rows and touches known ones with the hourly throttle.
def test_register_blobs(h: Harness) -> None:
    a, b = dg("blob-a"), dg("blob-b")
    h.store.register_blobs("test", [BlobRef(digest=a, size=3, kind="file")])
    t0 = h.clock.now()
    assert h.inspect.blob_last_access("test", a) == t0
    assert h.inspect.blob_size("test", a) == 3
    h.clock.advance(30 * 60)
    h.store.register_blobs(
        "test", [BlobRef(digest=a, size=3, kind="file"), BlobRef(digest=b, size=0, kind="tree")]
    )
    assert h.inspect.blob_last_access("test", a) == t0  # throttled
    assert h.inspect.blob_last_access("test", b) == h.clock.now()
    h.clock.advance(31 * 60)
    h.store.register_blobs("test", [BlobRef(digest=a, size=3, kind="file")])
    assert h.inspect.blob_last_access("test", a) == h.clock.now()
    assert h.inspect.blob_last_access("other", a) is None  # scoped by domain (I13)
    h.store.register_blobs("test", [])
    with pytest.raises(MetadataError):
        h.store.register_blobs("bad domain", [BlobRef(digest=a, size=3, kind="file")])


# P1-09 R2: a lease protects its digests until it expires; renewals extend and add; a finished
# build's leases are dropped.
def test_leases(h: Harness) -> None:
    build = new_build(h, "a")
    first = [dg(f"in-{i}") for i in range(3)]
    lease = h.store.acquire_lease("test", build, first, 900)
    assert h.inspect.leased("test", h.clock.now()) == set(first)
    assert h.inspect.leased("other", h.clock.now()) == set()
    h.clock.advance(600)
    h.store.renew_lease(build, lease, 900, add=[dg("out")])
    h.clock.advance(600)  # past the first expiry
    assert h.inspect.leased("test", h.clock.now()) == {*first, dg("out")}
    h.store.renew_lease(build, lease, 900)
    h.clock.advance(901)
    assert h.inspect.leased("test", h.clock.now()) == set()
    with pytest.raises(MetadataError, match="no live lease"):
        h.store.renew_lease(build, lease, 900)
    other = new_build(h, "b")
    again = h.store.acquire_lease("test", build, first, 900)
    with pytest.raises(MetadataError, match="no live lease"):
        h.store.renew_lease(other, again, 900)
    with pytest.raises(MetadataError, match="domain"):
        h.store.acquire_lease("elsewhere", other, first, 900)
    for bad in (0, 86_401):
        with pytest.raises(MetadataError, match="ttl_s"):
            h.store.acquire_lease("test", other, first, bad)
    h.store.finish_build(build, "passed")
    assert h.inspect.leased("test", h.clock.now()) == set()
    with pytest.raises(MetadataError, match="finished"):
        h.store.acquire_lease("test", build, first, 900)


# P2-08 R3: one live driver per build. A claim succeeds when the build has no driver lease, its
# lease expired, or the claimant already holds it (a renewal); otherwise the holder is returned.
def test_driver_lease(h: Harness) -> None:
    build = new_build(h, "a")
    a = h.store.claim_driver(build, "host-a:1:tok-a", 120)
    assert (a.owner, a.expires_at) == ("host-a:1:tok-a", h.clock.now() + timedelta(seconds=120))
    h.clock.advance(60)
    refused = h.store.claim_driver(build, "host-b:2:tok-b", 120)
    assert refused.owner == "host-a:1:tok-a"  # still A's: B must not drive
    assert refused.expires_at == a.expires_at
    renewed = h.store.claim_driver(build, "host-a:1:tok-a", 120)
    assert renewed.expires_at == h.clock.now() + timedelta(seconds=120)
    h.clock.advance(121)  # A stopped heartbeating
    taken = h.store.claim_driver(build, "host-b:2:tok-b", 120)
    assert taken.owner == "host-b:2:tok-b"
    assert h.store.claim_driver(build, "host-a:1:tok-a", 120).owner == "host-b:2:tok-b"
    h.store.release_driver(build, "host-a:1:tok-a")  # not A's any more: no effect
    assert h.store.claim_driver(build, "host-c:3:tok-c", 120).owner == "host-b:2:tok-b"
    h.store.release_driver(build, "host-b:2:tok-b")
    assert h.store.claim_driver(build, "host-c:3:tok-c", 120).owner == "host-c:3:tok-c"
    other = new_build(h, "b")  # leases are per build
    assert h.store.claim_driver(other, "host-a:1:tok-a", 120).owner == "host-a:1:tok-a"
    third = new_build(h, "c")
    assert h.store.claim_driver(third, "host-a:1:tok-a", 120).owner == "host-a:1:tok-a"
    h.store.release_driver(third, "host-a:1:tok-a")  # releases that build's lease only
    assert h.store.claim_driver(other, "host-d:4:tok-d", 120).owner == "host-a:1:tok-a"
    h.clock.advance(120)  # exactly the TTL: expired
    assert h.store.claim_driver(other, "host-d:4:tok-d", 120).owner == "host-d:4:tok-d"
    for bad in (0, 86_401):
        with pytest.raises(MetadataError, match="ttl_s"):
            h.store.claim_driver(other, "host-a:1:tok-a", bad)
    with pytest.raises(MetadataError):
        h.store.claim_driver(other, "", 120)
    with pytest.raises(MetadataError):
        h.store.claim_driver(BuildId(999_999), "host-a:1:tok-a", 120)
    h.store.finish_build(build, "passed")
    with pytest.raises(MetadataError, match="finished"):
        h.store.claim_driver(build, "host-c:3:tok-c", 120)
    h.store.release_driver(build, "host-c:3:tok-c")  # releasing after the end is harmless


# P1-09 R2: a lease may hold more digests than one HTTP batch.
def test_large_lease(h: Harness) -> None:
    build = new_build(h, "a")
    many = [dg(f"src-{i}") for i in range(1200)]
    lease = h.store.acquire_lease("test", build, many[:700], 900)
    h.store.renew_lease(build, lease, 900, add=many[700:])
    assert h.inspect.leased("test", h.clock.now()) == set(many)


# P1-09 R2: pins are idempotent and visible on the build; unpin says whether it was pinned.
def test_pin_unpin(h: Harness) -> None:
    build = new_build(h, "a")
    h.store.pin_build(build, by="alice", reason="sign-off")
    h.store.pin_build(build, by="alice")
    assert h.store.get_build(build).pinned is True
    assert h.inspect.pinned("test") == {dg("plan")}
    assert h.store.unpin_build(build) is True
    assert h.store.unpin_build(build) is False
    assert h.store.get_build(build).pinned is False
    assert h.inspect.pinned("test") == set()
    with pytest.raises(MetadataError):
        h.store.pin_build(BuildId(999_999), by="alice")
    with pytest.raises(MetadataError):
        h.store.unpin_build(BuildId(999_999))
    with pytest.raises(MetadataError):
        h.store.pin_build(build, by="")


# P1-09 R2 (I15): taking a lease, adding to it and pinning are uses of the objects they protect:
# they touch the blob rows, so a GC claim racing with them fails.
def test_leases_and_pins_touch(h: Harness) -> None:
    leased, added = dg("leased"), dg("added")
    h.store.register_blobs(
        "test", [BlobRef(digest=d, size=1, kind="file") for d in (leased, added, dg("plan"))]
    )
    build = new_build(h, "a")
    h.store.record_result(build, "a", make_result(dg("k")))  # registers its output `rep`
    h.clock.advance(2 * 3600)
    lease = h.store.acquire_lease("test", build, [leased], 900)
    assert h.inspect.blob_last_access("test", leased) == h.clock.now()
    assert h.inspect.blob_last_access("test", added) != h.clock.now()
    h.store.renew_lease(build, lease, 900, add=[added])
    assert h.inspect.blob_last_access("test", added) == h.clock.now()
    h.clock.advance(2 * 3600)
    h.store.pin_build(build, by="alice")
    assert h.inspect.blob_last_access("test", dg("plan")) == h.clock.now()
    assert h.inspect.blob_last_access("test", dg("rep")) == h.clock.now()


# P1-09 follow-up: the final plan is recorded with the build, and a pin touches it like the
# plan (the GC roots are in test_gc_store.py).
def test_finish_build_records_final_plan(h: Harness) -> None:
    final = dg("final plan")
    h.store.register_blobs(
        "test", [BlobRef(digest=d, size=1, kind="file") for d in (dg("plan"), final)]
    )
    build = new_build(h, "a")
    assert h.store.get_build(build).final_plan_digest is None
    h.store.finish_build(build, "passed", final_plan=final)
    assert h.store.get_build(build).final_plan_digest == final
    h.clock.advance(2 * 3600)
    h.store.pin_build(build, by="alice")
    assert h.inspect.blob_last_access("test", final) == h.clock.now()

    plain = new_build(h, "a")
    h.store.finish_build(plain, "failed")  # no final plan: only the plan is rooted
    assert h.store.get_build(plain).final_plan_digest is None


# --- P3-06: pilot queues ------------------------------------------------------------------------


def q_item(action_id: str, **fields: Any) -> QueueItem:
    return QueueItem(action_id=action_id, plan=dg("plan"), **fields)


def q_slot(pilot: str = "100_0", **fields: Any) -> PilotSlot:
    return PilotSlot(**{"pilot": pilot, "cpus": 1, "time_left_s": 3600, **fields})


# P3-06 R1, R2: lease, renew and finish an item; an unrenewed lease is lost, never run twice.
def test_pilot_queue_lease_cycle(h: Harness) -> None:
    pool = uuid4()
    build = new_build(h, "a")
    ids = h.store.queue_put("test", pool, [q_item("a", build=build), q_item("b")])
    assert len(set(ids)) == 2
    assert h.store.queue_lease("test", uuid4(), q_slot(), 60) is None  # pools are separate
    assert h.store.queue_lease("other", pool, q_slot(), 60) is None  # and scoped by domain
    lease = h.store.queue_lease("test", pool, q_slot("100_0", node="n1"), 60)
    assert lease is not None
    assert (lease.item, lease.action_id, lease.plan, lease.build) == (
        ids[0],
        "a",
        dg("plan"),
        build,
    )
    assert lease.expires_at == h.clock.now() + timedelta(seconds=60)
    first, second = h.store.queue_status("test", pool, ids)
    assert (first.state, first.pilot, first.node) == ("leased", "100_0", "n1")
    assert second.state == "queued"
    h.clock.advance(50)
    assert h.store.queue_renew("test", pool, ids[0], "100_0", 60)
    assert not h.store.queue_renew("test", pool, ids[0], "100_1", 60)  # not that pilot's
    h.clock.advance(50)  # 100 s after the lease, 50 after its renewal: still live
    assert h.store.queue_status("test", pool, ids[:1])[0].state == "leased"
    assert not h.store.queue_finish("test", pool, ids[0], "100_1", 0)
    assert h.store.queue_finish("test", pool, ids[0], "100_0", 75, "preempted")
    assert not h.store.queue_finish("test", pool, ids[0], "100_0", 0)  # once
    done = h.store.queue_status("test", pool, ids[:1])[0]
    assert (done.state, done.exit_code, done.infra_reason) == ("done", 75, "preempted")

    lease = h.store.queue_lease("test", pool, q_slot("100_1"), 60)
    assert lease is not None
    assert lease.item == ids[1]
    assert h.store.queue_lease("test", pool, q_slot("100_2"), 60) is None  # nothing left
    h.clock.advance(60)  # exactly the TTL: the pilot is presumed dead
    lost = h.store.queue_status("test", pool, ids[1:])[0]
    assert (lost.state, lost.pilot) == ("lost", "100_1")
    assert not h.store.queue_renew("test", pool, ids[1], "100_1", 60)
    assert not h.store.queue_finish("test", pool, ids[1], "100_1", 0)
    assert h.store.queue_lease("test", pool, q_slot("100_2"), 60) is None  # never re-leased


# P3-06 R2: a renewal after the TTL fails even before anyone asked for the status.
def test_pilot_queue_late_renewal(h: Harness) -> None:
    pool = uuid4()
    (item,) = h.store.queue_put("test", pool, [q_item("a")])
    assert h.store.queue_lease("test", pool, q_slot(), 30) is not None
    h.clock.advance(31)
    assert not h.store.queue_renew("test", pool, item, "100_0", 30)
    assert h.store.queue_status("test", pool, [item])[0].state == "lost"


# P3-06 R3: a pilot leases only what fits its allocation, oldest first.
def test_pilot_queue_matching(h: Harness) -> None:
    pool = uuid4()
    items = [
        q_item("big", cpus=4),
        q_item("mem", mem_mb=8192),
        q_item("lic", licenses={"simlic": 2}),
        q_item("feat", features=("avx512", "bigmem")),
        q_item("long", time_s=7200),
        q_item("small", cpus=2, mem_mb=1024, time_s=60, licenses={"simlic": 1}),
    ]
    ids = dict(
        zip([i.action_id for i in items], h.store.queue_put("test", pool, items), strict=True)
    )
    slot = q_slot(
        cpus=2, mem_mb=4096, time_left_s=3600, licenses={"simlic": 1}, features=("avx512",)
    )
    lease = h.store.queue_lease("test", pool, slot, 60)
    assert lease is not None
    assert lease.action_id == "small"
    assert h.store.queue_lease("test", pool, slot, 60) is None
    wide = q_slot(
        "100_1",
        cpus=8,
        time_left_s=3600,
        licenses={"simlic": 2, "other": 1},
        features=("avx512", "bigmem", "ssd"),
    )
    got = []
    while (lease := h.store.queue_lease("test", pool, wide, 60)) is not None:
        got.append(lease.action_id)
    assert got == ["big", "mem", "lic", "feat"]  # mem: the slot has no memory limit
    entries = h.store.queue_status("test", pool, [ids["long"]])
    assert entries[0].state == "queued"
    shunned = q_item("shunned", exclude=("n1",))
    (shunned_id,) = h.store.queue_put("test", pool, [shunned])
    assert h.store.queue_lease("test", pool, q_slot("100_3", node="n1"), 60) is None
    lease = h.store.queue_lease("test", pool, q_slot("100_3", node="n2"), 60)
    assert lease is not None
    assert lease.item == shunned_id
    long_enough = q_slot("100_2", time_left_s=7200)
    lease = h.store.queue_lease("test", pool, long_enough, 60)
    assert lease is not None
    assert lease.action_id == "long"


# P3-06 R5 (cancel): queued and leased items are cancelled, final ones keep their state.
def test_pilot_queue_cancel(h: Harness) -> None:
    pool = uuid4()
    ids = h.store.queue_put("test", pool, [q_item("a"), q_item("b"), q_item("c")])
    assert h.store.queue_lease("test", pool, q_slot("100_0"), 60) is not None
    assert h.store.queue_lease("test", pool, q_slot("100_1"), 60) is not None
    assert h.store.queue_finish("test", pool, ids[1], "100_1", 0)
    h.store.queue_cancel("test", pool, ids)
    states = [e.state for e in h.store.queue_status("test", pool, ids)]
    assert states == ["cancelled", "done", "cancelled"]
    assert not h.store.queue_renew("test", pool, ids[0], "100_0", 60)  # its pilot stops
    assert not h.store.queue_finish("test", pool, ids[0], "100_0", 0)
    assert h.store.queue_lease("test", pool, q_slot("100_2"), 60) is None
    h.store.queue_cancel("test", pool, [])  # nothing to do


def test_pilot_queue_errors(h: Harness) -> None:
    pool = uuid4()
    (item,) = h.store.queue_put("test", pool, [q_item("a")])
    with pytest.raises(MetadataError, match="queue item"):
        h.store.queue_status("test", uuid4(), [item])  # another pool's
    with pytest.raises(MetadataError, match="queue item"):
        h.store.queue_status("other", pool, [item])  # another domain's
    with pytest.raises(MetadataError, match="queue item"):
        h.store.queue_status("test", pool, [item + 1000])
    for bad in (0, 86_401):
        with pytest.raises(MetadataError, match="ttl_s"):
            h.store.queue_lease("test", pool, q_slot(), bad)
    with pytest.raises(MetadataError):
        h.store.queue_put("bad domain!", pool, [q_item("a")])
    with pytest.raises(MetadataError):
        h.store.queue_put("test", pool, [QueueItem.model_construct(action_id="", plan=dg("p"))])
    assert h.store.queue_put("test", pool, []) == []


# P3-06 R1: a large regression is queued through more than one HTTP batch.
def test_pilot_queue_large_put(h: Harness) -> None:
    pool = uuid4()
    ids = h.store.queue_put("test", pool, [q_item(f"t[{i}]") for i in range(1200)])
    assert len(set(ids)) == 1200
    entries = h.store.queue_status("test", pool, ids)
    assert [e.action_id for e in entries] == [f"t[{i}]" for i in range(1200)]


# P3-06 R1: concurrent pilots never lease the same item (PostgreSQL: SKIP LOCKED).
def test_pilot_queue_concurrent_leases(h: Harness) -> None:
    pool = uuid4()
    ids = h.store.queue_put("test", pool, [q_item(f"t[{i}]") for i in range(40)])
    got: list[int] = []
    lock = threading.Lock()
    errors: list[BaseException] = []

    def pilot(n: int) -> None:
        try:
            while (lease := h.store.queue_lease("test", pool, q_slot(f"100_{n}"), 60)) is not None:
                with lock:
                    got.append(lease.item)
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=pilot, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sorted(got) == sorted(ids)


# P3-06 R3: the edges of the matching rule (the SQL must agree with slot_fits).
def test_pilot_queue_matching_edges(h: Harness) -> None:
    pool = uuid4()
    h.store.queue_put("test", pool, [q_item("cov", licenses={"cov": 1})])
    assert h.store.queue_lease("test", pool, q_slot(licenses={"simlic": 1}), 60) is None
    pool = uuid4()
    h.store.queue_put("test", pool, [q_item("exact", mem_mb=1024), q_item("any")])
    slot = q_slot(mem_mb=1024)
    first = h.store.queue_lease("test", pool, slot, 60)
    second = h.store.queue_lease("test", pool, slot, 60)
    assert first is not None
    assert second is not None
    assert (first.action_id, second.action_id) == ("exact", "any")


# P3-06 R2: at exactly the TTL the lease is gone for renew and finish too, as for status.
def test_pilot_queue_expiry_boundary(h: Harness) -> None:
    pool = uuid4()
    a, b = h.store.queue_put("test", pool, [q_item("a"), q_item("b")])
    assert h.store.queue_lease("test", pool, q_slot("100_0"), 60) is not None
    assert h.store.queue_lease("test", pool, q_slot("100_1"), 60) is not None
    h.clock.advance(60)
    assert not h.store.queue_renew("test", pool, a, "100_0", 60)
    assert not h.store.queue_finish("test", pool, b, "100_1", 0)
