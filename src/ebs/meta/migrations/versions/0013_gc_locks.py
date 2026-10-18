"""GC locks: one `ebs gc` run per domain at a time (P1-09 follow-up).

Revision ID: 0013_gc_locks
Revises: 0012_final_plan
Create Date: 2026-10-18

Frozen: later schema changes go into new revisions, never into this file.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_gc_locks"
down_revision: str | None = "0012_final_plan"
branch_labels: str | None = None
depends_on: str | None = None

S = "ebs"
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "gc_locks",
        sa.Column("domain", sa.Text, nullable=False),
        sa.Column("owner", sa.Text, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("domain", name=op.f("pk_gc_locks")),
        sa.CheckConstraint(
            "length(owner) BETWEEN 1 AND 256", name=op.f("ck_gc_locks_owner_length")
        ),
        schema=S,
    )


def downgrade() -> None:
    op.drop_table("gc_locks", schema=S)
