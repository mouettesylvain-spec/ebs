"""Alembic migrations: upgrade/downgrade and model drift (R1); DB CHECK constraints (R8)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import get_args

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy.engine import URL

from ebs.meta import migrations
from ebs.meta.api import ActionState, InfraReason
from ebs.meta.models import SCHEMA, metadata
from tests.helpers.pg import fresh_database, url_string

P0_TABLES = {
    "domains",
    "builds",
    "actions",
    "action_cache",
    "blobs",
    "provenance_edges",
    "toolchains",
    "events",
    "api_tokens",  # P1-06 (revision 0005)
}
P1_TABLES = {"toolchain_manifests"}  # P1-07
GC_TABLES = {"leases", "lease_objects", "pins", "gc_runs", "tombstones"}  # P1-09 (revision 0006)
P1_TABLES |= GC_TABLES
RELEASE_TABLES = {"releases", "channels", "channel_history", "exports"}  # P2-02 (revision 0007)
IMPORT_TABLES = {"release_imports"}  # P2-03 (revision 0009)
DRIVER_TABLES = {"driver_leases"}  # P2-08 (revision 0011)
GC_LOCK_TABLES = {"gc_locks"}  # P1-09 follow-up (revision 0013)
P2_TABLES = RELEASE_TABLES | IMPORT_TABLES | DRIVER_TABLES | GC_LOCK_TABLES
P3_TABLES = {"pilot_queue", "archives", "revalidations"}  # P3-06, P3-04, P3-05 (0014-0016)
GOOD = "sha256:" + "a" * 64


@pytest.fixture
def empty_db(pg_server: URL) -> Iterator[URL]:
    with fresh_database(pg_server) as url:
        yield url


def _tables(url: URL) -> set[str]:
    engine = sa.create_engine(url, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            insp = sa.inspect(conn)
            if SCHEMA not in insp.get_schema_names():
                return set()
            return set(insp.get_table_names(schema=SCHEMA))
    finally:
        engine.dispose()


# R1
def test_upgrade_downgrade(empty_db: URL) -> None:
    url = url_string(empty_db)
    assert _tables(empty_db) == set()
    migrations.upgrade(url)
    assert _tables(empty_db) == P0_TABLES | P1_TABLES | P2_TABLES | P3_TABLES
    migrations.downgrade(url)
    engine = sa.create_engine(empty_db, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            assert SCHEMA not in sa.inspect(conn).get_schema_names()
    finally:
        engine.dispose()
    migrations.upgrade(url)  # the round trip leaves nothing behind that blocks a re-upgrade
    assert _tables(empty_db) == P0_TABLES | P1_TABLES | P2_TABLES | P3_TABLES


# R1: a deployment may pre-create schema `ebs`, and a role named `ebs` then has it first in its
# search_path ("$user", public); the version table must still go to `public` so that
# `downgrade base` can drop the schema.
def test_version_table_stays_in_public(empty_db: URL) -> None:
    engine = sa.create_engine(empty_db, poolclass=sa.pool.NullPool)
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(f"CREATE SCHEMA {SCHEMA}"))
            role: str = conn.execute(sa.text("SELECT current_user")).scalar_one()
            conn.execute(
                sa.text(
                    f'ALTER ROLE "{role}" IN DATABASE "{empty_db.database}" '
                    f"SET search_path TO {SCHEMA}, public"
                )
            )
        url = url_string(empty_db)
        migrations.upgrade(url)
        with engine.connect() as conn:
            assert "alembic_version" in sa.inspect(conn).get_table_names(schema="public")
        migrations.downgrade(url)
        with engine.connect() as conn:
            assert SCHEMA not in sa.inspect(conn).get_schema_names()
    finally:
        engine.dispose()


# R1
def test_no_model_drift(empty_db: URL) -> None:
    migrations.upgrade(url_string(empty_db))
    # Alembic reports the default schema as None; for a role named `ebs` that is `ebs` itself
    # ("$user" in search_path). Pin search_path at connect time (the dialect caches the default
    # schema on first connect) so `ebs` is always compared by name.
    engine = sa.create_engine(
        empty_db,
        poolclass=sa.pool.NullPool,
        connect_args={"options": "-c search_path=public"},
    )
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(
                conn,
                opts={
                    "include_schemas": True,
                    "include_name": lambda name, type_, parent: type_ != "schema" or name == SCHEMA,
                    "compare_type": True,
                    "compare_server_default": True,
                },
            )
            assert compare_metadata(ctx, metadata) == []
            # autogenerate ignores CHECK constraints; compare their names explicitly.
            insp = sa.inspect(conn)
            for table in metadata.sorted_tables:
                reflected = {c["name"] for c in insp.get_check_constraints(table.name, SCHEMA)}
                declared = {
                    str(c.name) for c in table.constraints if isinstance(c, sa.CheckConstraint)
                }
                assert reflected == declared, table.name
    finally:
        engine.dispose()


BAD_ROWS = [
    ("bad plan digest", "builds", {"plan_digest": "sha256:ABC"}),
    ("unknown cache mode", "builds", {"cache_mode": "sometimes"}),
    ("unknown build status", "builds", {"status": "fine"}),
    ("bad action key", "actions", {"key": "md5:" + "a" * 32}),
    ("bad action state", "actions", {"state": "sleeping"}),
    ("negative attempts", "actions", {"attempts": -1}),
    ("bad pending reason", "actions", {"pending_reason": "coffee"}),
    ("bad cache key", "action_cache", {"key": "sha256:" + "g" * 64}),
    ("cache result not object", "action_cache", {"result": "[]"}),
    ("cache result wrong version", "action_cache", {"result": '{"v": 2}'}),
    ("negative hits", "action_cache", {"hits": -1}),
    ("bad blob kind", "blobs", {"kind": "dir"}),
    ("digest with trailing newline", "blobs", {"digest": GOOD + "\n"}),
    ("bad edge direction", "provenance_edges", {"direction": "sideways"}),
    ("bad edge object id", "provenance_edges", {"object_id": "nope"}),
    ("bad event type", "events", {"type": "Not A Type"}),
    ("bad domain name", "domains", {"name": "../x"}),
]


def _valid_row(conn: sa.Connection, table: str) -> dict[str, object]:
    conn.execute(sa.text("INSERT INTO ebs.domains (name) VALUES ('d') ON CONFLICT DO NOTHING"))
    now = "2026-01-01T00:00:00Z"
    if table == "domains":
        return {"name": "d2"}
    build = {
        "uuid": "00000000-0000-0000-0000-000000000001",
        "domain": "d",
        "project": "p",
        "plan_digest": GOOD,
        "flow_repo": "r",
        "flow_commit": "c",
        "flow_dirty": False,
        "user_name": "u",
        "cache_mode": "read",
        "status": "running",
        "created_at": now,
    }
    if table == "builds":
        return build
    build_id: int = conn.execute(
        sa.text(
            "INSERT INTO ebs.builds (uuid, domain, project, plan_digest, flow_repo, flow_commit, "
            "flow_dirty, user_name, cache_mode, status, created_at) VALUES (:uuid, :domain, "
            ":project, :plan_digest, :flow_repo, :flow_commit, :flow_dirty, :user_name, "
            ":cache_mode, :status, :created_at) RETURNING id"
        ),
        build,
    ).scalar_one()
    rows: dict[str, dict[str, object]] = {
        "actions": {
            "build_id": build_id,
            "action_id": "a",
            "step": "s",
            "state": "queued",
            "queued_at": now,
        },
        "action_cache": {
            "domain": "d",
            "key": GOOD,
            "result": '{"v": 1}',
            "created_at": now,
            "last_access": now,
        },
        "blobs": {
            "domain": "d",
            "digest": GOOD,
            "size": 1,
            "kind": "file",
            "created_at": now,
            "last_access": now,
        },
        "provenance_edges": {
            "domain": "d",
            "action_key": GOOD,
            "direction": "in",
            "logical_path": "x",
            "object_id": GOOD,
        },
        # 0008 (P2-05): stored_at is the partition key; the row needs its month's partition.
        "events": {
            "build_id": build_id,
            "ts": now,
            "type": "running",
            "data": "{}",
            "stored_at": conn.execute(
                sa.text(
                    f"SELECT CAST(:t AS timestamptz) FROM {SCHEMA}.ensure_events_partition(:t)"
                ),
                {"t": now},
            ).scalar_one(),
        },
    }
    return rows[table]


def _insert(conn: sa.Connection, table: str, row: dict[str, object]) -> None:
    cols = ", ".join(row)
    vals = ", ".join(f"CAST(:{c} AS jsonb)" if c in {"result", "data"} else f":{c}" for c in row)
    conn.execute(sa.text(f"INSERT INTO ebs.{table} ({cols}) VALUES ({vals})"), row)


# R8
@pytest.mark.parametrize(("case", "table", "override"), BAD_ROWS, ids=[r[0] for r in BAD_ROWS])
def test_check_constraints_reject_bad_rows(
    pg_url: URL, case: str, table: str, override: dict[str, object]
) -> None:
    del case
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            _insert(conn, table, _valid_row(conn, table))  # the baseline row is accepted
            conn.rollback()
            row = {**_valid_row(conn, table), **override}
            with pytest.raises(sa.exc.IntegrityError, match="check constraint"):
                _insert(conn, table, row)
            conn.rollback()
    finally:
        engine.dispose()


def _infra_row(conn: sa.Connection, reason: str) -> dict[str, object]:
    return {**_valid_row(conn, "actions"), "state": "infra_failed", "infra_reason": reason}


# P0-14: revision 0002 adds infra reason `input_verification` (runner exit 76); every reason the
# API allows passes the CHECK constraint, anything else does not.
@pytest.mark.parametrize("reason", [*get_args(InfraReason), "coffee"])
def test_infra_reasons_match_api(pg_url: URL, reason: str) -> None:
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            row = _infra_row(conn, reason)
            if reason in get_args(InfraReason):
                _insert(conn, "actions", row)
            else:
                with pytest.raises(sa.exc.IntegrityError, match="check constraint"):
                    _insert(conn, "actions", row)
            conn.rollback()
    finally:
        engine.dispose()


# P0-14: downgrading 0002 restores the 0001 constraint.
def test_downgrade_0002(empty_db: URL) -> None:
    url = url_string(empty_db)
    migrations.upgrade(url)
    engine = sa.create_engine(empty_db, poolclass=sa.pool.NullPool)
    try:
        with engine.begin() as conn:  # a row only 0002 allows is rewritten, not lost
            row = _infra_row(conn, "input_verification")
            _insert(conn, "actions", row)
        migrations.downgrade(url, "0001_initial")
        with engine.connect() as conn:
            rows = conn.execute(sa.text("SELECT infra_reason FROM ebs.actions"))
            reasons: list[str] = list(rows.scalars())
            assert reasons == ["other"]
            # Reuse the committed build: `_valid_row` would insert its fixed-uuid build again.
            with pytest.raises(sa.exc.IntegrityError, match="check constraint"):
                _insert(conn, "actions", {**row, "action_id": "b"})
            conn.rollback()
    finally:
        engine.dispose()
    migrations.upgrade(url)


# P0-15: revision 0003 adds action state `skipped`; every state the API allows passes the CHECK.
@pytest.mark.parametrize("state", [*get_args(ActionState), "sleeping"])
def test_action_states_match_api(pg_url: URL, state: str) -> None:
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            row = {**_valid_row(conn, "actions"), "state": state}
            if state in get_args(ActionState):
                _insert(conn, "actions", row)
            else:
                with pytest.raises(sa.exc.IntegrityError, match="check constraint"):
                    _insert(conn, "actions", row)
            conn.rollback()
    finally:
        engine.dispose()


# P0-15: revision 0003 stores the result manifest on the action row; it must be a JSON object.
def test_action_result_must_be_an_object(pg_url: URL) -> None:
    engine = sa.create_engine(pg_url, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            row = _valid_row(conn, "actions")
            _insert(conn, "actions", {**row, "result": '{"v": 1}'})
            with pytest.raises(sa.exc.IntegrityError, match="check constraint"):
                _insert(conn, "actions", {**row, "action_id": "b", "result": "[1]"})
            conn.rollback()
    finally:
        engine.dispose()


# P0-15: downgrading 0003 maps `skipped` to `cancelled` and drops the stored results.
def test_downgrade_0003(empty_db: URL) -> None:
    url = url_string(empty_db)
    migrations.upgrade(url)
    engine = sa.create_engine(empty_db, poolclass=sa.pool.NullPool)
    try:
        with engine.begin() as conn:
            row = {**_valid_row(conn, "actions"), "state": "skipped", "result": '{"v": 1}'}
            _insert(conn, "actions", row)
        migrations.downgrade(url, "0002_infra_input_verification")
        with engine.connect() as conn:
            found = conn.execute(sa.text("SELECT state FROM ebs.actions"))
            states: list[str] = list(found.scalars())
            assert states == ["cancelled"]
            columns = {c["name"] for c in sa.inspect(conn).get_columns("actions", schema=SCHEMA)}
            assert "result" not in columns
            with pytest.raises(sa.exc.IntegrityError, match="check constraint"):
                _insert(conn, "actions", {**_without(row, "result"), "action_id": "b"})
            conn.rollback()
    finally:
        engine.dispose()
    migrations.upgrade(url)


def _without(row: dict[str, object], key: str) -> dict[str, object]:
    return {k: v for k, v in row.items() if k != key}


# P1-09: revision 0006 upgrades from 0005 and downgrades back to it, leaving the P0 data alone.
def test_gc_revision_round_trip(empty_db: URL) -> None:
    url = url_string(empty_db)
    migrations.upgrade(url, "0005_api_tokens")
    assert not GC_TABLES & _tables(empty_db)
    migrations.upgrade(url, "0006_gc")
    assert _tables(empty_db) >= GC_TABLES
    migrations.downgrade(url, "0005_api_tokens")
    assert not GC_TABLES & _tables(empty_db)
    assert _tables(empty_db) >= P0_TABLES


# P2-02: revision 0007 upgrades from 0006 and downgrades back to it, dropping its triggers and
# functions, so a re-upgrade works.
def test_release_revision_round_trip(empty_db: URL) -> None:
    url = url_string(empty_db)
    migrations.upgrade(url, "0006_gc")
    assert not P2_TABLES & _tables(empty_db)
    migrations.upgrade(url, "0007_releases")
    assert _tables(empty_db) >= RELEASE_TABLES
    assert not IMPORT_TABLES & _tables(empty_db)
    migrations.downgrade(url, "0006_gc")
    assert not P2_TABLES & _tables(empty_db)
    assert _tables(empty_db) >= GC_TABLES
    migrations.upgrade(url, "0007_releases")
    assert _tables(empty_db) >= RELEASE_TABLES


# P2-03: revision 0009 upgrades from 0008 and downgrades back to it (its triggers go with the
# table), so a re-upgrade works; the rows are append-only.
def test_release_imports_revision_round_trip(empty_db: URL) -> None:
    url = url_string(empty_db)
    migrations.upgrade(url, "0008_events_partitioned")
    assert not IMPORT_TABLES & _tables(empty_db)
    migrations.upgrade(url, "0009_release_imports")
    assert _tables(empty_db) >= IMPORT_TABLES
    engine = sa.create_engine(empty_db, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn, pytest.raises(sa.exc.DBAPIError, match="append-only"):
            conn.execute(sa.text("TRUNCATE ebs.release_imports"))
    finally:
        engine.dispose()
    migrations.downgrade(url, "0008_events_partitioned")
    assert not IMPORT_TABLES & _tables(empty_db)
    assert _tables(empty_db) >= RELEASE_TABLES
    migrations.upgrade(url, "0009_release_imports")
    assert _tables(empty_db) >= IMPORT_TABLES


# P2-04: revision 0010 adds the actions' rule, params and toolchain and drops them on the way
# back to 0009.
def test_action_provenance_revision_round_trip(empty_db: URL) -> None:
    url = url_string(empty_db)
    added = {"rule", "params", "toolchain"}
    migrations.upgrade(url, "0009_release_imports")
    assert not added & _columns(empty_db, "actions")
    migrations.upgrade(url, "0010_action_provenance")
    assert added <= _columns(empty_db, "actions")
    migrations.downgrade(url, "0009_release_imports")
    assert not added & _columns(empty_db, "actions")
    migrations.upgrade(url, "0010_action_provenance")
    assert added <= _columns(empty_db, "actions")


def _columns(url: URL, table: str) -> set[str]:
    engine = sa.create_engine(url, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            return {c["name"] for c in sa.inspect(conn).get_columns(table, schema=SCHEMA)}
    finally:
        engine.dispose()


# P2-08: revision 0011 adds the driver leases and downgrades back to 0010.
def test_driver_lease_revision_round_trip(empty_db: URL) -> None:
    url = url_string(empty_db)
    migrations.upgrade(url, "0010_action_provenance")
    assert not DRIVER_TABLES & _tables(empty_db)
    migrations.upgrade(url, "0011_driver_leases")
    assert _tables(empty_db) >= DRIVER_TABLES
    migrations.downgrade(url, "0010_action_provenance")
    assert not DRIVER_TABLES & _tables(empty_db)
    assert _tables(empty_db) >= P0_TABLES | P1_TABLES | RELEASE_TABLES | IMPORT_TABLES


# P1-09 follow-up: revision 0012 adds builds.final_plan_digest (checked as a digest) and drops
# it on the way back to 0011.
def test_final_plan_revision_round_trip(empty_db: URL) -> None:
    url = url_string(empty_db)
    migrations.upgrade(url, "0011_driver_leases")
    assert "final_plan_digest" not in _columns(empty_db, "builds")
    migrations.upgrade(url, "0012_final_plan")
    assert "final_plan_digest" in _columns(empty_db, "builds")
    engine = sa.create_engine(empty_db, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            conn.execute(sa.text("INSERT INTO ebs.domains (name) VALUES ('t')"))
            with pytest.raises(sa.exc.IntegrityError, match="final_plan_digest"):
                conn.execute(
                    sa.text(
                        "INSERT INTO ebs.builds (uuid, domain, project, plan_digest, flow_repo, "
                        "flow_commit, flow_dirty, user_name, cache_mode, status, created_at, "
                        "final_plan_digest) VALUES (gen_random_uuid(), 't', 'p', :d, '', '', "
                        "false, 'u', 'write', 'running', now(), 'not a digest')"
                    ),
                    {"d": "sha256:" + "0" * 64},
                )
    finally:
        engine.dispose()
    migrations.downgrade(url, "0011_driver_leases")
    assert "final_plan_digest" not in _columns(empty_db, "builds")


# P1-09 follow-up: revision 0013 adds the GC locks and downgrades back to 0012.
def test_gc_lock_revision_round_trip(empty_db: URL) -> None:
    url = url_string(empty_db)
    migrations.upgrade(url, "0012_final_plan")
    assert "gc_locks" not in _tables(empty_db)
    migrations.upgrade(url, "0013_gc_locks")
    assert "gc_locks" in _tables(empty_db)
    migrations.downgrade(url, "0012_final_plan")
    assert "gc_locks" not in _tables(empty_db)


# P3-06: revision 0014 adds the pilot queue and downgrades back to 0013.
def test_pilot_queue_revision_round_trip(empty_db: URL) -> None:
    url = url_string(empty_db)
    migrations.upgrade(url, "0013_gc_locks")
    assert "pilot_queue" not in _tables(empty_db)
    migrations.upgrade(url, "0014_pilot_queue")
    assert "pilot_queue" in _tables(empty_db)
    migrations.downgrade(url, "0013_gc_locks")
    assert "pilot_queue" not in _tables(empty_db)
