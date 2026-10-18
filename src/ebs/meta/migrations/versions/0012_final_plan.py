"""The final (refined) plan of a build, so a pinned build roots it (P1-09 follow-up).

Revision ID: 0012_final_plan
Revises: 0011_driver_leases
Create Date: 2026-10-18

Frozen: later schema changes go into new revisions, never into this file.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_final_plan"
down_revision: str | None = "0011_driver_leases"
branch_labels: str | None = None
depends_on: str | None = None

S = "ebs"
DIGEST = "^(sha256|blake3):[0-9a-f]{64}$"


def upgrade() -> None:
    op.add_column("builds", sa.Column("final_plan_digest", sa.Text, nullable=True), schema=S)
    op.create_check_constraint(
        op.f("ck_builds_final_plan_digest_digest"),
        "builds",
        f"final_plan_digest ~ '{DIGEST}'",
        schema=S,
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_builds_final_plan_digest_digest"), "builds", schema=S)
    op.drop_column("builds", "final_plan_digest", schema=S)
