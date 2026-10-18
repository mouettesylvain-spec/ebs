"""Channels (P2-02 R3): moves append history; a gate needs a passing consumer build."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from ebs.core.clock import FakeClock
from ebs.core.errors import ConfigError, MetadataError, ReleaseError
from ebs.meta.api import ActionRow, BuildId, GateCheck, ReleaseView
from ebs.meta.memory import InMemoryMetadataStore
from ebs.release.channels import GatePolicy, move_channel, require_passing
from ebs.release.releases import create_release
from tests.helpers.release import Action, finish, out

LIB = out(b"worklib", deterministic=False)
NAME = "rtl-team/cpu"


def _release(store: InMemoryMetadataStore, version: str) -> ReleaseView:
    build = finish(store, {"compile": Action({"worklib": out(version.encode())})})
    return create_release(
        store, name=NAME, version=version, build=build, outputs=["compile.worklib"], by="alice"
    )


# R3: `set` moves the pointer and appends to the history, oldest first, never rewriting it.
def test_history() -> None:
    clock = FakeClock()
    store = InMemoryMetadataStore(clock=clock)
    v1, v2 = _release(store, "1.0"), _release(store, "2.0")
    assert store.get_channel("test", NAME, "stable") is None
    assert store.channel_history("test", NAME, "stable") == []
    first = move_channel(store, GatePolicy(), v1, "stable", by="alice")
    clock.advance(10)
    move_channel(store, GatePolicy(), v2, "stable", by="bob")
    clock.advance(10)
    move_channel(store, GatePolicy(), v1, "stable", by="carol")  # a rollback is a new move
    move_channel(store, GatePolicy(), v2, "nightly", by="alice")
    history = store.channel_history("test", NAME, "stable")
    assert [(m.version, m.moved_by) for m in history] == [
        ("1.0", "alice"), ("2.0", "bob"), ("1.0", "carol"),
    ]  # fmt: skip
    assert history[0] == first
    assert history[0].gate is None
    assert history[0].moved_at < history[1].moved_at < history[2].moved_at
    channel = store.get_channel("test", NAME, "stable")
    assert channel is not None
    assert (channel.release.version, channel.updated_by, channel.updated_at) == (
        "1.0", "carol", history[2].moved_at,
    )  # fmt: skip
    nightly = store.get_channel("test", NAME, "nightly")
    assert nightly is not None
    assert nightly.release.version == "2.0"
    # A channel follows releases of its own name and domain only.
    other = finish(store, {"compile": Action({"worklib": LIB})})
    gpu = create_release(
        store, name="rtl-team/gpu", version="1.0", build=other, outputs=["compile.worklib"],
        by="alice",
    )  # fmt: skip
    with pytest.raises(MetadataError, match="own name and domain"):
        store.set_channel("test", NAME, "stable", gpu.id, by="alice")
    with pytest.raises(MetadataError, match="own name and domain"):
        store.set_channel("other", "rtl-team/gpu", "stable", gpu.id, by="alice")
    with pytest.raises(MetadataError, match="invalid channel name"):
        store.set_channel("test", NAME, "Stable!", v1.id, by="alice")
    assert len(store.channel_history("test", NAME, "stable")) == 3


def _consumer(
    store: InMemoryMetadataStore, rel: ReleaseView, *, project: str = "cpu-gate",
    passed: bool = True, consumes: bool = True,
) -> BuildId:  # fmt: skip
    """A build of `project` whose sim action staged the release's worklib (or something else)."""
    staged = next(iter(rel.outputs.values())).id if consumes else out(b"unrelated").id
    return finish(
        store,
        {"sim": Action({"result": out(b"pass")}, passed=passed, inputs={"lib": staged})},
        project=project,
        status="passed" if passed else "failed",
    )


GATES = GatePolicy({"stable": "cpu-gate", f"{NAME}:nightly": "smoke"})

Setup = Callable[[InMemoryMetadataStore, ReleaseView], BuildId | None]


# R3: a gated channel moves only with a passed build of the gate flow that consumed the release
# (provenance `in` edges); the check's result is stored in the history.
@pytest.mark.parametrize(
    ("setup", "named", "error"),
    [
        (lambda s, r: _consumer(s, r), False, None),
        (lambda s, r: _consumer(s, r), True, None),
        (lambda s, r: None, False, "none was found"),
        (lambda s, r: _consumer(s, r, project="other-flow"), False, "none was found"),
        (lambda s, r: _consumer(s, r, passed=False), False, "none was found"),
        (lambda s, r: _consumer(s, r, consumes=False), False, "none was found"),
        (lambda s, r: _consumer(s, r, consumes=False), True, "not a passed build"),
    ],
    ids=[
        "consumer-passed", "named-gate-build", "no-consumer", "other-project", "consumer-failed",
        "consumed-something-else", "named-build-not-consumer",
    ],
)  # fmt: skip
def test_gate_policy(setup: Setup, named: bool, error: str | None) -> None:
    store = InMemoryMetadataStore(clock=FakeClock())
    rel = _release(store, "1.0")
    gate_build = setup(store, rel)
    named_build = gate_build if named else None
    if error is not None:
        with pytest.raises(ReleaseError, match=error):
            move_channel(store, GATES, rel, "stable", by="alice", gate_build=named_build)
        assert store.channel_history("test", NAME, "stable") == []
        assert store.get_channel("test", NAME, "stable") is None
        return
    assert gate_build is not None
    move = move_channel(store, GATES, rel, "stable", by="alice", gate_build=named_build)
    assert move.gate == GateCheck(flow="cpu-gate", build=gate_build)
    assert store.channel_history("test", NAME, "stable") == [move]


# R3: the policy is per channel, or per release name and channel; ungated channels move freely.
def test_gate_lookup() -> None:
    assert GATES.gate_for(NAME, "stable") == "cpu-gate"
    assert GATES.gate_for(NAME, "nightly") == "smoke"
    assert GATES.gate_for("rtl-team/gpu", "nightly") is None
    store = InMemoryMetadataStore(clock=FakeClock())
    rel = _release(store, "1.0")
    assert move_channel(store, GATES, rel, "dev", by="alice").gate is None
    with pytest.raises(ReleaseError, match="'smoke'"):
        move_channel(store, GATES, rel, "nightly", by="alice")
    assert GatePolicy.from_section({"gates": {"stable": "g"}}).gates == {"stable": "g"}
    assert GatePolicy.from_section({}).gates == {}
    for bad in ({"gates": "x"}, {"gates": {"stable": ""}}, {"gates": {"stable": 3}}):
        with pytest.raises(ConfigError, match=r"\[release.gates\]"):
            GatePolicy.from_section(bad)


# R3: a gate build that consumed the release only through a cache hit still counts (the edges
# belong to the action key, not to the build that first ran it).
def test_gate_via_cache_hit() -> None:
    store = InMemoryMetadataStore(clock=FakeClock())
    rel = _release(store, "1.0")
    first = _consumer(store, rel, project="cpu-gate")
    key = store.list_actions(first)[0].key
    assert key is not None
    hit = finish(store, {}, project="cpu-gate", status=None)
    store.add_actions(hit, [ActionRow(action_id="sim", step="sim", key=key)])
    store.set_action_state(hit, "sim", "cached")
    store.finish_build(hit, "passed")
    assert store.gate_builds("test", "cpu-gate", [next(iter(rel.outputs.values())).id]) == [
        hit, first,
    ]  # fmt: skip


# P2-07 R4: `--if-passing` needs a passed build of the release's domain; it becomes the gate.
def test_require_passing() -> None:
    store = InMemoryMetadataStore(clock=FakeClock())
    rel = _release(store, "1.0")
    ok = finish(store, {"sim": Action()}, project="verif")
    bad = finish(store, {"sim": Action(passed=False)}, project="verif", status="failed")
    running = finish(store, {"sim": Action()}, status=None)
    other = finish(store, {"sim": Action()}, domain="other")
    for build, why in [(bad, "is failed"), (running, "is running"), (other, "domain 'other'")]:
        with pytest.raises(ReleaseError, match=why):
            require_passing(store, build, rel)
    passed = require_passing(store, ok, rel)
    move = move_channel(store, GatePolicy(), rel, "nightly", by="ci", passed=passed)
    assert move.gate == GateCheck(flow="verif", build=ok)
