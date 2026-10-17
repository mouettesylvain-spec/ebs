"""Releases, channels, channel history and exports (P2-02).

Revision ID: 0007_releases
Revises: 0006_gc
Create Date: 2026-10-17

Triggers enforce invariant I19 in the database itself: a release row is never deleted and only
its yank fields may be set, once; channel history and export audit rows are append-only.

Frozen: later schema changes go into new revisions, never into this file.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007_releases"
down_revision: str | None = "0006_gc"
branch_labels: str | None = None
depends_on: str | None = None

S = "ebs"
TS = sa.DateTime(timezone=True)
RELEASE_NAME = "^[a-z0-9][a-z0-9._-]{0,63}(/[a-z0-9][a-z0-9._-]{0,63})?$"
VERSION = "^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$"
CHANNEL = "^[a-z][a-z0-9_-]{0,63}$"

_RELEASES_IMMUTABLE = f"""
CREATE FUNCTION {S}.releases_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    yank text[] := ARRAY['yanked_at', 'yanked_by', 'yank_reason'];
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'releases are never deleted (I19): yank release % instead', OLD.id;
    END IF;
    IF OLD.yanked_at IS NOT NULL OR (to_jsonb(NEW) - yank) <> (to_jsonb(OLD) - yank) THEN
        RAISE EXCEPTION
            'releases are immutable (I19): only the yank fields of release % may be set, once',
            OLD.id;
    END IF;
    RETURN NEW;
END $$
"""

_APPEND_ONLY = f"""
CREATE FUNCTION {S}.append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% rows are append-only (P2-02): % refused', TG_TABLE_NAME, TG_OP;
END $$
"""


def _fk(column: str, table: str, target: str) -> sa.ForeignKeyConstraint:
    ref = f"{S}.{target}"
    return sa.ForeignKeyConstraint([column], [ref], name=op.f(f"fk_{table}_{column}"))


def upgrade() -> None:
    op.create_table(
        "releases",
        sa.Column("id", sa.BigInteger, sa.Identity(), nullable=False),
        sa.Column("domain", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("version", sa.Text, nullable=False),
        sa.Column("build_id", sa.BigInteger, nullable=False),
        sa.Column("outputs", JSONB, nullable=False),
        sa.Column("notes", sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column("allow_failed", sa.Text, nullable=True),
        sa.Column("attestation", sa.Text, nullable=True),
        sa.Column("signature", sa.Text, nullable=True),
        sa.Column("exported_from", sa.BigInteger, nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("created_by", sa.Text, nullable=False),
        sa.Column("yanked_at", TS, nullable=True),
        sa.Column("yanked_by", sa.Text, nullable=True),
        sa.Column("yank_reason", sa.Text, nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_releases")),
        sa.UniqueConstraint(
            "domain", "name", "version", name=op.f("uq_releases_domain_name_version")
        ),
        _fk("domain", "releases", "domains.name"),
        _fk("build_id", "releases", "builds.id"),
        _fk("exported_from", "releases", "releases.id"),
        sa.CheckConstraint(f"name ~ '{RELEASE_NAME}'", name=op.f("ck_releases_name_valid")),
        sa.CheckConstraint(f"version ~ '{VERSION}'", name=op.f("ck_releases_version_valid")),
        sa.CheckConstraint(
            "jsonb_typeof(outputs) = 'object'", name=op.f("ck_releases_outputs_object")
        ),
        sa.CheckConstraint(
            "(yanked_at IS NULL) = (yanked_by IS NULL) AND "
            "(yanked_at IS NULL) = (yank_reason IS NULL)",
            name=op.f("ck_releases_yank_fields"),
        ),
        schema=S,
    )
    op.create_table(
        "channel_history",
        sa.Column("id", sa.BigInteger, sa.Identity(), nullable=False),
        sa.Column("domain", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("channel", sa.Text, nullable=False),
        sa.Column("release_id", sa.BigInteger, nullable=False),
        sa.Column("moved_at", TS, nullable=False),
        sa.Column("moved_by", sa.Text, nullable=False),
        sa.Column("gate_flow", sa.Text, nullable=True),
        sa.Column("gate_build_id", sa.BigInteger, nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_channel_history")),
        _fk("domain", "channel_history", "domains.name"),
        _fk("release_id", "channel_history", "releases.id"),
        _fk("gate_build_id", "channel_history", "builds.id"),
        sa.CheckConstraint(f"channel ~ '{CHANNEL}'", name=op.f("ck_channel_history_channel_valid")),
        sa.CheckConstraint(
            "(gate_flow IS NULL) = (gate_build_id IS NULL)",
            name=op.f("ck_channel_history_gate_fields"),
        ),
        schema=S,
    )
    op.create_index(
        "ix_channel_history_domain_name_channel_id",
        "channel_history",
        ["domain", "name", "channel", "id"],
        schema=S,
    )
    op.create_table(
        "channels",
        sa.Column("domain", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("channel", sa.Text, nullable=False),
        sa.Column("release_id", sa.BigInteger, nullable=False),
        sa.Column("move_id", sa.BigInteger, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("updated_by", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("domain", "name", "channel", name=op.f("pk_channels")),
        _fk("domain", "channels", "domains.name"),
        _fk("release_id", "channels", "releases.id"),
        _fk("move_id", "channels", "channel_history.id"),
        schema=S,
    )
    op.create_table(
        "exports",
        sa.Column("id", sa.BigInteger, sa.Identity(), nullable=False),
        sa.Column("release_id", sa.BigInteger, nullable=False),
        sa.Column("exported_release_id", sa.BigInteger, nullable=False),
        sa.Column("to_domain", sa.Text, nullable=False),
        sa.Column("exported_at", TS, nullable=False),
        sa.Column("exported_by", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_exports")),
        _fk("release_id", "exports", "releases.id"),
        _fk("exported_release_id", "exports", "releases.id"),
        _fk("to_domain", "exports", "domains.name"),
        schema=S,
    )
    op.create_index("ix_exports_release_id", "exports", ["release_id"], schema=S)
    op.execute(_RELEASES_IMMUTABLE)
    op.execute(_APPEND_ONLY)
    op.execute(
        f"CREATE TRIGGER releases_immutable BEFORE UPDATE OR DELETE ON {S}.releases "
        f"FOR EACH ROW EXECUTE FUNCTION {S}.releases_immutable()"
    )
    for table in ("channel_history", "exports"):
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {S}.{table} "
            f"FOR EACH ROW EXECUTE FUNCTION {S}.append_only()"
        )
    for table in ("releases", "channel_history", "exports"):  # TRUNCATE skips row triggers
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {S}.{table} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION {S}.append_only()"
        )


def downgrade() -> None:
    op.drop_index("ix_exports_release_id", table_name="exports", schema=S)
    op.drop_table("exports", schema=S)
    op.drop_table("channels", schema=S)
    op.drop_index(
        "ix_channel_history_domain_name_channel_id", table_name="channel_history", schema=S
    )
    op.drop_table("channel_history", schema=S)
    op.drop_table("releases", schema=S)
    op.execute(f"DROP FUNCTION {S}.append_only()")
    op.execute(f"DROP FUNCTION {S}.releases_immutable()")
