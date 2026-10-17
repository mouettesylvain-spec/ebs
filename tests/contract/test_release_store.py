"""ReleaseStore contract suite (P2-02): `memory` as a unit test, `pg` as an integration test.

The HTTP store runs the same calls through the service in tests/integration/release/test_api.py.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from sqlalchemy.engine import URL

from ebs.core.clock import FakeClock
from ebs.core.errors import MetadataError
from ebs.meta.api import (
    ActionRow,
    GateCheck,
    ReleaseCreate,
    ReleaseOutput,
    ReleaseView,
    release_output_name,
)
from ebs.meta.memory import InMemoryMetadataStore
from ebs.meta.pg import PgMetadataStore
from ebs.release.releases import ReleaseBackend
from tests.helpers.pg import url_string
from tests.helpers.release import Action, finish, out

D = "test"
LIB = out(b"worklib", deterministic=False)
REPORT = out(b"report")


@dataclass
class S:
    store: ReleaseBackend
    clock: FakeClock


@pytest.fixture(params=["memory", pytest.param("pg", marks=pytest.mark.integration)])
def s(request: pytest.FixtureRequest) -> Iterator[S]:
    clock = FakeClock()
    if request.param == "memory":
        yield S(InMemoryMetadataStore(clock=clock), clock)
        return
    url: URL = request.getfixturevalue("pg_url")
    pg = PgMetadataStore.from_config({"url": url_string(url)}, clock=clock)
    try:
        yield S(pg, clock)
    finally:
        pg.close()


def _spec(build: int, version: str = "1.0", **kwargs: object) -> ReleaseCreate:
    outputs = {
        release_output_name("compile[lib=core]", "worklib"): ReleaseOutput(
            action_id="compile[lib=core]", output="worklib", **LIB.model_dump()
        ),
    }
    fields: dict[str, object] = {
        "domain": D, "name": "rtl-team/cpu", "version": version, "build": build,
        "outputs": outputs, "notes": "first cut",
    }  # fmt: skip
    fields.update(kwargs)
    return ReleaseCreate.model_validate(fields)


def _released(s: S, version: str = "1.0") -> ReleaseView:
    build = finish(s.store, {"compile[lib=core]": Action({"worklib": LIB})})
    return s.store.create_release(_spec(build, version), by="alice")


def test_create_and_read(s: S) -> None:
    rel = _released(s)
    assert (rel.name, rel.version, rel.notes, rel.created_by) == (
        "rtl-team/cpu", "1.0", "first cut", "alice",
    )  # fmt: skip
    assert rel.created_at == s.clock.now()
    assert rel.exported_from is None
    assert rel.yanked_at is None
    assert s.store.get_release(D, "rtl-team/cpu", "1.0") == rel
    assert s.store.get_release_by_id(rel.id) == rel
    assert s.store.get_release(D, "rtl-team/cpu", "9.9") is None
    assert s.store.get_release("other", "rtl-team/cpu", "1.0") is None
    s.clock.advance(1)
    v2 = _released(s, "2.0")
    gpu_build = finish(s.store, {"lint": Action({"report": REPORT})})
    gpu = s.store.create_release(
        ReleaseCreate(
            domain=D, name="rtl-team/gpu", version="1.0", build=gpu_build,
            outputs={"lint.report": ReleaseOutput(
                action_id="lint", output="report", **REPORT.model_dump())},
        ),
        by="bob",
    )  # fmt: skip
    assert s.store.list_releases(D) == [rel, v2, gpu]
    assert s.store.list_releases(D, name="rtl-team/gpu") == [gpu]
    assert s.store.list_releases("other") == []


def test_create_checks(s: S) -> None:
    running = finish(s.store, {"compile[lib=core]": Action({"worklib": LIB})}, status=None)
    with pytest.raises(MetadataError, match="still running"):
        s.store.create_release(_spec(running), by="alice")
    failed = finish(
        s.store, {"compile[lib=core]": Action({"worklib": LIB}, passed=False)}, status="failed"
    )
    with pytest.raises(MetadataError, match="did not pass"):
        s.store.create_release(_spec(failed), by="alice")
    waived = s.store.create_release(_spec(failed, allow_failed="known issue"), by="alice")
    assert waived.allow_failed == "known issue"
    with pytest.raises(MetadataError, match="already exists"):
        s.store.create_release(_spec(failed, allow_failed="again"), by="alice")
    forged = finish(s.store, {"compile[lib=core]": Action({"worklib": out(b"other")})})
    with pytest.raises(MetadataError, match="does not match"):
        s.store.create_release(_spec(forged, "2.0"), by="alice")
    with pytest.raises(MetadataError, match="no build"):
        s.store.create_release(_spec(9999, "3.0"), by="alice")
    with pytest.raises(MetadataError, match="author"):
        s.store.create_release(_spec(forged, "4.0"), by="")
    assert [r.version for r in s.store.list_releases(D)] == ["1.0"]


def test_yank_and_roots(s: S) -> None:
    rel = _released(s)
    assert s.store.gc_roots(D, s.clock.now()).released == {LIB.digest}  # type: ignore[attr-defined]
    s.clock.advance(5)
    yanked = s.store.yank_release(rel.id, by="bob", reason="bad")
    assert (yanked.yanked_at, yanked.yanked_by, yanked.yank_reason) == (s.clock.now(), "bob", "bad")
    s.clock.advance(5)
    assert s.store.yank_release(rel.id, by="carol", reason="again") == yanked
    assert s.store.list_releases(D) == [yanked]
    assert s.store.gc_roots(D, s.clock.now()).released == {LIB.digest}  # type: ignore[attr-defined]
    with pytest.raises(MetadataError, match="no release"):
        s.store.yank_release(4242, by="bob", reason="x")  # type: ignore[arg-type]


def test_channels(s: S) -> None:
    v1 = _released(s)
    s.clock.advance(1)
    v2 = _released(s, "2.0")
    assert s.store.get_channel(D, "rtl-team/cpu", "stable") is None
    first = s.store.set_channel(D, "rtl-team/cpu", "stable", v1.id, by="alice")
    s.clock.advance(1)
    gate = GateCheck(flow="cpu-gate", build=v2.build)
    second = s.store.set_channel(D, "rtl-team/cpu", "stable", v2.id, by="bob", gate=gate)
    assert s.store.channel_history(D, "rtl-team/cpu", "stable") == [first, second]
    assert second.gate == gate
    assert first.version == "1.0"
    channel = s.store.get_channel(D, "rtl-team/cpu", "stable")
    assert channel is not None
    assert (channel.release, channel.updated_by, channel.updated_at) == (
        v2, "bob", second.moved_at,
    )  # fmt: skip
    s.store.yank_release(v1.id, by="bob", reason="bad")
    with pytest.raises(MetadataError, match="yanked"):
        s.store.set_channel(D, "rtl-team/cpu", "stable", v1.id, by="alice")
    with pytest.raises(MetadataError, match="own name and domain"):
        s.store.set_channel(D, "rtl-team/gpu", "stable", v2.id, by="alice")
    with pytest.raises(MetadataError, match="no such release"):
        s.store.set_channel(D, "rtl-team/cpu", "stable", 4242, by="alice")  # type: ignore[arg-type]
    assert len(s.store.channel_history(D, "rtl-team/cpu", "stable")) == 2


def test_gate_builds(s: S) -> None:
    rel = _released(s)
    staged = {"lib": LIB.id}
    consumer = finish(
        s.store, {"sim": Action({"result": out(b"ok")}, inputs=staged)}, project="cpu-gate"
    )
    finish(  # failed, other project, other input: none counts
        s.store, {"sim": Action({"result": out(b"ko")}, passed=False, inputs=staged)},
        project="cpu-gate", status="failed",
    )  # fmt: skip
    finish(s.store, {"sim": Action({"result": out(b"x")}, inputs=staged)}, project="other")
    finish(s.store, {"sim": Action({"result": out(b"y")}, inputs={"lib": REPORT.id})},
           project="cpu-gate")  # fmt: skip
    # A cache hit of the consumer's action consumed the release too.
    key = s.store.list_actions(consumer)[0].key
    hit = finish(s.store, {}, project="cpu-gate", status=None)
    s.store.add_actions(hit, [ActionRow(action_id="sim", step="sim", key=key)])
    s.store.set_action_state(hit, "sim", "cached")
    s.store.finish_build(hit, "passed")
    ids = [o.id for o in rel.outputs.values()]
    assert s.store.gate_builds(D, "cpu-gate", ids) == [hit, consumer]
    assert s.store.gate_builds("other", "cpu-gate", ids) == []
    assert s.store.gate_builds(D, "cpu-gate", []) == []


def test_record_export(s: S) -> None:
    rel = _released(s)
    s.clock.advance(1)
    exported = s.store.record_export(rel.id, "partner", by="bob")
    assert exported.domain == "partner"
    assert exported.exported_from == rel.id
    assert exported.created_by == "bob"
    assert exported.model_dump(include={"name", "version", "build", "outputs", "notes"}) == (
        rel.model_dump(include={"name", "version", "build", "outputs", "notes"})
    )
    assert s.store.get_release("partner", "rtl-team/cpu", "1.0") == exported
    [audit] = s.store.list_exports(rel.id)
    assert (audit.release, audit.exported, audit.to_domain, audit.exported_by) == (
        rel.id, exported.id, "partner", "bob",
    )  # fmt: skip
    assert audit.exported_at == s.clock.now()
    assert s.store.list_exports(exported.id) == [audit]
    assert s.store.gc_roots("partner", s.clock.now()).released == {LIB.digest}  # type: ignore[attr-defined]
    with pytest.raises(MetadataError, match="already exists"):
        s.store.record_export(rel.id, "partner", by="bob")
    with pytest.raises(MetadataError, match="already in domain"):
        s.store.record_export(rel.id, D, by="bob")
    assert len(s.store.list_exports(rel.id)) == 1
    s.store.yank_release(rel.id, by="bob", reason="bad")
    with pytest.raises(MetadataError, match="yanked"):
        s.store.record_export(rel.id, "third", by="bob")
