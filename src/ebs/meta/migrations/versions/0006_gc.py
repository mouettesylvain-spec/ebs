"""Garbage collection v1: leases, pins, tombstones and GC runs (P1-09).

Revision ID: 0006_gc
Revises: 0005_api_tokens
Create Date: 2026-10-14

Frozen: later schema changes go into new revisions, never into this file.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006_gc"
down_revision: str | None = "0005_api_tokens"
branch_labels: str | None = None
depends_on: str | None = None

S = "ebs"
DIGEST = "^(sha256|blake3):[0-9a-f]{64}$"
TS = sa.DateTime(timezone=True)


def _domain_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["domain"], [f"{S}.domains.name"], name=op.f(f"fk_{table}_domain")
    )


def _build_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["build_id"],
        [f"{S}.builds.id"],
        name=op.f(f"fk_{table}_build_id"),
        ondelete="CASCADE",
    )


def upgrade() -> None:
    op.create_table(
        "leases",
        sa.Column("id", sa.BigInteger, sa.Identity(), nullable=False),
        sa.Column("domain", sa.Text, nullable=False),
        sa.Column("build_id", sa.BigInteger, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_leases")),
        _domain_fk("leases"),
        _build_fk("leases"),
        schema=S,
    )
    op.create_index("ix_leases_domain_expires_at", "leases", ["domain", "expires_at"], schema=S)
    op.create_table(
        "lease_objects",
        sa.Column("lease_id", sa.BigInteger, nullable=False),
        sa.Column("digest", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("lease_id", "digest", name=op.f("pk_lease_objects")),
        sa.ForeignKeyConstraint(
            ["lease_id"],
            [f"{S}.leases.id"],
            name=op.f("fk_lease_objects_lease_id"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(f"digest ~ '{DIGEST}'", name=op.f("ck_lease_objects_digest_digest")),
        schema=S,
    )
    op.create_table(
        "pins",
        sa.Column("domain", sa.Text, nullable=False),
        sa.Column("build_id", sa.BigInteger, nullable=False),
        sa.Column("pinned_by", sa.Text, nullable=False),
        sa.Column("reason", sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column("created_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("domain", "build_id", name=op.f("pk_pins")),
        _domain_fk("pins"),
        _build_fk("pins"),
        schema=S,
    )
    op.create_table(
        "gc_runs",
        sa.Column("id", sa.BigInteger, sa.Identity(), nullable=False),
        sa.Column("domain", sa.Text, nullable=False),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("finished_at", TS, nullable=False),
        sa.Column("marked", sa.BigInteger, nullable=False),
        sa.Column("swept_rows", sa.BigInteger, nullable=False),
        sa.Column("swept_bytes", sa.BigInteger, nullable=False),
        sa.Column("dry_run", sa.Boolean, nullable=False),
        sa.Column("report", JSONB, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_gc_runs")),
        _domain_fk("gc_runs"),
        sa.CheckConstraint(
            "jsonb_typeof(report) = 'object'", name=op.f("ck_gc_runs_report_object")
        ),
        schema=S,
    )
    op.create_index("ix_gc_runs_domain_started_at", "gc_runs", ["domain", "started_at"], schema=S)
    op.create_table(
        "tombstones",
        sa.Column("domain", sa.Text, nullable=False),
        sa.Column("digest", sa.Text, nullable=False),
        sa.Column("deleted_rows_at", TS, nullable=False),
        sa.Column("blob_delete_after", TS, nullable=False),
        sa.PrimaryKeyConstraint("domain", "digest", name=op.f("pk_tombstones")),
        _domain_fk("tombstones"),
        sa.CheckConstraint(f"digest ~ '{DIGEST}'", name=op.f("ck_tombstones_digest_digest")),
        schema=S,
    )


def downgrade() -> None:
    op.drop_table("tombstones", schema=S)
    op.drop_index("ix_gc_runs_domain_started_at", table_name="gc_runs", schema=S)
    op.drop_table("gc_runs", schema=S)
    op.drop_table("pins", schema=S)
    op.drop_table("lease_objects", schema=S)
    op.drop_index("ix_leases_domain_expires_at", table_name="leases", schema=S)
    op.drop_table("leases", schema=S)
