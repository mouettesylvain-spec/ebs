"""Pilot queue: actions pulled by pilot jobs (P3-06).

Revision ID: 0014_pilot_queue
Revises: 0013_gc_locks
Create Date: 2026-10-20

Frozen: later schema changes go into new revisions, never into this file.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0014_pilot_queue"
down_revision: str | None = "0013_gc_locks"
branch_labels: str | None = None
depends_on: str | None = None

S = "ebs"
TS = sa.DateTime(timezone=True)
DIGEST = "^(sha256|blake3):[0-9a-f]{64}$"
STATES = "'queued', 'leased', 'done', 'lost', 'cancelled'"
INFRA = (
    "'oom', 'timeout', 'node_fail', 'preempted', 'license', 'runner_crash', "
    "'input_verification', 'other'"
)


def upgrade() -> None:
    op.create_table(
        "pilot_queue",
        sa.Column("id", sa.BigInteger, sa.Identity(), nullable=False),
        sa.Column("domain", sa.Text, nullable=False),
        sa.Column("pool", sa.Uuid, nullable=False),
        sa.Column("action_id", sa.Text, nullable=False),
        sa.Column("plan", sa.Text, nullable=False),
        sa.Column("build_id", sa.BigInteger, nullable=True),
        sa.Column("cpus", sa.Integer, nullable=False),
        sa.Column("mem_mb", sa.BigInteger, nullable=True),
        sa.Column("time_s", sa.BigInteger, nullable=True),
        sa.Column("licenses", JSONB, nullable=False),
        sa.Column("features", JSONB, nullable=False),
        sa.Column("exclude", JSONB, nullable=False),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("pilot", sa.Text, nullable=True),
        sa.Column("node", sa.Text, nullable=True),
        sa.Column("expires_at", TS, nullable=True),
        sa.Column("exit_code", sa.Integer, nullable=True),
        sa.Column("infra_reason", sa.Text, nullable=True),
        sa.Column("queued_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pilot_queue")),
        sa.ForeignKeyConstraint(
            ["domain"], [f"{S}.domains.name"], name=op.f("fk_pilot_queue_domain")
        ),
        sa.CheckConstraint(f"plan ~ '{DIGEST}'", name=op.f("ck_pilot_queue_plan_digest")),
        sa.CheckConstraint(f"state IN ({STATES})", name=op.f("ck_pilot_queue_state_valid")),
        sa.CheckConstraint(
            f"infra_reason IN ({INFRA})", name=op.f("ck_pilot_queue_infra_reason_valid")
        ),
        sa.CheckConstraint("cpus >= 1", name=op.f("ck_pilot_queue_cpus_positive")),
        sa.CheckConstraint(
            "jsonb_typeof(licenses) = 'object'", name=op.f("ck_pilot_queue_licenses_object")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(features) = 'array'", name=op.f("ck_pilot_queue_features_array")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(exclude) = 'array'", name=op.f("ck_pilot_queue_exclude_array")
        ),
        sa.CheckConstraint(
            "state <> 'leased' OR expires_at IS NOT NULL",
            name=op.f("ck_pilot_queue_lease_expires"),
        ),
        schema=S,
    )
    op.create_index(
        "ix_pilot_queue_domain_pool_state_id",
        "pilot_queue",
        ["domain", "pool", "state", "id"],
        schema=S,
    )


def downgrade() -> None:
    op.drop_index("ix_pilot_queue_domain_pool_state_id", "pilot_queue", schema=S)
    op.drop_table("pilot_queue", schema=S)
