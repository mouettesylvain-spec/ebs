"""Action state `skipped` and the stored result manifest on each action (P0-15).

The driver marks actions `skipped` when a dependency failed or the build stopped after a
failure, and reads a finished action's manifest back with `get_result` in every cache mode.

Revision ID: 0003_skipped_action_result
Revises: 0002_infra_input_verification
Create Date: 2026-10-06

Frozen: later schema changes go into new revisions, never into this file.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003_skipped_action_result"
down_revision: str | None = "0002_infra_input_verification"
branch_labels: str | None = None
depends_on: str | None = None

S = "ebs"
STATE_CHECK = "ck_actions_state_valid"
RESULT_CHECK = "ck_actions_result_manifest"
OLD_STATES = (
    "queued",
    "pending",
    "running",
    "done",
    "failed",
    "infra_failed",
    "cached",
    "cancelled",
)
NEW_STATES = (*OLD_STATES, "skipped")


def _states(values: tuple[str, ...]) -> None:
    listed = ", ".join(f"'{v}'" for v in values)
    op.drop_constraint(op.f(STATE_CHECK), "actions", type_="check", schema=S)
    op.create_check_constraint(op.f(STATE_CHECK), "actions", f"state IN ({listed})", schema=S)


def upgrade() -> None:
    _states(NEW_STATES)
    op.add_column("actions", sa.Column("result", JSONB, nullable=True), schema=S)
    op.create_check_constraint(
        op.f(RESULT_CHECK),
        "actions",
        "result IS NULL OR (jsonb_typeof(result) = 'object' AND result->'v' = '1'::jsonb)",
        schema=S,
    )


def downgrade() -> None:
    op.drop_constraint(op.f(RESULT_CHECK), "actions", type_="check", schema=S)
    op.drop_column("actions", "result", schema=S)
    # Skipped actions never ran; the old schema's closest final state is `cancelled`.
    op.execute(f"UPDATE {S}.actions SET state = 'cancelled' WHERE state = 'skipped'")
    _states(OLD_STATES)
