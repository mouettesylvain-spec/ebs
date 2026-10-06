"""Infra reason `input_verification`: the executor's reason for runner exit 76 (P0-14).

Revision ID: 0002_infra_input_verification
Revises: 0001_initial
Create Date: 2026-10-06

Frozen: later schema changes go into new revisions, never into this file.
"""

from __future__ import annotations

from alembic import op

revision = "0002_infra_input_verification"
down_revision: str | None = "0001_initial"
branch_labels: str | None = None
depends_on: str | None = None

S = "ebs"
NAME = "ck_actions_infra_reason_valid"
OLD = ("oom", "timeout", "node_fail", "preempted", "license", "runner_crash", "other")
NEW = (*OLD[:-1], "input_verification", "other")


def _replace(values: tuple[str, ...]) -> None:
    listed = ", ".join(f"'{v}'" for v in values)
    op.drop_constraint(op.f(NAME), "actions", type_="check", schema=S)
    op.create_check_constraint(op.f(NAME), "actions", f"infra_reason IN ({listed})", schema=S)


def upgrade() -> None:
    _replace(NEW)


def downgrade() -> None:
    # Rows recorded with the new reason fall back to the generic one the old schema allows.
    op.execute(
        f"UPDATE {S}.actions SET infra_reason = 'other' WHERE infra_reason = 'input_verification'"
    )
    _replace(OLD)
