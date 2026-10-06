"""MetadataStore contract suite (docs/design/interfaces.md § 6).

Every implementation must pass it: `memory` runs as a unit test, `pg` as an integration test.
P1-06 adds `http` to `BACKENDS` with an `Inspector` over the service's backing store.
The inspector reads state the protocol does not expose (hit counters, edges) and injects faults.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime
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
    BuildCreate,
    BuildId,
    Event,
    InfraReason,
    MetadataStore,
    OutputResult,
    ResourceUsage,
    ResultManifest,
    RunnerInfo,
)
from ebs.meta.memory import InMemoryMetadataStore
from ebs.meta.pg import PgMetadataStore
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


@dataclass
class Harness:
    store: MetadataStore
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
        return [(e.type, e.action_id) for e in self._s._events if e.build == build]

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
]


@pytest.fixture(params=BACKENDS)
def h(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    clock = FakeClock()
    if request.param == "memory":
        mem = InMemoryMetadataStore(clock=clock)
        yield Harness(mem, clock, MemoryInspector(mem, monkeypatch))
        return
    url: URL = request.getfixturevalue("pg_url")
    pg = PgMetadataStore.from_config({"url": url_string(url)}, clock=clock)
    inspector = PgInspector(url)
    try:
        yield Harness(pg, clock, inspector)
    finally:
        pg.close()
        inspector.close()


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
