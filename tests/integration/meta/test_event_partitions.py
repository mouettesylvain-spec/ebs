"""The `events` table partitioned by month (P2-05 R6): routing, on-demand partitions, retention
and the 0008 migration of existing rows."""

from __future__ import annotations

import io
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import URL

from ebs.core.clock import FakeClock
from ebs.meta import migrations
from ebs.meta.api import Event
from ebs.meta.models import SCHEMA
from ebs.meta.pg import PgMetadataStore
from ebs.meta.service.__main__ import main
from tests.helpers.pg import fresh_database, url_string
from tests.helpers.release import finish

pytestmark = pytest.mark.integration


def _event(build: int, ts: datetime, type_: str = "running") -> Event:
    return Event(ts=ts, build=build, type=type_, data={})  # type: ignore[arg-type]


def _partitions(engine: sa.Engine) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
                "JOIN pg_class p ON p.oid = i.inhparent "
                "JOIN pg_namespace n ON n.oid = p.relnamespace "
                "WHERE p.relname = 'events' AND n.nspname = :s ORDER BY c.relname"
            ),
            {"s": SCHEMA},
        )
        return [r[0] for r in rows]


def _where(engine: sa.Engine) -> dict[int, str]:
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                f"SELECT e.id, c.relname FROM {SCHEMA}.events e "
                "JOIN pg_class c ON c.oid = e.tableoid"
            )
        )
        return {int(r[0]): str(r[1]) for r in rows}


@dataclass
class Pg:
    store: PgMetadataStore
    clock: FakeClock

    def at(self, when: datetime) -> None:
        """Move the store's clock (it only goes forward)."""
        self.clock.advance((when - self.clock.now()).total_seconds())


@pytest.fixture
def pg(pg_url: URL) -> Iterator[Pg]:
    clock = FakeClock()
    store = PgMetadataStore.from_config({"url": url_string(pg_url)}, clock=clock)
    try:
        yield Pg(store, clock)
    finally:
        store.close()


# R6: each event lands in the partition of the UTC month it was stored in (the store's clock),
# created on first use. Far-future years: the session database is shared with other tests.
def test_events_routed_to_monthly_partitions(pg: Pg) -> None:
    build = finish(pg.store, {}, status=None)
    for when in (datetime(2102, 3, 31, 23, 59, tzinfo=UTC), datetime(2102, 4, 1, tzinfo=UTC),
                 datetime(2102, 4, 20, tzinfo=UTC)):  # fmt: skip
        pg.at(when)
        pg.store.emit(_event(build, when))
    assert {"events_y2102m03", "events_y2102m04"} <= set(_partitions(pg.store.engine))
    placed = sorted(_where(pg.store.engine).items())
    assert [where for _, where in placed] == [
        "events_y2102m03", "events_y2102m04", "events_y2102m04",
    ]  # fmt: skip
    # Reading across partitions keeps the id order.
    found = pg.store.events_after(0, build=build)
    assert [e.event.ts.astimezone(UTC).month for e in found] == [3, 4, 4]


# R6: the event's own `ts` (the client's clock) never picks the partition: a skewed or forged
# timestamp cannot create partitions.
def test_client_ts_does_not_create_partitions(pg: Pg) -> None:
    build = finish(pg.store, {}, status=None)
    pg.at(datetime(2105, 6, 1, tzinfo=UTC))
    for year in (1, 1970, 9999):
        pg.store.emit(_event(build, datetime(year, 1, 1, tzinfo=UTC)))
    names = set(_partitions(pg.store.engine))
    assert not {"events_y0001m01", "events_y1970m01", "events_y9999m01"} & names
    assert set(_where(pg.store.engine).values()) == {"events_y2105m06"}
    assert [e.event.ts.year for e in pg.store.events_after(0, build=build)] == [1, 1970, 9999]


# R6: retention in another process drops a partition this store has written to; its next emit
# into that month recreates it instead of failing.
def test_emit_after_another_process_pruned(pg: Pg, pg_url: URL) -> None:
    build = finish(pg.store, {}, status=None)
    pg.at(datetime(2104, 5, 10, tzinfo=UTC))
    pg.store.emit(_event(build, pg.clock.now()))
    other = PgMetadataStore.from_config({"url": url_string(pg_url)}, clock=FakeClock())
    try:
        assert "2104-05" in other.prune_events(datetime(2104, 6, 1, tzinfo=UTC))
    finally:
        other.close()
    assert "events_y2104m05" not in _partitions(pg.store.engine)
    pg.store.emit(_event(build, pg.clock.now()))  # the cached partition is gone: recreated
    assert "events_y2104m05" in _partitions(pg.store.engine)
    assert len(pg.store.events_after(0, build=build)) == 1


# R2: a domain's emits are serialized until commit (ids commit in id order, so resuming after
# an id never skips a row); another domain's emits are not held up.
def test_emits_of_a_domain_are_serialized(pg: Pg) -> None:
    mine = finish(pg.store, {}, status=None)
    other = finish(pg.store, {}, status=None, domain="other")
    pg.store.emit(_event(mine, pg.clock.now()))  # the month's partition exists
    pg.store.emit(_event(other, pg.clock.now()))
    done = threading.Event()
    with pg.store.engine.connect() as holder:
        holder.execute(
            sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"{SCHEMA}.events:test"},
        )  # an uncommitted emit of domain "test" holds this lock

        def blocked() -> None:
            pg.store.emit(_event(mine, pg.clock.now()))
            done.set()

        thread = threading.Thread(target=blocked)
        thread.start()
        assert not done.wait(1.0), "an emit of the domain ran while another was uncommitted"
        pg.store.emit(_event(other, pg.clock.now()))  # another domain: not blocked
        holder.rollback()
    assert done.wait(20)
    thread.join()
    assert len(pg.store.events_after(0, build=mine)) == 2


# R6: the retention job drops whole partitions older than the retention period; the admin
# command reads it from [meta_service.events].retention_days and creates next month's partition.
def test_prune_command_drops_old_partitions(pg: Pg, pg_url: URL, tmp_path: Path) -> None:
    build = finish(pg.store, {}, status=None)
    for when in (datetime(2103, 1, 10, tzinfo=UTC), datetime(2103, 2, 10, tzinfo=UTC),
                 datetime(2103, 3, 10, tzinfo=UTC), datetime(2126, 1, 10, tzinfo=UTC)):  # fmt: skip
        pg.at(when)
        pg.store.emit(_event(build, when))
    config = tmp_path / "ebs.toml"
    config.write_text(
        f'[metadata]\nurl = "{url_string(pg_url)}"\n[meta_service.events]\nretention_days = 180\n'
    )
    env = {"EBS_CONFIG": str(config), "HOME": str(tmp_path)}
    clock = FakeClock(datetime(2103, 9, 15, tzinfo=UTC))  # cutoff: 2103-03-19
    out = io.StringIO()
    assert main(["events", "prune", "--dry-run"], environ=env, clock=clock, out=out) == 0
    # Other tests' old partitions may be listed too; this test's months must be, last.
    assert out.getvalue().startswith("would drop ")
    assert out.getvalue().endswith("2103-01, 2103-02\n")
    engine = pg.store.engine
    assert {"events_y2103m01", "events_y2103m02"} <= set(_partitions(engine))
    out = io.StringIO()
    assert main(["events", "prune"], environ=env, clock=clock, out=out) == 0
    assert out.getvalue().startswith("dropped ")
    assert out.getvalue().endswith("2103-01, 2103-02\n")
    names = set(_partitions(engine))
    assert not {"events_y2103m01", "events_y2103m02"} & names
    assert {"events_y2103m03", "events_y2126m01"} <= names
    assert "events_y2103m10" in names  # next month's partition exists before its first event
    assert [e.event.ts.year for e in pg.store.events_after(0, build=build)] == [2103, 2126]


# R6: upgrading to 0008 moves existing events into partitions with their ids; new ids follow.
def test_migration_keeps_existing_events(pg_server: URL) -> None:
    with fresh_database(pg_server) as url:
        migrations.upgrade(url_string(url), "0007_releases")
        engine = sa.create_engine(url, poolclass=sa.pool.NullPool)
        try:
            with engine.begin() as conn:
                conn.execute(
                    sa.text(
                        f"INSERT INTO {SCHEMA}.domains (name) VALUES ('test');"
                        f"INSERT INTO {SCHEMA}.builds (uuid, domain, project, plan_digest, "
                        "flow_repo, flow_commit, flow_dirty, user_name, cache_mode, status, "
                        "created_at) VALUES (gen_random_uuid(), 'test', 'demo', "
                        f"'sha256:{'a' * 64}', '', '', false, 'alice', 'write', 'running', now())"
                    )
                )
                for ts in ("2004-05-01T10:00:00Z", "2004-06-01T10:00:00Z"):
                    conn.execute(
                        sa.text(
                            f"INSERT INTO {SCHEMA}.events (build_id, ts, type, data) "
                            "SELECT id, CAST(:ts AS timestamptz), 'running', '{}' "
                            f"FROM {SCHEMA}.builds"
                        ),
                        {"ts": ts},
                    )
            before = _where(engine)
            migrations.upgrade(url_string(url))
            after = _where(engine)
            assert sorted(after) == sorted(before)
            assert sorted(after.values()) == [
                "events_y2004m05", "events_y2004m06",
            ]  # fmt: skip
            store = PgMetadataStore.from_config({"url": url_string(url)}, clock=FakeClock())
            try:
                build = store.events_after(0, domain="test")[0].event.build
                store.emit(_event(build, datetime(2004, 6, 2, tzinfo=UTC)))
                ids = [e.id for e in store.events_after(0, build=build)]
                assert ids[-1] > max(before)
            finally:
                store.close()
            migrations.downgrade(url_string(url), "0007_releases")
            assert sorted(_where(engine)) == sorted(ids)  # the downgrade keeps the rows too
        finally:
            engine.dispose()
