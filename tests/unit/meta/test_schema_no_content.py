"""Schema review (P2-01 R5): the metadata database never stores file contents.

Every column of `ebs.meta.models` must be classified below; a new column fails this test until
someone decides what it holds. File bytes (sources, outputs, logs) live in the CAS; the database
keeps their digests. The only binary column is the toolchain fingerprint manifest (install-tree
paths, sizes and hashes of vendor tools: no design data).
"""

from __future__ import annotations

from typing import Final, get_args

import sqlalchemy as sa

from ebs.meta.api import OutputResult, ReleaseOutput, ResultManifest
from ebs.meta.models import metadata

# What a column holds. "digest": an algo:hex digest or an id; "name": an identifier, user or
# label; "enum": a CHECKed keyword; "text": short human text (reasons, notes); "metadata":
# validated JSON of digests, names, numbers; "scalar": numbers, booleans, timestamps.
DIGEST, NAME, ENUM, TEXT, META, SCALAR = "digest", "name", "enum", "text", "metadata", "scalar"

BINARY_ALLOWED: Final = {"toolchain_manifests.entries"}  # zlib fingerprint lines (P1-07)

REVIEWED: Final[dict[str, str]] = {
    "domains.name": NAME, "domains.unix_group": NAME, "domains.cas_root": NAME,
    "api_tokens.principal": NAME, "api_tokens.kind": ENUM, "api_tokens.team": NAME,
    "api_tokens.hash": DIGEST,
    "toolchains.id": DIGEST, "toolchains.name": NAME, "toolchains.module": NAME,
    "toolchains.version": NAME, "toolchains.fingerprint": DIGEST, "toolchains.env": META,
    "toolchains.install_roots": META, "toolchains.registered_by": NAME,
    "toolchains.superseded_by": DIGEST,
    "toolchain_manifests.toolchain_id": DIGEST, "toolchain_manifests.entries": META,
    "action_cache.domain": NAME, "action_cache.key": DIGEST, "action_cache.result": META,
    "blobs.domain": NAME, "blobs.digest": DIGEST, "blobs.kind": ENUM,
    "builds.uuid": DIGEST, "builds.domain": NAME, "builds.project": NAME,
    "builds.plan_digest": DIGEST, "builds.final_plan_digest": DIGEST,
    "builds.flow_repo": NAME, "builds.flow_commit": DIGEST,
    "builds.user_name": NAME, "builds.ci_job": NAME, "builds.cache_mode": ENUM,
    "builds.status": ENUM,
    "gc_runs.domain": NAME, "gc_runs.report": META,
    "provenance_edges.domain": NAME, "provenance_edges.action_key": DIGEST,
    "provenance_edges.direction": ENUM, "provenance_edges.logical_path": NAME,
    "provenance_edges.object_id": DIGEST, "provenance_edges.content_digest": DIGEST,
    "tombstones.domain": NAME, "tombstones.digest": DIGEST,
    "actions.action_id": NAME, "actions.step": NAME, "actions.key": DIGEST,
    "actions.state": ENUM, "actions.slurm_job_id": NAME, "actions.pending_reason": ENUM,
    "actions.infra_reason": ENUM, "actions.result_key": DIGEST, "actions.result": META,
    "actions.rule": NAME, "actions.params": META, "actions.toolchain": DIGEST,  # P2-04
    "events.type": ENUM, "events.action_id": NAME, "events.data": META,
    "leases.domain": NAME, "lease_objects.digest": DIGEST,
    "pins.domain": NAME, "pins.pinned_by": NAME, "pins.reason": TEXT,
    "driver_leases.owner": NAME,  # host:pid:token (P2-08)
    "releases.domain": NAME, "releases.name": NAME, "releases.version": NAME,
    "releases.outputs": META, "releases.notes": TEXT, "releases.allow_failed": TEXT,
    "releases.attestation": META, "releases.signature": META, "releases.created_by": NAME,
    "releases.yanked_by": NAME, "releases.yank_reason": TEXT,
    "channel_history.domain": NAME, "channel_history.name": NAME,
    "channel_history.channel": NAME, "channel_history.moved_by": NAME,
    "channel_history.gate_flow": NAME,
    "channels.domain": NAME, "channels.name": NAME, "channels.channel": NAME,
    "channels.updated_by": NAME,
    "exports.to_domain": NAME, "exports.exported_by": NAME,
    "release_imports.alias": NAME,
}  # fmt: skip

_SCALARS = (sa.Integer, sa.BigInteger, sa.Boolean, sa.DateTime, sa.Numeric)


# R5: no binary column but the reviewed one, and every other column classified.
def test_schema_reviewed() -> None:
    unreviewed, binary = [], []
    for table in metadata.sorted_tables:
        for column in table.columns:
            name = f"{table.name}.{column.name}"
            if isinstance(column.type, sa.LargeBinary) and name not in BINARY_ALLOWED:
                binary.append(name)
            if isinstance(column.type, _SCALARS):
                continue
            if name not in REVIEWED:
                unreviewed.append(name)
    assert binary == [], "file contents belong in the CAS, not the metadata database"
    assert unreviewed == [], "classify new columns in REVIEWED (and keep contents out)"
    stale = set(REVIEWED) - {
        f"{t.name}.{c.name}" for t in metadata.sorted_tables for c in t.columns
    }
    assert stale == set()


# R5: the JSON payloads hold digests, never bytes: a result names its log and outputs by digest.
def test_payloads_hold_digests() -> None:
    assert set(ResultManifest.model_fields) == {
        "v", "action_key", "status", "exit_code", "inputs", "outputs", "log", "summary",
        "resources", "runner",
    }  # fmt: skip
    assert "Digest" in str(ResultManifest.model_fields["log"].annotation)
    assert set(OutputResult.model_fields) == {"digest", "id", "type", "size"}
    assert set(ReleaseOutput.model_fields) == {"action_id", "output", "digest", "id", "type",
                                                "size"}  # fmt: skip
    assert get_args(OutputResult.model_fields["type"].annotation) == ("file", "tree")
