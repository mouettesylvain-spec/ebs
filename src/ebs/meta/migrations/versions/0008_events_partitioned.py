"""Partition `events` by month for the event stream and its retention (P2-05 R6).

Revision ID: 0008_events_partitioned
Revises: 0007_releases
Create Date: 2026-10-17

`events` gains `stored_at`, the store's clock when the row was written, and becomes a table
partitioned by range of it, one partition per UTC month named `events_yYYYYmMM`, so retention
drops whole partitions instead of deleting rows. The partition key is the server's time, not the
event's `ts` (the client's clock): a skewed or forged `ts` cannot create partitions. Ids come
from the sequence `event_id_seq` (identity columns cannot span partitions) and the primary key is
(id, stored_at), as a partitioned table's key must include the partition key.
`ensure_events_partition` creates the partition of a timestamp's month if it is missing; the
store calls it before the first insert into a month. Existing rows keep their ids and get
`stored_at = ts`, the best record of when they were written.

Frozen: later schema changes go into new revisions, never into this file.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008_events_partitioned"
down_revision: str | None = "0007_releases"
branch_labels: str | None = None
depends_on: str | None = None

S = "ebs"
TS = sa.DateTime(timezone=True)
EVENT_TYPE = "^[a-z][a-z0-9_]*$"

_ENSURE_PARTITION = f"""
CREATE FUNCTION {S}.ensure_events_partition(at timestamptz) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE
    lo timestamp := date_trunc('month', at AT TIME ZONE 'UTC');
    part text := 'events_y' || to_char(lo, 'YYYY') || 'm' || to_char(lo, 'MM');
BEGIN
    IF to_regclass('{S}.' || part) IS NULL THEN
        -- One creator at a time; the others find the partition after the lock.
        PERFORM pg_advisory_xact_lock(hashtextextended('{S}.events partitions', 0));
        IF to_regclass('{S}.' || part) IS NULL THEN
            EXECUTE format(
                'CREATE TABLE {S}.%I PARTITION OF {S}.events FOR VALUES FROM (%L) TO (%L)',
                part, lo AT TIME ZONE 'UTC', (lo + interval '1 month') AT TIME ZONE 'UTC'
            );
        END IF;
    END IF;
    RETURN part;
END
$$
"""


def _rename(old: str, new: str) -> None:
    op.execute(f"ALTER TABLE {S}.{old} RENAME TO {new}")
    op.execute(f"ALTER INDEX {S}.pk_{old} RENAME TO pk_{new}")
    op.execute(f"ALTER INDEX {S}.ix_{old}_build_id_id RENAME TO ix_{new}_build_id_id")


def _columns(id_column: sa.Column[int], *, stored_at: bool) -> list[sa.schema.SchemaItem]:
    return [
        id_column,
        sa.Column("build_id", sa.BigInteger, nullable=False),
        sa.Column("ts", TS, nullable=False),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("action_id", sa.Text, nullable=True),
        sa.Column("data", JSONB, nullable=False),
        *([sa.Column("stored_at", TS, nullable=False)] if stored_at else []),
    ]


def _constraints(table: str) -> list[sa.schema.SchemaItem]:
    return [
        sa.ForeignKeyConstraint(
            ["build_id"], [f"{S}.builds.id"], name=op.f(f"fk_{table}_build_id"), ondelete="CASCADE"
        ),
        sa.CheckConstraint(f"type ~ '{EVENT_TYPE}'", name=op.f(f"ck_{table}_type_valid")),
        sa.CheckConstraint("jsonb_typeof(data) = 'object'", name=op.f(f"ck_{table}_data_object")),
    ]


def upgrade() -> None:
    _rename("events", "events_old")
    op.execute(f"CREATE SEQUENCE {S}.event_id_seq AS bigint")
    op.create_table(
        "events",
        *_columns(
            sa.Column(
                "id",
                sa.BigInteger,
                server_default=sa.text(f"nextval('{S}.event_id_seq'::regclass)"),
                nullable=False,
            ),
            stored_at=True,
        ),
        *_constraints("events"),
        sa.PrimaryKeyConstraint("id", "stored_at", name=op.f("pk_events")),
        schema=S,
        postgresql_partition_by="RANGE (stored_at)",
    )
    op.execute(f"ALTER SEQUENCE {S}.event_id_seq OWNED BY {S}.events.id")
    op.create_index("ix_events_build_id_id", "events", ["build_id", "id"], schema=S)
    op.execute(_ENSURE_PARTITION)
    op.execute(
        f"SELECT {S}.ensure_events_partition(m) FROM "
        f"(SELECT DISTINCT date_trunc('month', ts AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' AS m "
        f"FROM {S}.events_old) months"
    )
    op.execute(
        f"INSERT INTO {S}.events (id, build_id, ts, type, action_id, data, stored_at) "
        f"SELECT id, build_id, ts, type, action_id, data, ts FROM {S}.events_old"
    )
    op.execute(
        f"SELECT setval('{S}.event_id_seq', COALESCE((SELECT max(id) FROM {S}.events), 0) + 1, "
        "false)"
    )
    op.drop_table("events_old", schema=S)


def downgrade() -> None:
    _rename("events", "events_new")
    op.create_table(
        "events",
        *_columns(sa.Column("id", sa.BigInteger, sa.Identity(), nullable=False), stored_at=False),
        *_constraints("events"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
        schema=S,
    )
    op.create_index("ix_events_build_id_id", "events", ["build_id", "id"], schema=S)
    op.execute(
        f"INSERT INTO {S}.events (id, build_id, ts, type, action_id, data) "
        f"SELECT id, build_id, ts, type, action_id, data FROM {S}.events_new"
    )
    op.execute(
        f"SELECT setval(pg_get_serial_sequence('{S}.events', 'id'), "
        f"COALESCE((SELECT max(id) FROM {S}.events), 0) + 1, false)"
    )
    op.drop_table("events_new", schema=S)  # its partitions and sequence go with it
    op.execute(f"DROP FUNCTION {S}.ensure_events_partition(timestamptz)")
