"""Bearer tokens of the metadata service (P1-06).

CI jobs and services authenticate to the metadata service with bearer tokens; only the token's
SHA-256 is stored (`ebs.meta.service.tokens`).

Revision ID: 0004_api_tokens
Revises: 0003_skipped_action_result
Create Date: 2026-10-10

Frozen: later schema changes go into new revisions, never into this file.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_api_tokens"
down_revision: str | None = "0003_skipped_action_result"
branch_labels: str | None = None
depends_on: str | None = None

S = "ebs"
DIGEST = "^(sha256|blake3):[0-9a-f]{64}$"
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.BigInteger, sa.Identity(), nullable=False),
        sa.Column("principal", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("team", sa.Text, nullable=True),
        sa.Column("hash", sa.Text, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=True),
        sa.Column("revoked_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_tokens")),
        sa.UniqueConstraint("hash", name=op.f("uq_api_tokens_hash")),
        sa.CheckConstraint(
            "kind IN ('user', 'ci', 'service')", name=op.f("ck_api_tokens_kind_valid")
        ),
        sa.CheckConstraint(f"hash ~ '{DIGEST}'", name=op.f("ck_api_tokens_hash_digest")),
        schema=S,
    )
    op.create_index("ix_api_tokens_principal", "api_tokens", ["principal"], schema=S)


def downgrade() -> None:
    op.drop_index("ix_api_tokens_principal", table_name="api_tokens", schema=S)
    op.drop_table("api_tokens", schema=S)
