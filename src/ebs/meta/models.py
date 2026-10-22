"""PostgreSQL schema of the metadata store (docs/design/data-model.md).

SQLAlchemy Core tables in schema `ebs`. The Alembic revisions under `migrations/` must produce
exactly this schema (tests/integration/meta/test_migrations.py::test_no_model_drift). CHECK
constraints mirror the validation rules of `ebs.meta.api` (R8).
"""

from __future__ import annotations

from typing import Final, get_args

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from ebs.meta.api import (
    CHANNEL_PATTERN,
    DIGEST_SQL_PATTERN,
    DOMAIN_PATTERN,
    EVENT_TYPE_PATTERN,
    IMPORT_ALIAS_PATTERN,
    ActionState,
    BuildStatus,
    CacheMode,
    InfraReason,
    PendingReason,
    QueueState,
)
from ebs.meta.api import RELEASE_NAME_PATTERN as _RELEASE_NAME
from ebs.meta.api import VERSION_PATTERN as _VERSION

__all__ = [
    "SCHEMA",
    "action_cache",
    "actions",
    "api_tokens",
    "archives",
    "blobs",
    "builds",
    "channel_history",
    "channels",
    "domains",
    "driver_leases",
    "event_id_seq",
    "events",
    "exports",
    "gc_runs",
    "lease_objects",
    "leases",
    "metadata",
    "pilot_queue",
    "pins",
    "provenance_edges",
    "release_imports",
    "releases",
    "tombstones",
    "toolchain_manifests",
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
    sa.Column("final_plan_digest", sa.Text, nullable=True),  # 0012: set by finish_build
    _digest_check("plan_digest"),
    _digest_check("final_plan_digest"),
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
    sa.Column("rule", sa.Text, nullable=True),  # rule kind, params, toolchain (0010, P2-04)
    sa.Column("params", JSONB(none_as_null=True), nullable=True),  # None is SQL NULL
    sa.Column("toolchain", sa.Text, nullable=True),
    _digest_check("key"),
    _digest_check("result_key"),
    _digest_check("toolchain"),
    sa.CheckConstraint("params IS NULL OR jsonb_typeof(params) = 'object'", name="params_object"),
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
    sa.Column("last_used_at", _TS, nullable=True),  # P1-07: `ebs toolchain check --days`
    _digest_check("id"),
    _digest_check("fingerprint"),
    sa.CheckConstraint("jsonb_typeof(env) = 'object'", name="env_object"),
    sa.CheckConstraint("jsonb_typeof(install_roots) = 'array'", name="install_roots_array"),
)

toolchain_manifests = sa.Table(  # P1-07: fingerprint entries, for drift reports
    "toolchain_manifests",
    metadata,
    sa.Column(
        "toolchain_id",
        sa.Text,
        sa.ForeignKey("toolchains.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("entries", sa.LargeBinary, nullable=False),  # zlib-compressed JSON lines
)

# Partitioned by UTC month of `stored_at`, the store's clock at insert (revision 0008, P2-05
# R6): partitions `events_yYYYYmMM` are created on demand by `ebs.ensure_events_partition(ts)`
# and dropped whole by retention. A partitioned table's primary key must include the partition
# key, hence (id, stored_at); ids come from a sequence shared by every partition. `ts` is the
# event's own (client) time and never picks a partition.
event_id_seq = sa.Sequence("event_id_seq", metadata=metadata)

events = sa.Table(
    "events",
    metadata,
    # The database default is nextval('ebs.event_id_seq') too; autogenerate does not compare it.
    sa.Column("id", sa.BigInteger, event_id_seq, nullable=False),
    sa.Column(
        "build_id", sa.BigInteger, sa.ForeignKey("builds.id", ondelete="CASCADE"), nullable=False
    ),
    sa.Column("ts", _TS, nullable=False),
    sa.Column("type", sa.Text, nullable=False),
    sa.Column("action_id", sa.Text, nullable=True),
    sa.Column("data", JSONB, nullable=False),
    sa.Column("stored_at", _TS, nullable=False),
    sa.CheckConstraint(f"type ~ '{EVENT_TYPE_PATTERN}'", name="type_valid"),
    sa.CheckConstraint("jsonb_typeof(data) = 'object'", name="data_object"),
    sa.PrimaryKeyConstraint("id", "stored_at"),
    sa.Index(None, "build_id", "id"),
    postgresql_partition_by="RANGE (stored_at)",
)

api_tokens = sa.Table(  # bearer tokens of the metadata service (P1-06); only the hash is stored
    "api_tokens",
    metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("principal", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("team", sa.Text, nullable=True),
    sa.Column("hash", sa.Text, nullable=False, unique=True),
    sa.Column("created_at", _TS, nullable=False),
    sa.Column("expires_at", _TS, nullable=True),
    sa.Column("revoked_at", _TS, nullable=True),
    sa.CheckConstraint("kind IN ('user', 'ci', 'service')", name="kind_valid"),
    _digest_check("hash"),
    sa.Index(None, "principal"),
)

# --- Phase 1: garbage collection (P1-09, revision 0006) ------------------------------------------

leases = sa.Table(  # roots held by running builds; renewed by the driver heartbeat
    "leases",
    metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    _domain_fk(),
    sa.Column(
        "build_id", sa.BigInteger, sa.ForeignKey("builds.id", ondelete="CASCADE"), nullable=False
    ),
    sa.Column("expires_at", _TS, nullable=False),
    sa.Column("created_at", _TS, nullable=False),
    sa.Index(None, "domain", "expires_at"),
)

lease_objects = sa.Table(
    "lease_objects",
    metadata,
    sa.Column(
        "lease_id", sa.BigInteger, sa.ForeignKey("leases.id", ondelete="CASCADE"), primary_key=True
    ),
    sa.Column("digest", sa.Text, primary_key=True),
    _digest_check("digest"),
)

pins = sa.Table(  # `ebs pin BUILD`: the build's plan, outputs and logs are GC roots
    "pins",
    metadata,
    sa.Column("domain", sa.Text, sa.ForeignKey("domains.name"), primary_key=True),
    sa.Column(
        "build_id", sa.BigInteger, sa.ForeignKey("builds.id", ondelete="CASCADE"), primary_key=True
    ),
    sa.Column("pinned_by", sa.Text, nullable=False),
    sa.Column("reason", sa.Text, nullable=False, server_default=sa.text("''")),
    sa.Column("created_at", _TS, nullable=False),
)

gc_runs = sa.Table(
    "gc_runs",
    metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    _domain_fk(),
    sa.Column("started_at", _TS, nullable=False),
    sa.Column("finished_at", _TS, nullable=False),
    sa.Column("marked", sa.BigInteger, nullable=False),
    sa.Column("swept_rows", sa.BigInteger, nullable=False),
    sa.Column("swept_bytes", sa.BigInteger, nullable=False),
    sa.Column("dry_run", sa.Boolean, nullable=False),
    sa.Column("report", JSONB, nullable=False),
    sa.CheckConstraint("jsonb_typeof(report) = 'object'", name="report_object"),
    sa.Index(None, "domain", "started_at"),
)

tombstones = sa.Table(  # the grace period between deleting cache rows and their blobs (I15)
    "tombstones",
    metadata,
    sa.Column("domain", sa.Text, sa.ForeignKey("domains.name"), primary_key=True),
    sa.Column("digest", sa.Text, primary_key=True),
    sa.Column("deleted_rows_at", _TS, nullable=False),
    sa.Column("blob_delete_after", _TS, nullable=False),
    _digest_check("digest"),
)


# --- Phase 2: releases and channels (P2-02, revision 0007) ---------------------------------------
# Revision 0007 also installs triggers (not modelled here): `releases` refuses DELETE and any
# UPDATE but setting the yank fields once (I19); `channel_history` and `exports` are append-only.

releases = sa.Table(
    "releases",
    metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    _domain_fk(),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("version", sa.Text, nullable=False),
    sa.Column("build_id", sa.BigInteger, sa.ForeignKey("builds.id"), nullable=False),
    sa.Column("outputs", JSONB, nullable=False),  # {name: ReleaseOutput}
    sa.Column("notes", sa.Text, nullable=False, server_default=sa.text("''")),
    sa.Column("allow_failed", sa.Text, nullable=True),
    sa.Column("attestation", sa.Text, nullable=True),  # P3-03
    sa.Column("signature", sa.Text, nullable=True),  # P3-03
    sa.Column("exported_from", sa.BigInteger, sa.ForeignKey("releases.id"), nullable=True),
    sa.Column("created_at", _TS, nullable=False),
    sa.Column("created_by", sa.Text, nullable=False),
    sa.Column("yanked_at", _TS, nullable=True),
    sa.Column("yanked_by", sa.Text, nullable=True),
    sa.Column("yank_reason", sa.Text, nullable=True),
    sa.UniqueConstraint("domain", "name", "version"),
    sa.CheckConstraint(f"name ~ '{_RELEASE_NAME}'", name="name_valid"),
    sa.CheckConstraint(f"version ~ '{_VERSION}'", name="version_valid"),
    sa.CheckConstraint("jsonb_typeof(outputs) = 'object'", name="outputs_object"),
    sa.CheckConstraint(
        "(yanked_at IS NULL) = (yanked_by IS NULL) AND (yanked_at IS NULL) = (yank_reason IS NULL)",
        name="yank_fields",
    ),
)

channel_history = sa.Table(  # every move of a channel, append-only
    "channel_history",
    metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    _domain_fk(),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("channel", sa.Text, nullable=False),
    sa.Column("release_id", sa.BigInteger, sa.ForeignKey("releases.id"), nullable=False),
    sa.Column("moved_at", _TS, nullable=False),
    sa.Column("moved_by", sa.Text, nullable=False),
    sa.Column("gate_flow", sa.Text, nullable=True),
    sa.Column("gate_build_id", sa.BigInteger, sa.ForeignKey("builds.id"), nullable=True),
    sa.CheckConstraint(f"channel ~ '{CHANNEL_PATTERN}'", name="channel_valid"),
    sa.CheckConstraint("(gate_flow IS NULL) = (gate_build_id IS NULL)", name="gate_fields"),
    sa.Index(None, "domain", "name", "channel", "id"),
)

channels = sa.Table(  # the current pointer: the last move of each channel
    "channels",
    metadata,
    sa.Column("domain", sa.Text, sa.ForeignKey("domains.name"), primary_key=True),
    sa.Column("name", sa.Text, primary_key=True),
    sa.Column("channel", sa.Text, primary_key=True),
    sa.Column("release_id", sa.BigInteger, sa.ForeignKey("releases.id"), nullable=False),
    sa.Column("move_id", sa.BigInteger, sa.ForeignKey("channel_history.id"), nullable=False),
    sa.Column("updated_at", _TS, nullable=False),
    sa.Column("updated_by", sa.Text, nullable=False),
)

exports = sa.Table(  # audited cross-domain copies, append-only
    "exports",
    metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("release_id", sa.BigInteger, sa.ForeignKey("releases.id"), nullable=False),
    sa.Column("exported_release_id", sa.BigInteger, sa.ForeignKey("releases.id"), nullable=False),
    sa.Column("to_domain", sa.Text, sa.ForeignKey("domains.name"), nullable=False),
    sa.Column("exported_at", _TS, nullable=False),
    sa.Column("exported_by", sa.Text, nullable=False),
    sa.Index(None, "release_id"),
)

# --- Phase 2: imports (P2-03, revision 0009; append-only like the tables above) ----------------

release_imports = sa.Table(  # which release each import alias of a build used (`used-by`)
    "release_imports",
    metadata,
    sa.Column("build_id", sa.BigInteger, sa.ForeignKey("builds.id"), primary_key=True),
    sa.Column("alias", sa.Text, primary_key=True),
    sa.Column("release_id", sa.BigInteger, sa.ForeignKey("releases.id"), nullable=False),
    sa.Index(None, "release_id"),
    sa.CheckConstraint(f"alias ~ '{IMPORT_ALIAS_PATTERN}'", name="alias_valid"),
)


# --- Phase 2: detached and resumed drivers (P2-08, revision 0011) ---------------------------------

gc_locks = sa.Table(  # one GC run per domain (0013); renewed between the run's phases
    "gc_locks",
    metadata,
    sa.Column("domain", sa.Text, primary_key=True),
    sa.Column("owner", sa.Text, nullable=False),
    sa.Column("expires_at", _TS, nullable=False),
    sa.CheckConstraint("length(owner) BETWEEN 1 AND 256", name="owner_length"),
)

driver_leases = sa.Table(  # one live driver per build; renewed by the driver's heartbeat
    "driver_leases",
    metadata,
    sa.Column(
        "build_id", sa.BigInteger, sa.ForeignKey("builds.id", ondelete="CASCADE"), primary_key=True
    ),
    sa.Column("owner", sa.Text, nullable=False),
    sa.Column("expires_at", _TS, nullable=False),
    sa.Column("claimed_at", _TS, nullable=False),
    sa.CheckConstraint("length(owner) BETWEEN 1 AND 256", name="owner_length"),
)


# --- Phase 3: pilot worker pool (P3-06, revision 0014) --------------------------------------------

pilot_queue = sa.Table(  # actions waiting for, or leased by, the pilot jobs of a pool
    "pilot_queue",
    metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    _domain_fk(),
    sa.Column("pool", sa.Uuid, nullable=False),
    sa.Column("action_id", sa.Text, nullable=False),
    sa.Column("plan", sa.Text, nullable=False),
    sa.Column("build_id", sa.BigInteger, nullable=True),
    sa.Column("cpus", sa.Integer, nullable=False),
    sa.Column("mem_mb", sa.BigInteger, nullable=True),
    sa.Column("time_s", sa.BigInteger, nullable=True),
    sa.Column("licenses", JSONB, nullable=False),  # {feature: count}
    sa.Column("features", JSONB, nullable=False),  # sorted node features
    sa.Column("exclude", JSONB, nullable=False),  # sorted nodes it must not run on (P1-05)
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("pilot", sa.Text, nullable=True),
    sa.Column("node", sa.Text, nullable=True),
    sa.Column("expires_at", _TS, nullable=True),
    sa.Column("exit_code", sa.Integer, nullable=True),
    sa.Column("infra_reason", sa.Text, nullable=True),
    sa.Column("queued_at", _TS, nullable=False),
    _digest_check("plan"),
    _in_check("state", QueueState),
    _in_check("infra_reason", InfraReason),
    sa.CheckConstraint("cpus >= 1", name="cpus_positive"),
    sa.CheckConstraint("jsonb_typeof(licenses) = 'object'", name="licenses_object"),
    sa.CheckConstraint("jsonb_typeof(features) = 'array'", name="features_array"),
    sa.CheckConstraint("jsonb_typeof(exclude) = 'array'", name="exclude_array"),
    sa.CheckConstraint("state <> 'leased' OR expires_at IS NOT NULL", name="lease_expires"),
    sa.Index(None, "domain", "pool", "state", "id"),
)


# --- Phase 3: sign-off bundles (P3-04, revision 0015; append-only) --------------------------------

archives = sa.Table(  # sign-off bundles of releases written to cold storage
    "archives",
    metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("release_id", sa.BigInteger, sa.ForeignKey("releases.id"), nullable=False),
    sa.Column("bundle_digest", sa.Text, nullable=False),
    sa.Column("location", sa.Text, nullable=False),
    sa.Column("created_at", _TS, nullable=False),
    sa.Column("created_by", sa.Text, nullable=False),
    _digest_check("bundle_digest"),
    sa.CheckConstraint("length(location) BETWEEN 1 AND 4096", name="location_length"),
    sa.Index(None, "release_id"),
)
