"""Access tracking (P1-09 R1): every use of a CAS object reaches the `blobs` table.

Cache hits, fetches and materializations `touch` the objects they use (throttled to one write
per hour by the store, P0-10 R3), and every CAS put by the runner or the driver registers the
objects it stored. Imports arrive with P2-03.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path

import pytest

from ebs.cas.fs import FsCAS
from ebs.core.clock import FakeClock
from ebs.core.digest import Digest, hash_bytes
from ebs.gc.access import put_registered
from ebs.meta.api import BlobRef
from ebs.meta.memory import InMemoryMetadataStore
from ebs.plan.types import OutputSpec
from ebs.runner.errors import EXIT_INFRA
from tests.helpers.cli import Site, TestServices, write_site
from tests.helpers.driver import Node, make_env
from tests.helpers.outputs import digest_of, write_outputs_build
from tests.helpers.runner import Harness, RecordingStore, shell_spec, source_file

DOMAIN = "test"
HOUR = 3600.0


def last_access(store: InMemoryMetadataStore, digest: Digest) -> datetime | None:
    found = {b.digest: b.last_access for b in store.gc_blobs(DOMAIN)}
    return found.get(digest)


def cache_hit(tmp_path: Path) -> None:
    """A second build served from the cache touches the outputs it reuses."""
    env = make_env(tmp_path)
    plan = env.plan({"a": Node()})
    first = env.run(plan, env.executor(), cache_mode="write")
    out = env.store.get_result(first.build, "a")
    assert out is not None
    digest = out.outputs["out"].digest
    created = last_access(env.store, digest)
    env.clock.advance(2 * HOUR)
    second = env.run(plan, env.executor(), cache_mode="read")
    assert second.states == {"a": "cached"}
    assert created is not None
    assert last_access(env.store, digest) == env.clock.now() != created


def driver_plan(tmp_path: Path) -> None:
    """The driver registers the plan it stores in the CAS (on build start and when refined)."""
    env = make_env(tmp_path)
    plan = env.plan({"a": Node(), "b": Node(deps=(("a", "out"),))})
    outcome = env.run(plan, env.executor(), cache_mode="write")
    view = env.store.get_build(outcome.build)
    registered = {b.digest: b for b in env.store.gc_blobs(DOMAIN)}
    assert view.plan_digest in registered
    stored = [b for b in registered.values() if b.kind == "file" and env.cas.has(b.digest)]
    with env.cas.open(view.plan_digest) as f:
        assert registered[view.plan_digest].size == len(f.read())
    assert len(stored) >= 2  # the refined plan the runner of `b` loaded is registered too


def runner_inputs(tmp_path: Path) -> None:
    """The runner touches every input it materializes."""
    h = Harness(tmp_path, FsCAS(tmp_path / "cas", DOMAIN))
    ref = source_file(h.cas, "in.txt", b"payload\n")
    assert ref.id is not None
    h.store.register_blobs(DOMAIN, [BlobRef(digest=ref.id, size=8, kind="file")])
    created = last_access(h.store, ref.id)
    assert isinstance(h.store._clock, FakeClock)
    h.store._clock.advance(2 * HOUR)
    code, _ = h.run(shell_spec("cat in.txt", inputs=[ref]))
    assert code == 0
    assert last_access(h.store, ref.id) == h.store._clock.now() != created


def runner_outputs(tmp_path: Path) -> None:
    """The runner registers every object it stores: files, trees with all they contain, logs."""
    h = Harness(tmp_path, FsCAS(tmp_path / "cas", DOMAIN))
    script = "mkdir -p lib/sub && printf a > lib/a && printf bb > lib/sub/b && printf r > r.txt"
    spec = shell_spec(
        script,
        outputs=[OutputSpec("lib", "lib", "dir"), OutputSpec("report", "r.txt", "file")],
    )
    code, build = h.run(spec)
    assert code == 0
    result = h.result(build)
    assert result is not None
    assert result.log is not None
    registered = {b.digest: b for b in h.store.gc_blobs(DOMAIN)}
    tree = result.outputs["lib"].digest
    expected = {tree, result.outputs["report"].digest, result.log}
    expected |= {hash_bytes(b"a"), hash_bytes(b"bb")}
    sub = next(e.digest for e in h.cas.get_tree(tree).entries if e.name == "sub")
    assert sub is not None
    expected.add(sub)
    assert expected <= set(registered)
    assert registered[hash_bytes(b"bb")].size == 2
    assert registered[sub].kind == "tree"
    assert registered[tree].kind == "tree"


def fetch(tmp_path: Path) -> None:
    """`ebs fetch` touches what it copies out of the CAS."""
    write_site(tmp_path)
    site = Site(tmp_path, TestServices(tmp_path))
    built = write_outputs_build(site)
    digest = digest_of(built, "gen", "report")
    created = last_access(site.services.memory_store, digest)
    clock = site.services.clock
    assert isinstance(clock, FakeClock)
    clock.advance(2 * HOUR)
    result = site.invoke(["fetch", "gen", "-o", "got"])
    assert result.exit_code == 0, result.output
    assert last_access(site.services.memory_store, digest) == clock.now() != created


PATHS: dict[str, Callable[[Path], None]] = {
    "cache_hit": cache_hit,
    "driver_plan": driver_plan,
    "runner_inputs": runner_inputs,
    "runner_outputs": runner_outputs,
    "fetch": fetch,
}


# R1
@pytest.mark.parametrize("path", sorted(PATHS))
def test_touch_paths(path: str, tmp_path: Path) -> None:
    PATHS[path](tmp_path)


# R1: storing a known object again counts as a use, with the same hourly throttle as `touch`.
def test_register_touches_known_rows(tmp_path: Path) -> None:
    clock = FakeClock()
    store = InMemoryMetadataStore(clock=clock)
    d = hash_bytes(b"x")
    store.register_blobs(DOMAIN, [BlobRef(digest=d, size=1, kind="file")])
    t0 = clock.now()
    clock.advance(HOUR / 2)
    store.register_blobs(DOMAIN, [BlobRef(digest=d, size=1, kind="file")])
    assert last_access(store, d) == t0  # throttled
    clock.advance(HOUR)
    store.register_blobs(DOMAIN, [BlobRef(digest=d, size=1, kind="file")])
    assert last_access(store, d) == clock.now()


class _CollectsBeforeRegister(RecordingStore):
    """GC claims and unlinks the first registered object just before the registration lands
    (the put found the bytes already stored, so nothing was written)."""

    def __init__(self, cas: FsCAS) -> None:
        super().__init__()
        self.cas = cas
        self.collected: list[Digest] = []

    def register_blobs(self, domain: str, blobs: Iterable[BlobRef]) -> None:
        refs = list(blobs)
        if not self.collected and refs:
            self.collected.append(refs[0].digest)
            self.cas.delete(refs[0].digest)
        super().register_blobs(domain, refs)


# R1 (I15): a stored plan that GC collected between the put and its registration is stored
# again, so the driver never hands out a plan digest without bytes.
def test_put_registered_restores_collected_bytes(tmp_path: Path) -> None:
    cas = FsCAS(tmp_path / "cas", DOMAIN)
    store = _CollectsBeforeRegister(cas)
    digest = put_registered(cas, store, DOMAIN, b"plan bytes")
    assert store.collected == [digest]
    assert cas.has(digest)
    assert last_access(store, digest) is not None


# R1 (I15): a runner whose outputs were collected between the put and the registration posts no
# result (a cache entry without bytes would never expire); it reports an infra failure instead.
def test_runner_refuses_to_post_collected_outputs(tmp_path: Path) -> None:
    cas = FsCAS(tmp_path / "cas", DOMAIN)
    h = Harness(tmp_path, cas, store=_CollectsBeforeRegister(cas))
    spec = shell_spec("printf r > r.txt", outputs=[OutputSpec("report", "r.txt", "file")])
    code, build = h.run(spec)
    assert code == EXIT_INFRA
    assert h.result(build) is None
    assert h.store.cache_puts == []
    assert "garbage-collected" in h.err.getvalue()
