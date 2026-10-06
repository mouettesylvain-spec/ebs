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
from ebs.meta.api import InfraReason
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
}
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
    assert _tables(empty_db) == P0_TABLES
    migrations.downgrade(url)
    engine = sa.create_engine(empty_db, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            assert SCHEMA not in sa.inspect(conn).get_schema_names()
    finally:
        engine.dispose()
    migrations.upgrade(url)  # the round trip leaves nothing behind that blocks a re-upgrade
    assert _tables(empty_db) == P0_TABLES


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
        "events": {"build_id": build_id, "ts": now, "type": "running", "data": "{}"},
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
