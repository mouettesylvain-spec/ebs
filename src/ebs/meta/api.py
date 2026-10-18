"""Metadata store protocol and its models (docs/design/interfaces.md § 6, data-model.md).

The store holds shared state: builds, their actions, the action cache, provenance edges, blob
access times and events. Every model is frozen and strict; JSON payloads must also pass
`canonical_json` (no floats, NFC strings), and stores re-validate every model on write so values
smuggled past the constructor (`model_construct`) are refused too (R8). The SQL schema mirrors
these rules with CHECK constraints (`ebs.meta.models`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Final, Literal, NewType, Protocol
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    PlainValidator,
    StrictBool,
    StrictInt,
    StrictStr,
    WithJsonSchema,
)

from ebs.core.digest import Digest
from ebs.core.errors import DigestError, MetadataError
from ebs.flow.model import NAME_PATTERN

__all__ = [
    "API_PREFIX",
    "BATCH_MAX",
    "DIGEST_SQL_PATTERN",
    "DOMAIN_PATTERN",
    "DRIVER_LEASE_TTL_S",
    "EVENT_TYPE_PATTERN",
    "FINAL_BUILD_STATUSES",
    "LEASE_TTL_S",
    "MAX_LEASE_TTL_S",
    "TERMINAL_STATES",
    "TOUCH_INTERVAL_S",
    "TRANSITIONS",
    "ActionBatch",
    "ActionId",
    "ActionList",
    "ActionRow",
    "ActionState",
    "ActionUpdate",
    "BlobBatch",
    "BlobRef",
    "BuildCreate",
    "BuildFinish",
    "BuildId",
    "BuildStatus",
    "BuildView",
    "CacheHits",
    "CacheMode",
    "Changed",
    "CreatedBuild",
    "CreatedLease",
    "DigestBatch",
    "DigestField",
    "DomainPolicy",
    "DriverClaim",
    "DriverLease",
    "DriverRelease",
    "ErrorBody",
    "Event",
    "EventType",
    "GcAttribution",
    "GcBlob",
    "GcCacheEntry",
    "GcRoots",
    "GcRun",
    "GcStore",
    "InfraReason",
    "LeaseId",
    "LeaseRequest",
    "MetadataStore",
    "OptionalDigest",
    "OptionalResult",
    "OutputResult",
    "PendingReason",
    "PinRequest",
    "RegisterOutcome",
    "ResourceUsage",
    "ResultManifest",
    "RunnerInfo",
    "StateChange",
    "Tombstone",
    "ToolchainCreate",
    "ToolchainList",
    "ToolchainManifest",
    "ToolchainRecord",
    "ToolchainRegistration",
    "check_domain",
    "check_transition",
]

DIGEST_SQL_PATTERN: Final = "^(sha256|blake3):[0-9a-f]{64}$"
"""Digest rule as a POSIX regex for SQL CHECK constraints; equivalent to `Digest.parse`."""

DOMAIN_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
"""Confidentiality domain names; the same rule as the fs CAS directory names."""

EVENT_TYPE_PATTERN: Final = "^[a-z][a-z0-9_]*$"

TOUCH_INTERVAL_S: Final = 3600
"""`last_access` is written at most once per key per hour (write-amplification guard, R3)."""

BuildId = NewType("BuildId", int)
LeaseId = NewType("LeaseId", int)

MAX_LEASE_TTL_S: Final = 86_400
"""Longest lease a store grants: a lease is a heartbeat-renewed root, never a permanent pin."""

LEASE_TTL_S: Final = 900
"""Lease lifetime the driver requests and renews on its heartbeat (P1-09 R2: 15 min)."""

DRIVER_LEASE_TTL_S: Final = 300
"""Driver lease lifetime (P2-08 R3): renewed every third of it; a driver silent for longer may
be replaced by `ebs build --resume`."""

ActionState = Literal[
    "queued",
    "pending",
    "running",
    "done",
    "failed",
    "infra_failed",
    "cached",
    "cancelled",
    "skipped",  # P0-15: never started, because a dependency failed or the build stopped
]
BuildStatus = Literal["running", "passed", "failed", "infra_failed", "cancelled"]
CacheMode = Literal["off", "read", "write"]
PendingReason = Literal["licenses", "resources", "priority", "other"]
InfraReason = Literal[
    "oom",
    "timeout",
    "node_fail",
    "preempted",
    "license",
    "runner_crash",
    "input_verification",  # runner exit 76: staged bytes did not match their digest (P0-14)
    "other",
]
EventType = Literal[
    "build_started",
    "plan_ready",
    "cache_hit",
    "submitted",
    "pending",
    "running",
    "finished",
    "infra_failed",
    "retrying",
    "build_finished",
    "stat_audit_mismatch",
    "build_resumed",  # P2-08: a new driver took over a running build
]

FINAL_BUILD_STATUSES: Final[frozenset[str]] = frozenset(
    {"passed", "failed", "infra_failed", "cancelled"}
)

TERMINAL_STATES: Final[frozenset[str]] = frozenset(
    {"done", "failed", "infra_failed", "cached", "cancelled", "skipped"}
)

TRANSITIONS: Final[Mapping[str, frozenset[str]]] = {
    "queued": frozenset({"pending", "running", "cached", "cancelled", "infra_failed", "skipped"}),
    "pending": frozenset({"pending", "running", "infra_failed", "cancelled"}),
    "running": frozenset({"done", "failed", "infra_failed", "cancelled"}),
    "infra_failed": frozenset({"queued"}),  # retry
    "done": frozenset(),
    "failed": frozenset(),
    "cached": frozenset(),
    "cancelled": frozenset(),
    "skipped": frozenset(),
}
"""Legal action state changes (R7). `pending -> pending` updates the pending reason."""

_DOMAIN_RE: Final = re.compile(DOMAIN_PATTERN)


def check_domain(domain: object) -> str:
    """Return `domain` if it is a valid domain name, else raise MetadataError."""
    if not isinstance(domain, str) or _DOMAIN_RE.fullmatch(domain) is None:
        raise MetadataError(
            f"invalid domain {domain!r}: use letters, digits, '.', '_' or '-', starting with a "
            "letter or digit, at most 128 characters"
        )
    return domain


def check_transition(action_id: str, current: str, new: str) -> None:
    """Raise MetadataError unless `current -> new` is a legal action state change."""
    if new not in TRANSITIONS:
        raise MetadataError(
            f"action {action_id!r}: unknown state {new!r}; expected one of {sorted(TRANSITIONS)}"
        )
    if new not in TRANSITIONS[current]:
        allowed = sorted(TRANSITIONS[current]) or ["none, it is final"]
        raise MetadataError(
            f"action {action_id!r}: illegal state change {current} -> {new} "
            f"(allowed from {current}: {', '.join(allowed)})"
        )


def _digest(value: object) -> Digest:
    if isinstance(value, Digest):
        return value
    if isinstance(value, str):
        try:
            return Digest.parse(value)
        except DigestError as exc:
            raise ValueError(str(exc)) from None
    raise ValueError(f"expected a digest string, got {type(value).__name__}")


DigestField = Annotated[
    Digest,
    PlainValidator(_digest),
    PlainSerializer(str, return_type=str),
    WithJsonSchema({"type": "string", "pattern": DIGEST_SQL_PATTERN}),
]
"""A `Digest` field: accepts `Digest` or `"<algo>:<hex>"`, serializes as the string."""


def _logical_path(value: str) -> str:
    parts = value.split("/")
    if not value or value.startswith("/") or "\x00" in value or {".", ".."} & set(parts):
        raise ValueError(
            f"invalid logical path {value!r}: expected a relative POSIX path without '.' or '..'"
        )
    return value


LogicalPath = Annotated[StrictStr, Field(max_length=4096), AfterValidator(_logical_path)]
ActionId = Annotated[StrictStr, Field(min_length=1, max_length=512, pattern=r"^[^\x00-\x1f]+$")]
_Name = Annotated[StrictStr, Field(min_length=1, max_length=256, pattern=r"^[^\x00-\x1f]+$")]
_Text = Annotated[StrictStr, Field(max_length=4096)]
_NonNegative = Annotated[StrictInt, Field(ge=0)]
_Domain = Annotated[StrictStr, Field(pattern=DOMAIN_PATTERN)]

_FROZEN = ConfigDict(frozen=True, extra="forbid", strict=True)


class OutputResult(BaseModel):
    """One produced output: its content digest and the id passed downstream."""

    model_config = _FROZEN

    digest: DigestField  # content (blob digest or tree manifest digest)
    id: DigestField  # = digest if deterministic, else nondeterministic_output_id(key, name)
    type: Literal["file", "tree"]
    size: _NonNegative


class ResourceUsage(BaseModel):
    """Measured resource use, in whole units (canonical JSON has no floats)."""

    model_config = _FROZEN

    max_rss_kb: _NonNegative
    cpu_s: _NonNegative
    wall_s: _NonNegative


class RunnerInfo(BaseModel):
    """Where the result was produced; not part of the cache identity."""

    model_config = _FROZEN

    version: _Name
    host: _Name
    slurm_job_id: _Name | None = None


class ResultManifest(BaseModel):
    """What an action produced; the value of the action cache (interfaces.md § 6).

    `status="failed"` means the tool ran and reported failure, which is cacheable; infrastructure
    failures never produce a manifest. `inputs` maps each staged logical path to its input id and
    feeds the `in` provenance edges.
    """

    model_config = _FROZEN

    v: Literal[1] = 1
    action_key: DigestField
    status: Literal["passed", "failed"]
    exit_code: StrictInt
    inputs: dict[LogicalPath, DigestField]
    outputs: dict[_Name, OutputResult]
    log: DigestField | None
    summary: dict[_Name, StrictStr | StrictInt]
    resources: ResourceUsage
    runner: RunnerInfo

    def to_json(self) -> dict[str, object]:
        """The JSON form stored in the cache (digests as strings)."""
        return self.model_dump(mode="json")


class BlobRef(BaseModel):
    """A CAS object to register in the `blobs` table (GC bookkeeping, P1-09 R1)."""

    model_config = _FROZEN

    digest: DigestField
    size: _NonNegative
    kind: Literal["file", "tree"]


class BuildCreate(BaseModel):
    """Everything known about a build when it starts."""

    model_config = _FROZEN

    uuid: UUID
    domain: _Domain
    project: _Name
    plan_digest: DigestField
    flow_repo: _Text
    flow_commit: _Text
    flow_dirty: StrictBool
    user_name: _Name
    ci_job: _Name | None = None
    cache_mode: CacheMode


class BuildView(BuildCreate):
    """A stored build with its status and per-state action counts (`ebs status`)."""

    id: BuildId
    status: BuildStatus
    created_at: AwareDatetime
    finished_at: AwareDatetime | None
    pinned: StrictBool
    action_counts: dict[ActionState, _NonNegative]


class ActionRow(BaseModel):
    """One action of a build. `add_actions` takes rows; missing `queued_at` means now."""

    model_config = _FROZEN

    action_id: ActionId
    step: Annotated[StrictStr, Field(pattern=NAME_PATTERN)]
    key: DigestField | None = None
    state: ActionState = "queued"
    attempts: _NonNegative = 0
    slurm_job_id: _Name | None = None
    pending_reason: PendingReason | None = None
    infra_reason: InfraReason | None = None
    queued_at: AwareDatetime | None = None
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    cached: StrictBool = False
    result_key: DigestField | None = None


class DriverLease(BaseModel):
    """Who drives a build, until when (P2-08 R3). `owner` is opaque to the store
    (`host:pid:token` by convention)."""

    model_config = _FROZEN

    build: BuildId
    owner: _Name
    expires_at: AwareDatetime


class ActionUpdate(BaseModel):
    """The fields `set_action_state(**fields)` accepts; timestamps are set by the store."""

    model_config = ConfigDict(extra="forbid", strict=True)

    key: DigestField | None = None
    result_key: DigestField | None = None
    slurm_job_id: _Name | None = None
    pending_reason: PendingReason | None = None
    infra_reason: InfraReason | None = None


class Event(BaseModel):
    """A driver event (interfaces.md § 10); `ebs.driver.events` re-exports this model."""

    model_config = _FROZEN

    v: Literal[1] = 1
    ts: AwareDatetime
    build: BuildId
    type: EventType
    action_id: ActionId | None = None
    data: dict[str, object]


_EnvName = Annotated[StrictStr, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]


def _install_root(value: str) -> str:
    if not value.startswith("/") or "\x00" in value:
        raise ValueError(f"invalid install root {value!r}: expected an absolute path")
    return value


_InstallRoot = Annotated[StrictStr, Field(max_length=4096), AfterValidator(_install_root)]


class ToolchainCreate(BaseModel):
    """A toolchain snapshot to register (P1-07; data-model.md `toolchains`).

    `id` must equal `ebs.toolchain.model.toolchain_id(module, version, fingerprint, env)`: the
    registry computes it, and checks it again on every read (stores cannot, see `_common`).
    `name` is the module without its version (`questa` for `questa/2025.2`).
    """

    model_config = _FROZEN

    id: DigestField
    name: _Name
    module: _Name
    version: _Name
    fingerprint: DigestField
    env: dict[_EnvName, StrictStr]
    install_roots: Annotated[list[_InstallRoot], Field(min_length=1)]
    registered_by: _Name


class ToolchainRecord(ToolchainCreate):
    """A registered toolchain. Only `superseded_by` and `last_used_at` ever change."""

    registered_at: AwareDatetime
    superseded_by: DigestField | None
    last_used_at: AwareDatetime | None


class RegisterOutcome(BaseModel):
    """What `register_toolchain` did: whether it inserted the row, and which rows of the same
    module it marked as superseded by this one."""

    model_config = _FROZEN

    inserted: StrictBool
    superseded: tuple[DigestField, ...]


class MetadataStore(Protocol):
    """Shared build state. Every method is one short transaction; errors raise MetadataError."""

    # action cache
    def cache_get(self, domain: str, key: Digest) -> ResultManifest | None:
        """The cached result of `key` in `domain`, or None. Counts a hit and touches
        `last_access` at most once per hour."""
        ...

    def cache_put(self, domain: str, key: Digest, result: ResultManifest) -> bool:
        """Insert if absent; True if this call inserted, False if the key was already cached."""
        ...

    def cache_replace_failed(self, domain: str, key: Digest, result: ResultManifest) -> bool:
        """Replace the cached result of `key` only if it is a test failure (`--rerun-failed`);
        True if replaced. Never inserts and never overwrites a passed result (P0-15)."""
        ...

    # builds
    def create_build(self, b: BuildCreate) -> BuildId: ...

    def add_actions(self, build: BuildId, actions: Sequence[ActionRow]) -> None:
        """Add actions to a running build, all or nothing; duplicate action ids are refused."""
        ...

    def set_action_state(
        self, build: BuildId, action_id: str, state: ActionState, **fields: object
    ) -> None:
        """Move an action along the state machine (`TRANSITIONS`) and set `fields`
        (`ActionUpdate`). Timestamps and `attempts` are maintained by the store."""
        ...

    def record_result(self, build: BuildId, action_id: str, result: ResultManifest) -> None:
        """Store the action's key, result key and manifest, its provenance edges and its output
        blobs, in one transaction. Idempotent; a later manifest for the same key replaces the
        stored one. Does not change the action's state."""
        ...

    def get_result(self, build: BuildId, action_id: str) -> ResultManifest | None:
        """The manifest last recorded for this action of this build, or None (P0-15: the
        driver reads it when the executor reports `done`, in every cache mode)."""
        ...

    def finish_build(self, build: BuildId, status: BuildStatus) -> None: ...

    def get_build(self, build: BuildId) -> BuildView: ...

    def list_actions(self, build: BuildId, *, state: ActionState | None = None) -> list[ActionRow]:
        """Actions of `build` ordered by action id, optionally only those in `state`."""
        ...

    def resolve_output(self, domain: str, object_id: Digest) -> Digest | None:
        """The content digest of an output whose passed-down id is `object_id` (its `out`
        provenance edge), or None if no result recorded it. For a deterministic output the id
        is the content; for a `deterministic: false` one, the first recorded bytes win."""
        ...

    # access tracking
    def touch(self, domain: str, digests: Iterable[Digest]) -> None:
        """Mark known blobs as used now (at most once per hour each); unknown digests are
        ignored."""
        ...

    def register_blobs(self, domain: str, blobs: Iterable[BlobRef]) -> None:
        """Record CAS objects just stored (P1-09 R1): new rows get `created_at = last_access =
        now`; known rows are touched like `touch` (at most once per hour)."""
        ...

    # leases and pins (P1-09 R2)
    def acquire_lease(
        self, domain: str, build: BuildId, digests: Iterable[Digest], ttl_s: int
    ) -> LeaseId:
        """Protect `digests` (and, through trees, everything below them) from GC until
        `now + ttl_s`. The build must be in `domain` and running."""
        ...

    def renew_lease(
        self, build: BuildId, lease: LeaseId, ttl_s: int, *, add: Iterable[Digest] = ()
    ) -> None:
        """Extend a live lease of `build` to `now + ttl_s` and add `add` to it. An unknown or
        expired lease (GC may already have dropped it) raises MetadataError: acquire a new one."""
        ...

    def pin_build(self, build: BuildId, *, by: str, reason: str = "") -> None:
        """Make the build's plan, outputs and logs GC roots (idempotent; the first pin's author
        and reason are kept)."""
        ...

    def unpin_build(self, build: BuildId) -> bool:
        """Remove the pin; True if the build was pinned."""
        ...

    # driver lease (P2-08 R3)
    def claim_driver(self, build: BuildId, owner: str, ttl_s: int) -> DriverLease:
        """Take or renew the build's driver lease, atomically: it becomes `owner`'s until
        `now + ttl_s` if the build has none, it expired, or `owner` holds it. Returns the lease
        as it now stands: its owner differs from `owner` when another driver holds it. The build
        must be running; `1 <= ttl_s <= MAX_LEASE_TTL_S`."""
        ...

    def release_driver(self, build: BuildId, owner: str) -> None:
        """Drop the build's driver lease if `owner` holds it; otherwise do nothing."""
        ...

    # events
    def emit(self, event: Event) -> None: ...

    # toolchain registry (P1-07)
    def register_toolchain(self, spec: ToolchainCreate, manifest: bytes) -> RegisterOutcome:
        """Register an immutable toolchain snapshot with its fingerprint manifest, atomically.

        A new id is inserted. An id already registered is left as it is (its manifest too). In
        both cases this id becomes the module's current toolchain: every other non-superseded
        row of the module gets `superseded_by = spec.id`, and a superseded row registered again
        (a reverted install) has its `superseded_by` cleared. Concurrent registrations of one
        module are serialized, so a module has at most one current toolchain.
        """
        ...

    def get_toolchain(self, toolchain_id: Digest) -> ToolchainRecord | None: ...

    def current_toolchain(self, module: str) -> ToolchainRecord | None:
        """The module's non-superseded toolchain (latest registered if ever several), or None."""
        ...

    def list_toolchains(
        self, *, module: str | None = None, current_only: bool = False
    ) -> list[ToolchainRecord]:
        """Toolchains ordered by (module, registered_at, id)."""
        ...

    def toolchain_manifest(self, toolchain_id: Digest) -> bytes | None:
        """The manifest stored by `register_toolchain` (opaque bytes), or None."""
        ...

    def touch_toolchain(self, toolchain_id: Digest) -> None:
        """Mark a toolchain as used now (at most once per hour); unknown ids are ignored."""
        ...


# --- HTTP wire models (P1-06) --------------------------------------------------------------------
# The metadata service (`ebs.meta.service`) and its client (`ebs.meta.client`) exchange the models
# above plus these envelopes; both sides parse JSON with `model_validate_json` (strict models
# accept ISO datetimes and UUID strings only in JSON mode).

API_PREFIX: Final = "/api/v1"
"""Every REST route of the metadata service lives below this prefix."""

BATCH_MAX: Final = 500
"""Most items one batch request may carry (`add_actions`, `touch`, `cache_get_many`)."""

_WIRE = ConfigDict(frozen=True, extra="forbid", strict=True)


class ActionBatch(BaseModel):
    """Body of `POST /builds/{id}/actions`: at most `BATCH_MAX` rows, added all or nothing."""

    model_config = _WIRE

    actions: Annotated[list[ActionRow], Field(max_length=BATCH_MAX)]


class DigestBatch(BaseModel):
    """Body of `touch` and `cache_get_many`: at most `BATCH_MAX` digests."""

    model_config = _WIRE

    digests: Annotated[list[DigestField], Field(max_length=BATCH_MAX)]


class StateChange(BaseModel):
    """Body of `set_action_state`; the store validates `state` and `fields` (`ActionUpdate`)."""

    model_config = _WIRE

    state: Annotated[StrictStr, Field(max_length=64)]
    fields: dict[StrictStr, object] = Field(default_factory=dict)


class BuildFinish(BaseModel):
    """Body of `finish_build`; the store checks that `status` is final."""

    model_config = _WIRE

    status: Annotated[StrictStr, Field(max_length=64)]


class CreatedBuild(BaseModel):
    model_config = _WIRE

    id: StrictInt


class Changed(BaseModel):
    """Whether a `cache_put` inserted or a `cache_replace_failed` replaced the entry."""

    model_config = _WIRE

    changed: StrictBool


class OptionalResult(BaseModel):
    model_config = _WIRE

    result: ResultManifest | None


class OptionalDigest(BaseModel):
    model_config = _WIRE

    digest: DigestField | None


class ActionList(BaseModel):
    model_config = _WIRE

    actions: list[ActionRow]


class CacheHits(BaseModel):
    """Response of `cache_get_many`: only the keys that hit, by their digest string."""

    model_config = _WIRE

    results: dict[StrictStr, ResultManifest]


class ErrorBody(BaseModel):
    """Every error response: an actionable message (`MetadataError` text on the client)."""

    model_config = _WIRE

    detail: StrictStr


class ToolchainRegistration(BaseModel):
    """Body of `POST /toolchains`: the snapshot and its manifest, base64 (standard alphabet)."""

    model_config = _WIRE

    spec: ToolchainCreate
    manifest_b64: StrictStr


class ToolchainManifest(BaseModel):
    """Response of `GET /toolchains/{id}/manifest`: base64, or None for an unknown id."""

    model_config = _WIRE

    manifest_b64: StrictStr | None


class BlobBatch(BaseModel):
    """Body of `register_blobs`: at most `BATCH_MAX` objects."""

    model_config = _WIRE

    blobs: Annotated[list[BlobRef], Field(max_length=BATCH_MAX)]


class LeaseRequest(BaseModel):
    """Body of `acquire_lease` and `renew_lease` (whose `digests` are added to the lease)."""

    model_config = _WIRE

    domain: _Domain | None = None  # acquire only
    digests: Annotated[list[DigestField], Field(max_length=BATCH_MAX)] = Field(default_factory=list)
    ttl_s: Annotated[StrictInt, Field(ge=1, le=MAX_LEASE_TTL_S)]


class DriverClaim(BaseModel):
    """Body of `claim_driver`."""

    model_config = _WIRE

    owner: _Name
    ttl_s: Annotated[StrictInt, Field(ge=1, le=MAX_LEASE_TTL_S)]


class DriverRelease(BaseModel):
    """Body of `release_driver`."""

    model_config = _WIRE

    owner: _Name


class CreatedLease(BaseModel):
    model_config = _WIRE

    id: StrictInt


class PinRequest(BaseModel):
    """Body of `pin_build`; the pin's author is the authenticated caller."""

    model_config = _WIRE

    reason: _Text = ""


class ToolchainList(BaseModel):
    """Response of the toolchain reads; `get` and `current` hold zero or one record."""

    model_config = _WIRE

    toolchains: list[ToolchainRecord]


# --- garbage collection (P1-09) ------------------------------------------------------------------
# `GcStore` is the GC's view of the metadata database. Only the PostgreSQL and in-memory stores
# implement it: GC runs as a service account with direct database access (R7), never over HTTP.


@dataclass(frozen=True, slots=True)
class DomainPolicy:
    """The GC settings of a domain (`domains` table; defaults when the domain has no row)."""

    ttl_days: int = 30
    quota_bytes: int | None = None
    high_water: float = 0.85
    low_water: float = 0.75


@dataclass(frozen=True, slots=True)
class GcBlob:
    """A `blobs` row."""

    digest: Digest
    size: int
    kind: str
    created_at: datetime
    last_access: datetime


@dataclass(frozen=True, slots=True)
class GcCacheEntry:
    """An action-cache row as GC sees it: what it references and when it was last used."""

    key: Digest
    last_access: datetime
    outputs: tuple[Digest, ...]  # content digests
    log: Digest | None


@dataclass(frozen=True, slots=True)
class GcRoots:
    """Digests GC must never delete (with everything below them, I15)."""

    pinned: frozenset[Digest]  # plan, outputs and logs of pinned builds
    leased: frozenset[Digest]  # objects of live leases
    released: frozenset[Digest]  # release outputs (P2-02; empty until the table exists)


@dataclass(frozen=True, slots=True)
class Tombstone:
    """An object scheduled for deletion: its cache rows went at `deleted_rows_at` (or it was
    unreachable then); its bytes may go after `blob_delete_after` if still unused (I15)."""

    digest: Digest
    deleted_rows_at: datetime
    blob_delete_after: datetime


@dataclass(frozen=True, slots=True)
class GcAttribution:
    """An object recorded as a plan, output or log of an action of a build (for reports)."""

    digest: Digest
    build: BuildId
    project: str
    step: str


@dataclass(frozen=True, slots=True)
class GcRun:
    """One `gc_runs` row."""

    domain: str
    started_at: datetime
    finished_at: datetime
    marked: int
    swept_rows: int
    swept_bytes: int
    dry_run: bool
    report: dict[str, object]  # canonical JSON


class GcStore(Protocol):
    """What GC reads and changes in the metadata database. Writes re-check their condition in
    the same transaction, so a build running concurrently always wins (R7)."""

    def gc_policy(self, domain: str) -> DomainPolicy: ...

    def gc_blobs(self, domain: str) -> list[GcBlob]: ...

    def gc_cache_entries(self, domain: str) -> list[GcCacheEntry]: ...

    def gc_roots(self, domain: str, now: datetime) -> GcRoots:
        """Pins, releases and the leases still live at `now`."""
        ...

    def gc_tombstones(self, domain: str) -> list[Tombstone]: ...

    def gc_attribution(self, domain: str) -> list[GcAttribution]: ...

    def register_blobs(self, domain: str, blobs: Iterable[BlobRef]) -> None: ...

    def gc_expire_leases(self, domain: str, now: datetime) -> int:
        """Delete leases expired at `now`; returns how many."""
        ...

    def gc_delete_cache_entries(
        self, domain: str, keys: Iterable[Digest], *, unused_before: datetime
    ) -> list[Digest]:
        """Delete the entries among `keys` whose `last_access` is still at or before
        `unused_before` (a hit since the snapshot keeps the row); returns the deleted keys."""
        ...

    def gc_tombstone(
        self, domain: str, digests: Iterable[Digest], *, now: datetime, delete_after: datetime
    ) -> None:
        """Tombstone `digests`; an existing tombstone is kept as it is."""
        ...

    def gc_drop_tombstones(self, domain: str, digests: Iterable[Digest]) -> None: ...

    def gc_claim(self, domain: str, digest: Digest, now: datetime) -> bool:
        """Atomically: if `digest` has a tombstone due at `now` and its blob row was not touched
        since the tombstone, delete both and return True (the caller then deletes the bytes);
        else False."""
        ...

    def gc_record_run(self, run: GcRun) -> None: ...
