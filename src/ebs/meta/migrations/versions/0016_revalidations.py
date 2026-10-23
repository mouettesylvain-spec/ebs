"""Revalidations: reports of re-runs of a release's sign-off plan (P3-05).

Revision ID: 0016_revalidations
Revises: 0015_archives
Create Date: 2026-10-23

Rows are append-only (the `append_only()` trigger function of 0007).

Frozen: later schema changes go into new revisions, never into this file.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0016_revalidations"
down_revision: str | None = "0015_archives"
branch_labels: str | None = None
depends_on: str | None = None

S = "ebs"
TS = sa.DateTime(timezone=True)
DIGEST = "^(sha256|blake3):[0-9a-f]{64}$"


def upgrade() -> None:
    op.create_table(
        "revalidations",
        sa.Column("id", sa.BigInteger, sa.Identity(), nullable=False),
        sa.Column("release_id", sa.BigInteger, nullable=False),
        sa.Column("build_id", sa.BigInteger, nullable=True),
        sa.Column("verdict", sa.Text, nullable=False),
        sa.Column("report_digest", sa.Text, nullable=False),
        sa.Column("markdown_digest", sa.Text, nullable=False),
        sa.Column("toolchains", JSONB, nullable=False),
        sa.Column("counts", JSONB, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("created_by", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_revalidations")),
        sa.ForeignKeyConstraint(
            ["release_id"], [f"{S}.releases.id"], name=op.f("fk_revalidations_release_id")
        ),
        sa.ForeignKeyConstraint(
            ["build_id"], [f"{S}.builds.id"], name=op.f("fk_revalidations_build_id")
        ),
        sa.CheckConstraint(
            f"report_digest ~ '{DIGEST}'", name=op.f("ck_revalidations_report_digest_digest")
        ),
        sa.CheckConstraint(
            f"markdown_digest ~ '{DIGEST}'", name=op.f("ck_revalidations_markdown_digest_digest")
        ),
        sa.CheckConstraint(
            "verdict IN ('reproduced', 'differences', 'failed')",
            name=op.f("ck_revalidations_verdict_valid"),
        ),
        schema=S,
    )
    op.create_index("ix_revalidations_release_id", "revalidations", ["release_id"], schema=S)
    op.execute(
        f"CREATE TRIGGER revalidations_append_only BEFORE UPDATE OR DELETE ON {S}.revalidations "
        f"FOR EACH ROW EXECUTE FUNCTION {S}.append_only()"
    )
    op.execute(  # TRUNCATE skips row triggers
        f"CREATE TRIGGER revalidations_no_truncate BEFORE TRUNCATE ON {S}.revalidations "
        f"FOR EACH STATEMENT EXECUTE FUNCTION {S}.append_only()"
    )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER revalidations_no_truncate ON {S}.revalidations")
    op.execute(f"DROP TRIGGER revalidations_append_only ON {S}.revalidations")
    op.drop_index("ix_revalidations_release_id", table_name="revalidations", schema=S)
    op.drop_table("revalidations", schema=S)
