"""Releases, channels and exports over the metadata service API on PostgreSQL (P2-02 R5).

Clients use `HttpMetadataStore`; the service authorizes every write on the owner of the
release's build (P2-01 adds team and domain membership to the same hook) and applies its own
gate policy, so a client cannot skip a gate or forge an output.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.engine import URL

from ebs.core.clock import FakeClock
from ebs.core.errors import MetadataError
from ebs.meta.api import GateCheck, ReleaseCreate, ReleaseOutput
from ebs.meta.client import HttpMetadataStore
from ebs.meta.pg import PgMetadataStore
from ebs.release.channels import GatePolicy, move_channel
from ebs.release.releases import create_release
from tests.helpers.meta_http import FakeMunge, Service, make_service
from tests.helpers.pg import url_string
from tests.helpers.release import Action, finish, out

LIB = out(b"worklib", deterministic=False)
NAME = "rtl-team/cpu"


@pytest.fixture
def pg(pg_url: URL) -> Iterator[PgMetadataStore]:
    store = PgMetadataStore.from_config({"url": url_string(pg_url)}, clock=FakeClock())
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def service(pg: PgMetadataStore) -> Service:
    return make_service(pg, gates=GatePolicy({"stable": "cpu-gate"}))


def _clients(service: Service) -> tuple[HttpMetadataStore, HttpMetadataStore]:
    return service.store(FakeMunge(uid=1000)), service.store(FakeMunge(uid=1001))  # alice, bob


# R5: create, read, list and yank over the API, with the caller as author and owner checks.
def test_releases(service: Service, pg: PgMetadataStore) -> None:
    alice, bob = _clients(service)
    build = finish(alice, {"compile[lib=core]": Action({"worklib": LIB})})
    with pytest.raises(MetadataError, match="'bob' is not allowed to release"):
        create_release(
            bob, name=NAME, version="1.0", build=build, outputs=["compile.worklib"], by="bob"
        )
    rel = create_release(
        alice, name=NAME, version="1.0", build=build, outputs=["compile.worklib"],
        by="ignored", notes="first",
    )  # fmt: skip
    assert rel.created_by == "alice"  # the authenticated caller
    assert pg.get_release_by_id(rel.id) == rel
    assert bob.get_release("test", NAME, "1.0") == rel  # reads are open (until P2-01)
    assert bob.get_release_by_id(rel.id) == rel
    assert bob.get_release_by_id(4242) is None  # type: ignore[arg-type]
    assert bob.list_releases("test") == [rel]
    assert bob.list_releases("test", name="rtl-team/gpu") == []
    # The service's store checks what a client sends against the build.
    forged = ReleaseCreate(
        domain="test", name=NAME, version="2.0", build=build,
        outputs={"compile[lib=core].worklib": ReleaseOutput(
            action_id="compile[lib=core]", output="worklib", **out(b"forged").model_dump())},
    )  # fmt: skip
    with pytest.raises(MetadataError, match="does not match"):
        alice.create_release(forged, by="alice")
    with pytest.raises(MetadataError, match="already exists"):
        alice.create_release(
            ReleaseCreate.model_validate(rel.model_dump(include=set(ReleaseCreate.model_fields))),
            by="alice",
        )
    with pytest.raises(MetadataError, match="'bob' is not allowed"):
        bob.yank_release(rel.id, by="bob", reason="mine now")
    yanked = alice.yank_release(rel.id, by="ignored", reason="bad waiver")
    assert (yanked.yanked_by, yanked.yank_reason) == ("alice", "bad waiver")
    assert pg.get_release_by_id(rel.id) == yanked


# R5, R3: channel moves over the API run the service's gate, whatever the client's policy says.
def test_channels_and_gate(service: Service, pg: PgMetadataStore) -> None:
    alice, bob = _clients(service)
    build = finish(alice, {"compile[lib=core]": Action({"worklib": LIB})})
    rel = create_release(
        alice, name=NAME, version="1.0", build=build, outputs=["compile.worklib"], by="alice"
    )
    nightly = move_channel(alice, GatePolicy(), rel, "nightly", by="alice")  # ungated
    assert nightly.gate is None
    assert nightly.moved_by == "alice"
    with pytest.raises(MetadataError, match="none was found"):
        move_channel(alice, GatePolicy(), rel, "stable", by="alice")  # the service's gate
    with pytest.raises(MetadataError, match="'bob' is not allowed"):
        bob.set_channel("test", NAME, "nightly", rel.id, by="bob")
    with pytest.raises(MetadataError, match="own name and domain"):
        alice.set_channel("test", "rtl-team/gpu", "nightly", rel.id, by="alice")
    gate = finish(
        bob, {"sim": Action({"result": out(b"ok")}, inputs={"lib": LIB.id})},
        project="cpu-gate", user="bob",
    )  # fmt: skip
    assert alice.gate_builds("test", "cpu-gate", [LIB.id]) == [gate]
    stable = alice.set_channel("test", NAME, "stable", rel.id, by="alice")
    assert stable.gate == GateCheck(flow="cpu-gate", build=gate)
    assert bob.channel_history("test", NAME, "stable") == [stable]
    assert pg.channel_history("test", NAME, "stable") == [stable]
    channel = bob.get_channel("test", NAME, "stable")
    assert channel is not None
    assert channel.release == rel
    assert bob.get_channel("test", NAME, "dev") is None
    with pytest.raises(MetadataError, match="not a passed build"):
        alice.set_channel(
            "test", NAME, "stable", rel.id, by="alice",
            gate=GateCheck(flow="cpu-gate", build=build),  # names a build that is no gate
        )  # fmt: skip


# R5, R4: the export record (the bytes are copied by the client, test_export.py).
def test_record_export(service: Service) -> None:
    alice, bob = _clients(service)
    build = finish(alice, {"compile[lib=core]": Action({"worklib": LIB})})
    rel = create_release(
        alice, name=NAME, version="1.0", build=build, outputs=["compile.worklib"], by="alice"
    )
    with pytest.raises(MetadataError, match="'bob' is not allowed"):
        bob.record_export(rel.id, "partner", by="bob")
    exported = alice.record_export(rel.id, "partner", by="alice")
    assert exported.exported_from == rel.id
    assert exported.domain == "partner"
    [audit] = bob.list_exports(rel.id)
    assert (audit.exported, audit.to_domain, audit.exported_by) == (exported.id, "partner", "alice")
