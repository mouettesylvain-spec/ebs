"""Archives: sign-off bundles of releases written to cold storage (P3-04).

Revision ID: 0015_archives
Revises: 0014_pilot_queue
Create Date: 2026-10-21

Rows are append-only (the `append_only()` trigger function of 0007).

Frozen: later schema changes go into new revisions, never into this file.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_archives"
down_revision: str | None = "0014_pilot_queue"
branch_labels: str | None = None
depends_on: str | None = None

S = "ebs"
TS = sa.DateTime(timezone=True)
DIGEST = "^(sha256|blake3):[0-9a-f]{64}$"


def upgrade() -> None:
    op.create_table(
        "archives",
        sa.Column("id", sa.BigInteger, sa.Identity(), nullable=False),
        sa.Column("release_id", sa.BigInteger, nullable=False),
        sa.Column("bundle_digest", sa.Text, nullable=False),
        sa.Column("location", sa.Text, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("created_by", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_archives")),
        sa.ForeignKeyConstraint(
            ["release_id"], [f"{S}.releases.id"], name=op.f("fk_archives_release_id")
        ),
        sa.CheckConstraint(
            f"bundle_digest ~ '{DIGEST}'", name=op.f("ck_archives_bundle_digest_digest")
        ),
        sa.CheckConstraint(
            "length(location) BETWEEN 1 AND 4096", name=op.f("ck_archives_location_length")
        ),
        schema=S,
    )
    op.create_index("ix_archives_release_id", "archives", ["release_id"], schema=S)
    op.execute(
        f"CREATE TRIGGER archives_append_only BEFORE UPDATE OR DELETE ON {S}.archives "
        f"FOR EACH ROW EXECUTE FUNCTION {S}.append_only()"
    )
    op.execute(  # TRUNCATE skips row triggers
        f"CREATE TRIGGER archives_no_truncate BEFORE TRUNCATE ON {S}.archives "
        f"FOR EACH STATEMENT EXECUTE FUNCTION {S}.append_only()"
    )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER archives_no_truncate ON {S}.archives")
    op.execute(f"DROP TRIGGER archives_append_only ON {S}.archives")
    op.drop_index("ix_archives_release_id", table_name="archives", schema=S)
    op.drop_table("archives", schema=S)
