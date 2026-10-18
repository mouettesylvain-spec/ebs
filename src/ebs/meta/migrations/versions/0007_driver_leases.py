"""Driver leases: one live driver per build (P2-08).

Revision ID: 0007_driver_leases
Revises: 0006_gc
Create Date: 2026-10-17

Frozen: later schema changes go into new revisions, never into this file.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_driver_leases"
down_revision: str | None = "0006_gc"
branch_labels: str | None = None
depends_on: str | None = None

S = "ebs"
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "driver_leases",
        sa.Column("build_id", sa.BigInteger, nullable=False),
        sa.Column("owner", sa.Text, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("claimed_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("build_id", name=op.f("pk_driver_leases")),
        sa.ForeignKeyConstraint(
            ["build_id"],
            [f"{S}.builds.id"],
            name=op.f("fk_driver_leases_build_id"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "length(owner) BETWEEN 1 AND 256", name=op.f("ck_driver_leases_owner_length")
        ),
        schema=S,
    )


def downgrade() -> None:
    op.drop_table("driver_leases", schema=S)
