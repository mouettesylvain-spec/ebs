"""Phase 0 schema: domains, builds, actions, action cache, blobs, provenance, toolchains, events.

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-30

Frozen: later schema changes go into new revisions, never into this file.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001_initial"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None

S = "ebs"
DIGEST = "^(sha256|blake3):[0-9a-f]{64}$"
TS = sa.DateTime(timezone=True)


def _digest(table: str, column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"{column} ~ '{DIGEST}'", name=op.f(f"ck_{table}_{column}_digest"))


def _in(table: str, column: str, values: tuple[str, ...]) -> sa.CheckConstraint:
    listed = ", ".join(f"'{v}'" for v in values)
    return sa.CheckConstraint(f"{column} IN ({listed})", name=op.f(f"ck_{table}_{column}_valid"))


def _domain_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["domain"], [f"{S}.domains.name"], name=op.f(f"fk_{table}_domain")
    )


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {S}")

    op.create_table(
        "domains",
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("unix_group", sa.Text, nullable=True),
        sa.Column("cas_root", sa.Text, nullable=True),
        sa.Column("ttl_days", sa.Integer, nullable=False, server_default=sa.text("30")),
        sa.Column("quota_bytes", sa.BigInteger, nullable=True),
        sa.Column("high_water", sa.Numeric, nullable=False, server_default=sa.text("0.85")),
        sa.Column("low_water", sa.Numeric, nullable=False, server_default=sa.text("0.75")),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_domains")),
        sa.CheckConstraint(
            "name ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'", name=op.f("ck_domains_name_valid")
        ),
        sa.CheckConstraint("ttl_days > 0", name=op.f("ck_domains_ttl_days_positive")),
        sa.CheckConstraint(
            "0 < low_water AND low_water <= high_water AND high_water <= 1",
            name=op.f("ck_domains_water"),
        ),
        schema=S,
    )

    op.create_table(
        "builds",
        sa.Column("id", sa.BigInteger, sa.Identity(), nullable=False),
        sa.Column("uuid", sa.Uuid, nullable=False),
        sa.Column("domain", sa.Text, nullable=False),
        sa.Column("project", sa.Text, nullable=False),
        sa.Column("plan_digest", sa.Text, nullable=False),
        sa.Column("flow_repo", sa.Text, nullable=False),
        sa.Column("flow_commit", sa.Text, nullable=False),
        sa.Column("flow_dirty", sa.Boolean, nullable=False),
        sa.Column("user_name", sa.Text, nullable=False),
        sa.Column("ci_job", sa.Text, nullable=True),
        sa.Column("cache_mode", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("finished_at", TS, nullable=True),
        sa.Column("pinned", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_builds")),
        sa.UniqueConstraint("uuid", name=op.f("uq_builds_uuid")),
        _domain_fk("builds"),
        _digest("builds", "plan_digest"),
        _in("builds", "cache_mode", ("off", "read", "write")),
        _in("builds", "status", ("running", "passed", "failed", "infra_failed", "cancelled")),
        sa.CheckConstraint(
            "(status = 'running') = (finished_at IS NULL)", name=op.f("ck_builds_finished_at_set")
        ),
        schema=S,
    )

    op.create_table(
        "actions",
        sa.Column("build_id", sa.BigInteger, nullable=False),
        sa.Column("action_id", sa.Text, nullable=False),
        sa.Column("step", sa.Text, nullable=False),
        sa.Column("key", sa.Text, nullable=True),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("slurm_job_id", sa.Text, nullable=True),
        sa.Column("pending_reason", sa.Text, nullable=True),
        sa.Column("infra_reason", sa.Text, nullable=True),
        sa.Column("queued_at", TS, nullable=False),
        sa.Column("started_at", TS, nullable=True),
        sa.Column("finished_at", TS, nullable=True),
        sa.Column("cached", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("result_key", sa.Text, nullable=True),
        sa.PrimaryKeyConstraint("build_id", "action_id", name=op.f("pk_actions")),
        sa.ForeignKeyConstraint(
            ["build_id"], [f"{S}.builds.id"], name=op.f("fk_actions_build_id"), ondelete="CASCADE"
        ),
        _digest("actions", "key"),
        _digest("actions", "result_key"),
        _in(
            "actions",
            "state",
            (
                "queued",
                "pending",
                "running",
                "done",
                "failed",
                "infra_failed",
                "cached",
                "cancelled",
            ),
        ),
        _in("actions", "pending_reason", ("licenses", "resources", "priority", "other")),
        _in(
            "actions",
            "infra_reason",
            ("oom", "timeout", "node_fail", "preempted", "license", "runner_crash", "other"),
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_actions_attempts_non_negative")),
        schema=S,
    )
    op.create_index("ix_actions_build_id_state", "actions", ["build_id", "state"], schema=S)
    op.create_index("ix_actions_key", "actions", ["key"], schema=S)

    op.create_table(
        "action_cache",
        sa.Column("domain", sa.Text, nullable=False),
        sa.Column("key", sa.Text, nullable=False),
        sa.Column("result", JSONB, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("last_access", TS, nullable=False),
        sa.Column("hits", sa.BigInteger, nullable=False, server_default=sa.text("0")),
        sa.PrimaryKeyConstraint("domain", "key", name=op.f("pk_action_cache")),
        _domain_fk("action_cache"),
        _digest("action_cache", "key"),
        sa.CheckConstraint(
            "jsonb_typeof(result) = 'object' AND result->'v' = '1'::jsonb",
            name=op.f("ck_action_cache_result_manifest"),
        ),
        sa.CheckConstraint("hits >= 0", name=op.f("ck_action_cache_hits_non_negative")),
        schema=S,
    )

    op.create_table(
        "blobs",
        sa.Column("domain", sa.Text, nullable=False),
        sa.Column("digest", sa.Text, nullable=False),
        sa.Column("size", sa.BigInteger, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("last_access", TS, nullable=False),
        sa.PrimaryKeyConstraint("domain", "digest", name=op.f("pk_blobs")),
        _domain_fk("blobs"),
        _digest("blobs", "digest"),
        sa.CheckConstraint("kind IN ('file', 'tree')", name=op.f("ck_blobs_kind_valid")),
        sa.CheckConstraint("size >= 0", name=op.f("ck_blobs_size_non_negative")),
        schema=S,
    )

    op.create_table(
        "provenance_edges",
        sa.Column("domain", sa.Text, nullable=False),
        sa.Column("action_key", sa.Text, nullable=False),
        sa.Column("direction", sa.Text, nullable=False),
        sa.Column("logical_path", sa.Text, nullable=False),
        sa.Column("object_id", sa.Text, nullable=False),
        sa.Column("content_digest", sa.Text, nullable=True),
        sa.PrimaryKeyConstraint(
            "domain", "action_key", "direction", "logical_path", name=op.f("pk_provenance_edges")
        ),
        _domain_fk("provenance_edges"),
        _digest("provenance_edges", "action_key"),
        _digest("provenance_edges", "object_id"),
        _digest("provenance_edges", "content_digest"),
        sa.CheckConstraint(
            "direction IN ('in', 'out')", name=op.f("ck_provenance_edges_direction_valid")
        ),
        schema=S,
    )
    op.create_index(
        "ix_provenance_edges_domain_object_id",
        "provenance_edges",
        ["domain", "object_id"],
        schema=S,
    )
    op.create_index(
        "ix_provenance_edges_domain_content_digest",
        "provenance_edges",
        ["domain", "content_digest"],
        schema=S,
    )

    op.create_table(
        "toolchains",
        sa.Column("id", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("module", sa.Text, nullable=False),
        sa.Column("version", sa.Text, nullable=False),
        sa.Column("fingerprint", sa.Text, nullable=False),
        sa.Column("env", JSONB, nullable=False),
        sa.Column("install_roots", JSONB, nullable=False),
        sa.Column("registered_at", TS, nullable=False),
        sa.Column("registered_by", sa.Text, nullable=False),
        sa.Column("superseded_by", sa.Text, nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_toolchains")),
        sa.ForeignKeyConstraint(
            ["superseded_by"], [f"{S}.toolchains.id"], name=op.f("fk_toolchains_superseded_by")
        ),
        _digest("toolchains", "id"),
        _digest("toolchains", "fingerprint"),
        sa.CheckConstraint("jsonb_typeof(env) = 'object'", name=op.f("ck_toolchains_env_object")),
        sa.CheckConstraint(
            "jsonb_typeof(install_roots) = 'array'", name=op.f("ck_toolchains_install_roots_array")
        ),
        schema=S,
    )

    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger, sa.Identity(), nullable=False),
        sa.Column("build_id", sa.BigInteger, nullable=False),
        sa.Column("ts", TS, nullable=False),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("action_id", sa.Text, nullable=True),
        sa.Column("data", JSONB, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
        sa.ForeignKeyConstraint(
            ["build_id"], [f"{S}.builds.id"], name=op.f("fk_events_build_id"), ondelete="CASCADE"
        ),
        sa.CheckConstraint("type ~ '^[a-z][a-z0-9_]*$'", name=op.f("ck_events_type_valid")),
        sa.CheckConstraint("jsonb_typeof(data) = 'object'", name=op.f("ck_events_data_object")),
        schema=S,
    )
    op.create_index("ix_events_build_id_id", "events", ["build_id", "id"], schema=S)


def downgrade() -> None:
    for table in (
        "events",
        "toolchains",
        "provenance_edges",
        "blobs",
        "action_cache",
        "actions",
        "builds",
        "domains",
    ):
        op.drop_table(table, schema=S)
    op.execute(f"DROP SCHEMA IF EXISTS {S}")
