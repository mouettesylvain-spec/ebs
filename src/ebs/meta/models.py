"""PostgreSQL schema of the metadata store, phase 0 (docs/design/data-model.md).

SQLAlchemy Core tables in schema `ebs`. The Alembic revisions under `migrations/` must produce
exactly this schema (tests/integration/meta/test_migrations.py::test_no_model_drift). CHECK
constraints mirror the validation rules of `ebs.meta.api` (R8).
"""

from __future__ import annotations

from typing import Final, get_args

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from ebs.meta.api import (
    DIGEST_SQL_PATTERN,
    DOMAIN_PATTERN,
    EVENT_TYPE_PATTERN,
    ActionState,
    BuildStatus,
    CacheMode,
    InfraReason,
    PendingReason,
)

__all__ = [
    "SCHEMA",
    "action_cache",
    "actions",
    "blobs",
    "builds",
    "domains",
    "events",
    "metadata",
    "provenance_edges",
    "toolchains",
]

SCHEMA: Final = "ebs"

metadata = sa.MetaData(
    schema=SCHEMA,
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_N_name)s",
        "pk": "pk_%(table_name)s",
    },
)

_TS = sa.DateTime(timezone=True)


def _digest_check(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"{column} ~ '{DIGEST_SQL_PATTERN}'", name=f"{column}_digest")


def _in_check(column: str, values: object) -> sa.CheckConstraint:
    listed = ", ".join(f"'{v}'" for v in get_args(values))
    return sa.CheckConstraint(f"{column} IN ({listed})", name=f"{column}_valid")


def _domain_fk() -> sa.Column[str]:
    return sa.Column("domain", sa.Text, sa.ForeignKey("domains.name"), nullable=False)


domains = sa.Table(
    "domains",
    metadata,
    sa.Column("name", sa.Text, primary_key=True),
    sa.Column("unix_group", sa.Text, nullable=True),  # set by the domain admin tooling (P2-01)
    sa.Column("cas_root", sa.Text, nullable=True),
    sa.Column("ttl_days", sa.Integer, nullable=False, server_default=sa.text("30")),
    sa.Column("quota_bytes", sa.BigInteger, nullable=True),
    sa.Column("high_water", sa.Numeric, nullable=False, server_default=sa.text("0.85")),
    sa.Column("low_water", sa.Numeric, nullable=False, server_default=sa.text("0.75")),
    sa.CheckConstraint(f"name ~ '{DOMAIN_PATTERN}'", name="name_valid"),
    sa.CheckConstraint("ttl_days > 0", name="ttl_days_positive"),
    sa.CheckConstraint(
        "0 < low_water AND low_water <= high_water AND high_water <= 1", name="water"
    ),
)

builds = sa.Table(
    "builds",
    metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("uuid", sa.Uuid, nullable=False, unique=True),
    _domain_fk(),
    sa.Column("project", sa.Text, nullable=False),
    sa.Column("plan_digest", sa.Text, nullable=False),
    sa.Column("flow_repo", sa.Text, nullable=False),
    sa.Column("flow_commit", sa.Text, nullable=False),
    sa.Column("flow_dirty", sa.Boolean, nullable=False),
    sa.Column("user_name", sa.Text, nullable=False),
    sa.Column("ci_job", sa.Text, nullable=True),
    sa.Column("cache_mode", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("created_at", _TS, nullable=False),
    sa.Column("finished_at", _TS, nullable=True),
    sa.Column("pinned", sa.Boolean, nullable=False, server_default=sa.false()),
    _digest_check("plan_digest"),
    _in_check("cache_mode", CacheMode),
    _in_check("status", BuildStatus),
    sa.CheckConstraint("(status = 'running') = (finished_at IS NULL)", name="finished_at_set"),
)

actions = sa.Table(
    "actions",
    metadata,
    sa.Column(
        "build_id", sa.BigInteger, sa.ForeignKey("builds.id", ondelete="CASCADE"), primary_key=True
    ),
    sa.Column("action_id", sa.Text, primary_key=True),
    sa.Column("step", sa.Text, nullable=False),
    sa.Column("key", sa.Text, nullable=True),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("slurm_job_id", sa.Text, nullable=True),
    sa.Column("pending_reason", sa.Text, nullable=True),
    sa.Column("infra_reason", sa.Text, nullable=True),
    sa.Column("queued_at", _TS, nullable=False),
    sa.Column("started_at", _TS, nullable=True),
    sa.Column("finished_at", _TS, nullable=True),
    sa.Column("cached", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("result_key", sa.Text, nullable=True),
    sa.Column("result", JSONB, nullable=True),  # last recorded ResultManifest (0003, P0-15)
    _digest_check("key"),
    _digest_check("result_key"),
    sa.CheckConstraint(
        "result IS NULL OR (jsonb_typeof(result) = 'object' AND result->'v' = '1'::jsonb)",
        name="result_manifest",
    ),
    _in_check("state", ActionState),
    _in_check("pending_reason", PendingReason),
    _in_check("infra_reason", InfraReason),
    sa.CheckConstraint("attempts >= 0", name="attempts_non_negative"),
    sa.Index(None, "build_id", "state"),
    sa.Index(None, "key"),
)

action_cache = sa.Table(
    "action_cache",
    metadata,
    sa.Column("domain", sa.Text, sa.ForeignKey("domains.name"), primary_key=True),
    sa.Column("key", sa.Text, primary_key=True),
    sa.Column("result", JSONB, nullable=False),
    sa.Column("created_at", _TS, nullable=False),
    sa.Column("last_access", _TS, nullable=False),
    sa.Column("hits", sa.BigInteger, nullable=False, server_default=sa.text("0")),
    _digest_check("key"),
    sa.CheckConstraint(
        "jsonb_typeof(result) = 'object' AND result->'v' = '1'::jsonb", name="result_manifest"
    ),
    sa.CheckConstraint("hits >= 0", name="hits_non_negative"),
)

blobs = sa.Table(  # GC bookkeeping; the CAS is the source of truth for bytes
    "blobs",
    metadata,
    sa.Column("domain", sa.Text, sa.ForeignKey("domains.name"), primary_key=True),
    sa.Column("digest", sa.Text, primary_key=True),
    sa.Column("size", sa.BigInteger, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("created_at", _TS, nullable=False),
    sa.Column("last_access", _TS, nullable=False),
    _digest_check("digest"),
    sa.CheckConstraint("kind IN ('file', 'tree')", name="kind_valid"),
    sa.CheckConstraint("size >= 0", name="size_non_negative"),
)

provenance_edges = sa.Table(
    "provenance_edges",
    metadata,
    sa.Column("domain", sa.Text, sa.ForeignKey("domains.name"), primary_key=True),
    sa.Column("action_key", sa.Text, primary_key=True),
    sa.Column("direction", sa.Text, primary_key=True),
    sa.Column("logical_path", sa.Text, primary_key=True),
    sa.Column("object_id", sa.Text, nullable=False),
    sa.Column("content_digest", sa.Text, nullable=True),
    _digest_check("action_key"),
    _digest_check("object_id"),
    _digest_check("content_digest"),
    sa.CheckConstraint("direction IN ('in', 'out')", name="direction_valid"),
    sa.Index(None, "domain", "object_id"),
    sa.Index(None, "domain", "content_digest"),
)

toolchains = sa.Table(  # written by `ebs toolchain register` (P1-07)
    "toolchains",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("module", sa.Text, nullable=False),
    sa.Column("version", sa.Text, nullable=False),
    sa.Column("fingerprint", sa.Text, nullable=False),
    sa.Column("env", JSONB, nullable=False),
    sa.Column("install_roots", JSONB, nullable=False),
    sa.Column("registered_at", _TS, nullable=False),
    sa.Column("registered_by", sa.Text, nullable=False),
    sa.Column("superseded_by", sa.Text, sa.ForeignKey("toolchains.id"), nullable=True),
    _digest_check("id"),
    _digest_check("fingerprint"),
    sa.CheckConstraint("jsonb_typeof(env) = 'object'", name="env_object"),
    sa.CheckConstraint("jsonb_typeof(install_roots) = 'array'", name="install_roots_array"),
)

events = sa.Table(
    "events",
    metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column(
        "build_id", sa.BigInteger, sa.ForeignKey("builds.id", ondelete="CASCADE"), nullable=False
    ),
    sa.Column("ts", _TS, nullable=False),
    sa.Column("type", sa.Text, nullable=False),
    sa.Column("action_id", sa.Text, nullable=True),
    sa.Column("data", JSONB, nullable=False),
    sa.CheckConstraint(f"type ~ '{EVENT_TYPE_PATTERN}'", name="type_valid"),
    sa.CheckConstraint("jsonb_typeof(data) = 'object'", name="data_object"),
    sa.Index(None, "build_id", "id"),
)
