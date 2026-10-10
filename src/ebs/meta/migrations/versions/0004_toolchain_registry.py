"""Toolchain registry: last use and fingerprint manifests (P1-07).

`ebs toolchain check --days N` re-fingerprints the toolchains used in the last N days
(`last_used_at`, touched by the registry resolver), and a drift report lists the changed files by
comparing fingerprint manifests (`toolchain_manifests`).

Revision ID: 0004_toolchain_registry
Revises: 0003_skipped_action_result
Create Date: 2026-10-10

Frozen: later schema changes go into new revisions, never into this file.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_toolchain_registry"
down_revision: str | None = "0003_skipped_action_result"
branch_labels: str | None = None
depends_on: str | None = None

S = "ebs"


def upgrade() -> None:
    op.add_column(
        "toolchains", sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True), schema=S
    )
    op.create_table(
        "toolchain_manifests",
        sa.Column("toolchain_id", sa.Text, nullable=False),
        sa.Column("entries", sa.LargeBinary, nullable=False),
        sa.PrimaryKeyConstraint("toolchain_id", name=op.f("pk_toolchain_manifests")),
        sa.ForeignKeyConstraint(
            ["toolchain_id"],
            [f"{S}.toolchains.id"],
            name=op.f("fk_toolchain_manifests_toolchain_id"),
            ondelete="CASCADE",
        ),
        schema=S,
    )


def downgrade() -> None:
    op.drop_table("toolchain_manifests", schema=S)
    op.drop_column("toolchains", "last_used_at", schema=S)
