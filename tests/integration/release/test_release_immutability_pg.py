"""I19 in the database (P2-02 R2): releases are never deleted and only yanked once, whatever
SQL a client sends; channel history and export audit rows are append-only."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import URL

from ebs.core.clock import FakeClock
from ebs.meta.pg import PgMetadataStore
from ebs.release.channels import GatePolicy, move_channel
from ebs.release.releases import create_release
from tests.helpers.pg import url_string
from tests.helpers.release import Action, finish, out


@pytest.fixture
def pg(pg_url: URL) -> Iterator[PgMetadataStore]:
    store = PgMetadataStore.from_config({"url": url_string(pg_url)}, clock=FakeClock())
    try:
        yield store
    finally:
        store.close()


def _refused(pg: PgMetadataStore, sql: str, match: str) -> None:
    with pytest.raises(sa.exc.DBAPIError, match=match), pg.engine.begin() as conn:
        conn.execute(sa.text(sql))


# R2 (I19)
def test_no_update_no_delete(pg: PgMetadataStore) -> None:
    build = finish(pg, {"lint": Action({"report": out(b"lint")})})
    rel = create_release(
        pg, name="rtl-team/cpu", version="1.0", build=build, outputs=["lint.report"], by="alice"
    )
    move_channel(pg, GatePolicy(), rel, "stable", by="alice")
    pg.record_export(rel.id, "partner", by="alice")
    where = f"WHERE id = {int(rel.id)}"
    _refused(pg, f"DELETE FROM ebs.releases {where}", "never deleted")
    _refused(pg, f"UPDATE ebs.releases SET version = '2.0' {where}", "immutable")
    _refused(pg, f"UPDATE ebs.releases SET outputs = '{{}}' {where}", "immutable")
    _refused(pg, f"UPDATE ebs.releases SET notes = 'x' {where}", "immutable")
    _refused(pg, "UPDATE ebs.channel_history SET moved_by = 'eve'", "append-only")
    _refused(pg, "DELETE FROM ebs.channel_history", "append-only")
    _refused(pg, "UPDATE ebs.exports SET exported_by = 'eve'", "append-only")
    _refused(pg, "DELETE FROM ebs.exports", "append-only")
    for table in ("releases", "channel_history", "exports"):
        _refused(pg, f"TRUNCATE ebs.{table} CASCADE", "append-only")
    with pytest.raises(sa.exc.IntegrityError), pg.engine.begin() as conn:
        conn.execute(sa.text(f"DELETE FROM ebs.builds WHERE id = {int(build)}"))  # still referenced
    assert pg.get_release_by_id(rel.id) == rel


# R2 (I19): the yank fields may be set once, and nothing else with them.
def test_yank(pg: PgMetadataStore) -> None:
    build = finish(pg, {"lint": Action({"report": out(b"lint")})})
    rel = create_release(
        pg, name="rtl-team/cpu", version="1.0", build=build, outputs=["lint.report"], by="alice"
    )
    where = f"WHERE id = {int(rel.id)}"
    _refused(
        pg,
        f"UPDATE ebs.releases SET yanked_at = now(), yanked_by = 'bob', yank_reason = 'x', "
        f"notes = 'changed' {where}",
        "immutable",
    )
    yanked = pg.yank_release(rel.id, by="bob", reason="wrong waiver")
    assert yanked.yank_reason == "wrong waiver"
    _refused(pg, f"UPDATE ebs.releases SET yank_reason = 'other' {where}", "immutable")
    _refused(pg, f"UPDATE ebs.releases SET yanked_at = NULL {where}", "immutable")
    assert pg.yank_release(rel.id, by="carol", reason="again") == yanked


# R3: concurrent moves of one channel are serialized; the pointer is always the last move.
def test_concurrent_moves(pg: PgMetadataStore) -> None:
    from concurrent.futures import ThreadPoolExecutor

    build = finish(pg, {"lint": Action({"report": out(b"lint")})})
    rels = [
        create_release(
            pg, name="rtl-team/cpu", version=f"{i}.0", build=build, outputs=["lint.report"],
            by="alice",
        )
        for i in range(4)
    ]  # fmt: skip
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(
            lambda i: pg.set_channel("test", "rtl-team/cpu", "stable", rels[i % 4].id, by="a"),
            range(40),
        ))  # fmt: skip
    history = pg.channel_history("test", "rtl-team/cpu", "stable")
    assert len(history) == 40
    channel = pg.get_channel("test", "rtl-team/cpu", "stable")
    assert channel is not None
    assert channel.release.id == history[-1].release
