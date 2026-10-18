"""What each action is, for provenance queries: rule kind, params and toolchain (P2-04).

`ebs why` shows an action's rule, params and toolchain, and `ebs tests` selects actions whose
rule kind is `*.sim`, from the database alone (the service has no CAS access to read plans).
Rows written before this revision hold NULL.

Revision ID: 0008_action_provenance
Revises: 0007_releases
Create Date: 2026-10-17

Frozen: later schema changes go into new revisions, never into this file.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008_action_provenance"
down_revision: str | None = "0007_releases"
branch_labels: str | None = None
depends_on: str | None = None

S = "ebs"
DIGEST = "^(sha256|blake3):[0-9a-f]{64}$"


def upgrade() -> None:
    op.add_column("actions", sa.Column("rule", sa.Text, nullable=True), schema=S)
    op.add_column("actions", sa.Column("params", JSONB, nullable=True), schema=S)
    op.add_column("actions", sa.Column("toolchain", sa.Text, nullable=True), schema=S)
    op.create_check_constraint(
        op.f("ck_actions_toolchain_digest"), "actions", f"toolchain ~ '{DIGEST}'", schema=S
    )
    op.create_check_constraint(
        op.f("ck_actions_params_object"),
        "actions",
        "params IS NULL OR jsonb_typeof(params) = 'object'",
        schema=S,
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_actions_params_object"), "actions", type_="check", schema=S)
    op.drop_constraint(op.f("ck_actions_toolchain_digest"), "actions", type_="check", schema=S)
    op.drop_column("actions", "toolchain", schema=S)
    op.drop_column("actions", "params", schema=S)
    op.drop_column("actions", "rule", schema=S)
