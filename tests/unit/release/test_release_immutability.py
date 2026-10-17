"""Releases are immutable and never deleted, only yanked (P2-02 R2, invariant I19).

The PostgreSQL side (a trigger refuses UPDATE and DELETE) is in
tests/integration/release/test_release_immutability_pg.py.
"""

from __future__ import annotations

import inspect

import pydantic
import pytest

from ebs.core.clock import FakeClock
from ebs.core.errors import MetadataError, ReleaseError
from ebs.meta.api import ReleaseStore, ReleaseView
from ebs.meta.client import HttpMetadataStore
from ebs.meta.memory import InMemoryMetadataStore
from ebs.meta.pg import PgMetadataStore
from ebs.release.channels import GatePolicy, move_channel
from ebs.release.releases import create_release
from tests.helpers.release import Action, finish, out


def _released(store: InMemoryMetadataStore, version: str = "1.0") -> ReleaseView:
    build = finish(store, {"lint": Action({"report": out(b"lint")})})
    return create_release(
        store, name="rtl-team/cpu", version=version, build=build, outputs=["lint.report"],
        by="alice",
    )  # fmt: skip


# R2 (I19): no store offers a way to change or delete a release except `yank_release`.
def test_no_update_no_delete() -> None:
    writes = {
        n for n, _ in inspect.getmembers(ReleaseStore, inspect.isfunction)
        if not n.startswith(("get_", "list_", "channel_history", "gate_builds", "_"))
    }  # fmt: skip
    assert writes == {"create_release", "yank_release", "set_channel", "record_export"}
    for cls in (InMemoryMetadataStore, PgMetadataStore, HttpMetadataStore):
        names = {n for n in dir(cls) if "release" in n}
        assert not {n for n in names if n.startswith(("delete", "update", "remove", "drop"))}
    store = InMemoryMetadataStore(clock=FakeClock())
    rel = _released(store)
    with pytest.raises(pydantic.ValidationError):
        rel.version = "2.0"
    # Whatever a caller does with a returned view, the stored release is unchanged.
    rel.outputs.clear()
    stored = store.get_release_by_id(rel.id)
    assert stored is not None
    assert list(stored.outputs) == ["lint.report"]
    with pytest.raises(ReleaseError, match="already exists"):
        _released(store)


# R2 (I19): yank sets the yank fields once, keeps the release readable and listed, and stops
# channels from pointing at it.
def test_yank() -> None:
    clock = FakeClock()
    store = InMemoryMetadataStore(clock=clock)
    rel = _released(store)
    move_channel(store, GatePolicy(), rel, "stable", by="alice")
    clock.advance(60)
    yanked = store.yank_release(rel.id, by="bob", reason="wrong lint waiver")
    assert (yanked.yanked_by, yanked.yank_reason) == ("bob", "wrong lint waiver")
    assert yanked.yanked_at == clock.now()
    assert yanked.model_dump(exclude={"yanked_at", "yanked_by", "yank_reason"}) == rel.model_dump(
        exclude={"yanked_at", "yanked_by", "yank_reason"}
    )
    clock.advance(60)
    again = store.yank_release(rel.id, by="carol", reason="other")
    assert again == yanked  # the first yank is kept
    assert store.get_release("test", "rtl-team/cpu", "1.0") == yanked
    assert store.list_releases("test") == [yanked]
    with pytest.raises(ReleaseError, match="yanked"):
        move_channel(store, GatePolicy(), yanked, "stable", by="alice")
    with pytest.raises(MetadataError, match="yanked"):
        store.set_channel("test", "rtl-team/cpu", "nightly", rel.id, by="alice")
    with pytest.raises(MetadataError, match="no release"):
        store.yank_release(999, by="bob", reason="x")  # type: ignore[arg-type]
    with pytest.raises(MetadataError, match="reason"):
        store.yank_release(rel.id, by="bob", reason="")
    # The channel still shows where it pointed; its history is never rewritten.
    channel = store.get_channel("test", "rtl-team/cpu", "stable")
    assert channel is not None
    assert channel.release.yanked_at is not None
