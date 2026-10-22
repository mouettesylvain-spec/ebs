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
    StringConstraints,
    WithJsonSchema,
)

from ebs.core.digest import Digest
from ebs.core.errors import DigestError, MetadataError
from ebs.flow.model import LICENSE_PATTERN, NAME_PATTERN

__all__ = [
    "API_PREFIX",
    "BATCH_MAX",
    "CHANNEL_PATTERN",
    "DIGEST_SQL_PATTERN",
    "DOMAIN_PATTERN",
    "DRIVER_LEASE_TTL_S",
    "EVENT_PAGE_MAX",
    "EVENT_TYPE_PATTERN",
    "FEATURE_PATTERN",
    "FINAL_BUILD_STATUSES",
    "FINAL_QUEUE_STATES",
    "IMPORT_ALIAS_PATTERN",
    "LEASE_TTL_S",
    "LOG_CHUNK_MAX",
    "MAX_LEASE_TTL_S",
    "PROVENANCE_DEPTH_MAX",
    "PROVENANCE_START_MAX",
    "QUEUE_LEASE_TTL_S",
    "RELEASE_NAME_PATTERN",
    "STATS_WINDOW_MAX_DAYS",
    "TERMINAL_STATES",
    "TOUCH_INTERVAL_S",
    "TRANSITIONS",
    "VERSION_PATTERN",
    "ActionBatch",
    "ActionId",
    "ActionList",
    "ActionRow",
    "ActionState",
    "ActionUpdate",
    "ArchiveList",
    "ArchiveRecord",
    "ArchiveRequest",
    "BlobBatch",
    "BlobRef",
    "BuildCreate",
    "BuildFilter",
    "BuildFinish",
    "BuildId",
    "BuildList",
    "BuildStatus",
    "BuildView",
    "BuildViewList",
    "CacheHits",
    "CacheMode",
    "Changed",
    "ChannelHistory",
    "ChannelList",
    "ChannelMove",
    "ChannelSet",
    "ChannelView",
    "CreatedBuild",
    "CreatedLease",
    "DashboardReader",
    "DigestBatch",
    "DigestField",
    "DomainPolicy",
    "DomainStats",
    "DriverClaim",
    "DriverLease",
    "DriverRelease",
    "ErrorBody",
    "Event",
    "EventReader",
    "EventType",
    "ExportList",
    "ExportRecord",
    "ExportRequest",
    "GateCheck",
    "GateQuery",
    "GcAttribution",
    "GcBlob",
    "GcCacheEntry",
    "GcLock",
    "GcRoots",
    "GcRun",
    "GcStore",
    "InfraReason",
    "LeaseId",
    "LeaseRequest",
    "MetadataBackend",
    "MetadataStore",
    "OptionalDigest",
    "OptionalQueueLease",
    "OptionalResult",
    "OutputResult",
    "ParamJson",
    "PendingReason",
    "PendingWait",
    "PilotQueueStore",
    "PilotSlot",
    "PinRequest",
    "ProvenanceAction",
    "ProvenanceEdge",
    "ProvenanceGraph",
    "ProvenanceQuery",
    "ProvenanceRelease",
    "ProvenanceStore",
    "QueueEntries",
    "QueueEntry",
    "QueueFinish",
    "QueueIds",
    "QueueItem",
    "QueueLease",
    "QueueLeaseRequest",
    "QueuePut",
    "QueueRenew",
    "QueueState",
    "RegisterOutcome",
    "ReleaseCreate",
    "ReleaseId",
    "ReleaseImport",
    "ReleaseImportBatch",
    "ReleaseImportList",
    "ReleaseList",
    "ReleaseOutput",
    "ReleaseStore",
    "ReleaseView",
    "ResourceUsage",
    "ResultManifest",
    "ResultSummary",
    "ResultSummaryList",
    "RunnerInfo",
    "StateChange",
    "StepStats",
    "StoredEvent",
    "Tombstone",
    "ToolchainCreate",
    "ToolchainList",
    "ToolchainManifest",
    "ToolchainRecord",
    "ToolchainRegistration",
    "YankRequest",
    "check_domain",
    "check_transition",
    "release_output_name",
    "slot_fits",
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
    sandbox: _Name | None = None  # P3-01 R5: staging | bwrap | apptainer; None before P3-01


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
    final_plan_digest: DigestField | None = None  # set by finish_build (revision 0012)


ParamJson = StrictBool | StrictInt | StrictStr | list[StrictStr]
"""A plan param as stored with its action (`ebs.plan.types.ParamValue`, tuples as lists)."""


class ActionRow(BaseModel):
    """One action of a build. `add_actions` takes rows; missing `queued_at` means now.

    `rule`, `params` and `toolchain` are what the plan says the action is (P2-04: provenance
    queries show them and `ebs tests` selects `*.sim` rules by them); they are set when the
    action is added and never change. Rows written before P2-04 hold None.
    """

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
    rule: _Name | None = None  # the rule kind, e.g. "questa.sim"
    params: dict[_Name, ParamJson] | None = None
    toolchain: DigestField | None = None


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

    def cache_peek(self, domain: str, key: Digest) -> ResultManifest | None:
        """`cache_get` without its side effects: no hit counted, no touch (`ebs plan`'s
        prediction, which uses nothing)."""
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

    def finish_build(
        self, build: BuildId, status: BuildStatus, *, final_plan: Digest | None = None
    ) -> None:
        """Set the final status. `final_plan` is the plan as refined at the end (every key that
        could be computed), stored in the CAS; a pinned build roots it with its plan."""
        ...

    def record_release_imports(self, build: BuildId, imports: Sequence[ReleaseImport]) -> None:
        """Record which release each import alias of `build` used (P2-03 R5, for `used-by`),
        all or nothing. Every release must be in the build's domain; recording the same row
        again is a no-op, another release for a recorded alias is refused."""
        ...

    def list_release_imports(self, build: BuildId) -> list[ReleaseImport]:
        """The release imports of `build`, ordered by alias."""
        ...

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
    final_plan: DigestField | None = None


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
    next: StrictStr | None = None  # the cursor of the next page (P2-05 R3)


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
    next: StrictStr | None = None  # the cursor of the next page (P2-05 R3)


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

    pinned: frozenset[Digest]  # plan, final plan, outputs and logs of pinned builds
    leased: frozenset[Digest]  # objects of live leases
    released: frozenset[Digest]  # content digests of release outputs, yanked ones too (P2-02)


@dataclass(frozen=True, slots=True)
class GcLock:
    """Who runs GC on a domain, until when (one run per domain at a time)."""

    domain: str
    owner: str  # host:pid:token
    expires_at: datetime


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

    def gc_lock(self, domain: str, owner: str, *, now: datetime, ttl_s: int) -> GcLock:
        """Take the domain's GC lock until `now + ttl_s` if it is free, expired or already
        `owner`'s (a renewal); returns the lock as it now stands, so another owner means
        another GC run holds it. `1 <= ttl_s <= MAX_LEASE_TTL_S`."""
        ...

    def gc_unlock(self, domain: str, owner: str) -> None:
        """Drop the domain's GC lock if `owner` holds it, else do nothing."""
        ...


# --- releases and channels (P2-02) ---------------------------------------------------------------
# A release is a named, versioned selection of a finished build's outputs; a channel is a movable
# pointer to a release of the same name, with its full history (data-model.md, Phase 2).

RELEASE_NAME_PATTERN: Final = r"^[a-z0-9][a-z0-9._-]{0,63}(/[a-z0-9][a-z0-9._-]{0,63})?$"
"""`team/product` or `product`, e.g. `rtl-team/cpu`."""

VERSION_PATTERN: Final = r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$"
"""Release versions (decision D4): free-form; CalVer `YYYY.MM.N` is recommended."""

CHANNEL_PATTERN: Final = r"^[a-z][a-z0-9_-]{0,63}$"
"""Channel names, e.g. `stable`, `nightly`."""

ReleaseId = NewType("ReleaseId", int)

_ReleaseName = Annotated[StrictStr, Field(pattern=RELEASE_NAME_PATTERN)]
_Version = Annotated[StrictStr, Field(pattern=VERSION_PATTERN)]
_Channel = Annotated[StrictStr, Field(pattern=CHANNEL_PATTERN)]


IMPORT_ALIAS_PATTERN: Final = r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"
"""Import aliases as flows name them (`imports: {rtl: …}` -> `${imports.rtl}`)."""


class ReleaseImport(BaseModel):
    """One `release_imports` row: a build consumed `release` through import `alias`."""

    model_config = _FROZEN

    alias: Annotated[StrictStr, Field(pattern=IMPORT_ALIAS_PATTERN)]
    release: ReleaseId


def release_output_name(action_id: str, output: str) -> str:
    """The name of an action's output in a release: `<action id>.<output>`."""
    return f"{action_id}.{output}"


class ReleaseOutput(BaseModel):
    """One released output: where it comes from and the result it had in the build."""

    model_config = _FROZEN

    action_id: ActionId
    output: _Name
    digest: DigestField  # content
    id: DigestField  # the id passed downstream (= digest if deterministic)
    type: Literal["file", "tree"]
    size: _NonNegative


class ReleaseCreate(BaseModel):
    """A release to create. `outputs` maps `release_output_name(action, output)` to the result
    the action recorded; the store checks every entry against the build (P2-02 R1).
    `allow_failed` is the reason given for releasing outputs of actions that did not pass."""

    model_config = _FROZEN

    domain: _Domain
    name: _ReleaseName
    version: _Version
    build: BuildId
    outputs: Annotated[dict[_Name, ReleaseOutput], Field(min_length=1)]
    notes: _Text = ""
    allow_failed: Annotated[StrictStr, Field(min_length=1, max_length=4096)] | None = None


class ReleaseView(ReleaseCreate):
    """A stored release. Immutable: only the yank fields are ever set, once (I19)."""

    id: ReleaseId
    created_at: AwareDatetime
    created_by: _Name
    exported_from: ReleaseId | None
    yanked_at: AwareDatetime | None
    yanked_by: _Name | None
    yank_reason: _Text | None


class GateCheck(BaseModel):
    """A passed channel gate: `build` is a passing build of project `flow` that consumed the
    release (provenance edges), checked when the channel moved."""

    model_config = _FROZEN

    flow: _Name
    build: BuildId


class ChannelMove(BaseModel):
    """One entry of a channel's history."""

    model_config = _FROZEN

    id: StrictInt
    domain: _Domain
    name: _ReleaseName
    channel: _Channel
    release: ReleaseId
    version: _Version
    moved_at: AwareDatetime
    moved_by: _Name
    gate: GateCheck | None


class ChannelView(BaseModel):
    model_config = _FROZEN

    domain: _Domain
    name: _ReleaseName
    channel: _Channel
    release: ReleaseView
    updated_at: AwareDatetime
    updated_by: _Name


class ExportRecord(BaseModel):
    """The audit row of a cross-domain export (`exports` table)."""

    model_config = _FROZEN

    id: StrictInt
    release: ReleaseId  # the exported release, in its own domain
    exported: ReleaseId  # the release created in `to_domain`
    to_domain: _Domain
    exported_at: AwareDatetime
    exported_by: _Name


_Location = Annotated[StrictStr, Field(min_length=1, max_length=4096, pattern=r"^[^\x00-\x1f]+$")]


class ArchiveRecord(BaseModel):
    """A sign-off bundle of a release written to cold storage (`archives` table, P3-04 R2)."""

    model_config = _FROZEN

    id: StrictInt
    release: ReleaseId
    bundle: DigestField  # the digest of the bundle file's bytes
    location: _Location  # where it was written (an absolute path on the archiving host)
    created_at: AwareDatetime
    created_by: _Name


class ReleaseStore(Protocol):
    """Releases, channels, exports and archives (P2-02, P3-04). Implemented by the PostgreSQL,
    in-memory and HTTP stores; there is no method to change or delete a release other than
    `yank_release` (I19)."""

    def create_release(self, spec: ReleaseCreate, *, by: str) -> ReleaseView:
        """Insert a release. The build must be in `spec.domain` and finished; every output must
        be one its action recorded (same digest, id, type, size), from an action that passed
        unless `allow_failed` gives a reason. `(domain, name, version)` is unique."""
        ...

    def get_release(self, domain: str, name: str, version: str) -> ReleaseView | None: ...

    def get_release_by_id(self, release: ReleaseId) -> ReleaseView | None: ...

    def list_releases(self, domain: str, *, name: str | None = None) -> list[ReleaseView]:
        """Ordered by (name, created_at, id)."""
        ...

    def yank_release(self, release: ReleaseId, *, by: str, reason: str) -> ReleaseView:
        """Mark the release yanked (idempotent: the first yank is kept). Never deletes it."""
        ...

    def set_channel(
        self,
        domain: str,
        name: str,
        channel: str,
        release: ReleaseId,
        *,
        by: str,
        gate: GateCheck | None = None,
    ) -> ChannelMove:
        """Point the channel at `release` (of this domain and name, not yanked) and append the
        move, with its gate check, to the channel's history."""
        ...

    def get_channel(self, domain: str, name: str, channel: str) -> ChannelView | None: ...

    def channel_history(self, domain: str, name: str, channel: str) -> list[ChannelMove]:
        """Oldest first."""
        ...

    def gate_builds(self, domain: str, flow: str, ids: Iterable[Digest]) -> list[BuildId]:
        """Passed builds of project `flow` in `domain` with an action that consumed one of
        `ids` (an `in` provenance edge), newest first."""
        ...

    def record_export(self, release: ReleaseId, to_domain: str, *, by: str) -> ReleaseView:
        """Create the release in `to_domain` (same name, version, outputs; `exported_from`) and
        its audit row, atomically. The caller has copied the bytes into that domain's CAS."""
        ...

    def list_exports(self, release: ReleaseId) -> list[ExportRecord]: ...

    def record_archive(
        self, release: ReleaseId, bundle: Digest, location: str, *, by: str
    ) -> ArchiveRecord:
        """Record that the sign-off bundle `bundle` of `release` was written to `location`
        (append-only). The same release, bundle and location again returns the first record."""
        ...

    def list_archives(self, release: ReleaseId) -> list[ArchiveRecord]:
        """Oldest first."""
        ...


# --- provenance (P2-04) ---------------------------------------------------------------------------
# The `provenance_edges` written since phase 0, walked within one domain: crossing domains is the
# caller's job (`ebs.release.provenance`), through export audit rows only (R4).

PROVENANCE_DEPTH_MAX: Final = 200
"""Deepest walk a store performs, in action levels (data-model.md, "Query patterns")."""

PROVENANCE_START_MAX: Final = 10_000
"""Most ids plus keys one walk may start from over HTTP (a release with many outputs, e.g. a
regression's results, is one walk: walks cannot be split into batches and merged exactly)."""


class ProvenanceEdge(BaseModel):
    """One `provenance_edges` row: `in` holds an input id at its logical path, `out` an output
    (by name) with its passed-down id and its content digest."""

    model_config = _FROZEN

    action_key: DigestField
    direction: Literal["in", "out"]
    logical_path: Annotated[StrictStr, Field(min_length=1, max_length=4096)]
    object_id: DigestField
    content_digest: DigestField | None


class ProvenanceAction(BaseModel):
    """An action row whose key a walk reached, with its build. Several builds may hold the same
    key (cache hits); each row is listed."""

    model_config = _FROZEN

    key: DigestField
    build: BuildId
    action_id: ActionId
    step: _Name
    rule: _Name | None
    params: dict[_Name, ParamJson] | None
    toolchain: DigestField | None
    state: ActionState
    cached: StrictBool
    status: Literal["passed", "failed"] | None  # of the recorded result; None without one
    finished_at: AwareDatetime | None
    project: _Name
    flow_repo: _Text
    flow_commit: _Text
    flow_dirty: StrictBool
    user_name: _Name
    build_created_at: AwareDatetime


class ProvenanceRelease(BaseModel):
    """A release output of the domain whose id is in the graph."""

    model_config = _FROZEN

    release: ReleaseId
    name: _ReleaseName
    version: _Version
    output: _Name  # `release_output_name(action, output)`
    object_id: DigestField
    exported_from: ReleaseId | None
    yanked: StrictBool


class ProvenanceGraph(BaseModel):
    """The result of a walk: every edge of the walked actions (sorted by key, direction, path),
    their action rows (by build, action id), the releases holding an id of the graph (by id,
    output) and whether `depth` stopped the walk before its end."""

    model_config = _FROZEN

    domain: _Domain
    edges: list[ProvenanceEdge]
    actions: list[ProvenanceAction]
    releases: list[ProvenanceRelease]
    truncated: StrictBool


class ProvenanceStore(Protocol):
    """Walks of the provenance graph within one domain (P2-04; PostgreSQL as a recursive CTE,
    in-memory and HTTP stores). `depth` counts action levels, 1..PROVENANCE_DEPTH_MAX."""

    def provenance_upstream(
        self, domain: str, *, ids: Iterable[Digest], keys: Iterable[Digest], depth: int
    ) -> ProvenanceGraph:
        """Where `ids` came from: level 1 is the actions in `keys` plus those that produced an
        id of `ids` (an `out` edge whose id or content digest it is); level n+1 the producers
        of the inputs of level n."""
        ...

    def provenance_downstream(
        self, domain: str, *, ids: Iterable[Digest], keys: Iterable[Digest], depth: int
    ) -> ProvenanceGraph:
        """What consumed `ids`: level 1 is the actions in `keys` plus those with an `in` edge
        holding an id of `ids`; level n+1 the consumers of the outputs of level n."""
        ...


# --- pilot queue (P3-06) -------------------------------------------------------------------------

QUEUE_LEASE_TTL_S: Final = 60
"""Visibility timeout of a leased queue item: a pilot renews it every third of it while its
runner works; a pilot silent for longer is presumed dead and its item is `lost`."""

QueueState = Literal["queued", "leased", "done", "lost", "cancelled"]
"""`queued` waits for a pilot; `leased` runs on one; `done` has the runner's exit code; `lost`:
its pilot stopped renewing the lease (an infrastructure failure, retried by the driver);
`cancelled`: the executor withdrew it (a leased one's pilot stops its runner)."""

FINAL_QUEUE_STATES: Final[frozenset[str]] = frozenset({"done", "lost", "cancelled"})

FEATURE_PATTERN: Final = r"^[A-Za-z0-9_.-]+$"
"""One SLURM node feature (a constraint is features joined by `&`)."""

_LicenseName = Annotated[StrictStr, StringConstraints(pattern=LICENSE_PATTERN)]
_Positive = Annotated[StrictInt, Field(ge=1)]
_Feature = Annotated[StrictStr, StringConstraints(pattern=FEATURE_PATTERN, max_length=256)]
_ExitCode = Annotated[StrictInt, Field(ge=-128, le=255)]  # negative: killed by that signal


def _sorted_features(value: tuple[str, ...]) -> tuple[str, ...]:
    if list(value) != sorted(set(value)):
        raise ValueError("must be sorted and unique")
    return value


_Features = Annotated[tuple[_Feature, ...], AfterValidator(_sorted_features)]
_Nodes = Annotated[tuple[_Name, ...], AfterValidator(_sorted_features)]


class QueueItem(BaseModel):
    """One action for the pilots of a pool: what the runner needs to find it, and what it needs
    of a pilot's allocation (`slot_fits`). `features` are the node features of its constraint."""

    model_config = _FROZEN

    action_id: ActionId
    plan: DigestField
    build: BuildId | None = None  # None: the runner prints its result instead of posting it
    cpus: _Positive = 1
    mem_mb: _Positive | None = None
    time_s: _Positive | None = None
    licenses: dict[_LicenseName, _Positive] = Field(default_factory=dict)
    features: _Features = ()
    exclude: _Nodes = ()  # P1-05: nodes it must not run on (a pilot there never leases it)


class PilotSlot(BaseModel):
    """What one pilot offers when it asks for work (P3-06 R3): its allocation, the walltime it
    has left for an action, and the licenses and node features its job was granted."""

    model_config = _FROZEN

    pilot: _Name  # the pilot's SLURM job id (array task), as it reports itself
    node: _Name | None = None
    cpus: _Positive
    mem_mb: _Positive | None = None  # None: the job has no memory limit
    time_left_s: _NonNegative
    licenses: dict[_LicenseName, _Positive] = Field(default_factory=dict)
    features: _Features = ()


def slot_fits(slot: PilotSlot, item: QueueItem) -> bool:
    """Whether a pilot with `slot` may run `item` (R3): every resource and license count it
    asks for is within the allocation, its node features are the pilot's, the pilot's node is
    not excluded, and its time limit (if any) fits the walltime left. Every store backend
    leases by exactly this rule."""
    if item.cpus > slot.cpus or (item.time_s or 0) > slot.time_left_s:
        return False
    if item.mem_mb is not None and slot.mem_mb is not None and item.mem_mb > slot.mem_mb:
        return False
    if any(slot.licenses.get(name, 0) < count for name, count in item.licenses.items()):
        return False
    if slot.node is not None and slot.node in item.exclude:
        return False
    return set(item.features) <= set(slot.features)


class QueueLease(BaseModel):
    """An item a pilot leased: run it, renew the lease before `expires_at`, then finish it."""

    model_config = _FROZEN

    item: StrictInt
    action_id: ActionId
    plan: DigestField
    build: BuildId | None
    expires_at: AwareDatetime


class QueueEntry(BaseModel):
    """Where one queue item stands; `exit_code` and `infra_reason` are the runner's (`done`)."""

    model_config = _FROZEN

    item: StrictInt
    action_id: ActionId
    state: QueueState
    pilot: _Name | None = None
    node: _Name | None = None
    exit_code: _ExitCode | None = None
    infra_reason: InfraReason | None = None


class PilotQueueStore(Protocol):
    """Per-build action queues that pilot jobs pull from (P3-06, interfaces.md § 6).

    A pool is one executor's queue (a UUID it draws), in one domain; items are numbered by the
    store. Leasing takes the oldest queued item that fits the pilot's slot, and concurrent
    pilots never lease the same item (PostgreSQL: `FOR UPDATE SKIP LOCKED`). A lease not renewed
    within its TTL is `lost`: renewing or finishing it then returns False, and the pilot stops
    its runner. Every method is one short transaction.
    """

    def queue_put(self, domain: str, pool: UUID, items: Sequence[QueueItem]) -> list[int]:
        """Queue `items`, all or nothing; their ids, in order."""
        ...

    def queue_lease(
        self, domain: str, pool: UUID, slot: PilotSlot, ttl_s: int
    ) -> QueueLease | None:
        """Lease the oldest queued item of the pool that fits `slot` until `now + ttl_s`, or
        None if there is none."""
        ...

    def queue_renew(self, domain: str, pool: UUID, item: int, pilot: str, ttl_s: int) -> bool:
        """Extend `pilot`'s live lease of `item` to `now + ttl_s`; False if it is not (any more)
        leased by `pilot`: it was cancelled or lost, so stop the runner."""
        ...

    def queue_finish(
        self,
        domain: str,
        pool: UUID,
        item: int,
        pilot: str,
        exit_code: int,
        infra_reason: InfraReason | None = None,
    ) -> bool:
        """Record the runner's exit for `pilot`'s live lease of `item` (state `done`); False,
        changing nothing, if `pilot` no longer holds it."""
        ...

    def queue_status(self, domain: str, pool: UUID, items: Sequence[int]) -> list[QueueEntry]:
        """The entries of `items`, in order; leases past their TTL become `lost` first. An id
        not in the pool raises MetadataError."""
        ...

    def queue_cancel(self, domain: str, pool: UUID, items: Sequence[int]) -> None:
        """Cancel the items that are not final yet; final ones keep their state."""
        ...


class QueuePut(BaseModel):
    """Body of `queue_put`: at most `BATCH_MAX` items (the client splits larger puts)."""

    model_config = _WIRE

    items: Annotated[list[QueueItem], Field(min_length=1, max_length=BATCH_MAX)]


class QueueIds(BaseModel):
    """Body of `queue_status` and `queue_cancel`, and the response of `queue_put`."""

    model_config = _WIRE

    ids: Annotated[list[StrictInt], Field(max_length=BATCH_MAX)]


class QueueLeaseRequest(BaseModel):
    """Body of `queue_lease`."""

    model_config = _WIRE

    slot: PilotSlot
    ttl_s: Annotated[StrictInt, Field(ge=1, le=MAX_LEASE_TTL_S)]


class OptionalQueueLease(BaseModel):
    model_config = _WIRE

    lease: QueueLease | None


class QueueRenew(BaseModel):
    """Body of `queue_renew`."""

    model_config = _WIRE

    pilot: _Name
    ttl_s: Annotated[StrictInt, Field(ge=1, le=MAX_LEASE_TTL_S)]


class QueueFinish(BaseModel):
    """Body of `queue_finish`."""

    model_config = _WIRE

    pilot: _Name
    exit_code: _ExitCode
    infra_reason: InfraReason | None = None


class QueueEntries(BaseModel):
    """Response of `queue_status`."""

    model_config = _WIRE

    entries: list[QueueEntry]


class MetadataBackend(MetadataStore, ReleaseStore, ProvenanceStore, PilotQueueStore, Protocol):
    """What every store backend implements (PostgreSQL, in-memory, HTTP): `open_metadata_store`
    returns one."""


class YankRequest(BaseModel):
    """Body of `POST /releases/{id}/yank`; the author is the authenticated caller."""

    model_config = _WIRE

    reason: Annotated[StrictStr, Field(min_length=1, max_length=4096)]


class ChannelSet(BaseModel):
    """Body of `PUT /channels`: the service checks the channel's gate itself."""

    model_config = _WIRE

    domain: _Domain
    name: _ReleaseName
    channel: _Channel
    release: StrictInt
    gate_build: StrictInt | None = None  # the gate build to check; default: any


class ExportRequest(BaseModel):
    model_config = _WIRE

    to_domain: _Domain


class GateQuery(BaseModel):
    """Body of `POST /domains/{domain}/gate-builds`."""

    model_config = _WIRE

    flow: _Name
    ids: Annotated[list[DigestField], Field(max_length=BATCH_MAX)]


class ReleaseImportBatch(BaseModel):
    """Body of `POST /builds/{id}/release-imports`."""

    model_config = _WIRE

    imports: Annotated[list[ReleaseImport], Field(max_length=BATCH_MAX)]


class ReleaseImportList(BaseModel):
    model_config = _WIRE

    imports: list[ReleaseImport]
    next: StrictStr | None = None  # the cursor of the next page (P2-05 R3)


class ReleaseList(BaseModel):
    model_config = _WIRE

    releases: list[ReleaseView]
    next: StrictStr | None = None  # the cursor of the next page (P2-05 R3)


class ChannelList(BaseModel):
    """Response of the channel reads; `GET /channels` holds zero or one channel."""

    model_config = _WIRE

    channels: list[ChannelView]


class ChannelHistory(BaseModel):
    model_config = _WIRE

    moves: list[ChannelMove]
    next: StrictStr | None = None  # the cursor of the next page (P2-05 R3)


class BuildList(BaseModel):
    model_config = _WIRE

    builds: list[StrictInt]


class ExportList(BaseModel):
    model_config = _WIRE

    exports: list[ExportRecord]
    next: StrictStr | None = None  # the cursor of the next page (P2-05 R3)


class ArchiveRequest(BaseModel):
    """Body of `POST /releases/{id}/archives`; the author is the authenticated caller."""

    model_config = _WIRE

    bundle: DigestField
    location: _Location


class ArchiveList(BaseModel):
    model_config = _WIRE

    archives: list[ArchiveRecord]
    next: StrictStr | None = None  # the cursor of the next page (P2-05 R3)


class ProvenanceQuery(BaseModel):
    """Body of `POST /domains/{domain}/provenance/{upstream,downstream}`."""

    model_config = _WIRE

    ids: Annotated[list[DigestField], Field(max_length=PROVENANCE_START_MAX)] = []
    keys: Annotated[list[DigestField], Field(max_length=PROVENANCE_START_MAX)] = []
    depth: Annotated[StrictInt, Field(ge=1, le=PROVENANCE_DEPTH_MAX)] = PROVENANCE_DEPTH_MAX


# --- event stream (P2-05) ------------------------------------------------------------------------

EVENT_PAGE_MAX: Final = 1000
"""The most events one `events_after` call returns."""


class StoredEvent(BaseModel):
    """An event with its id in the `events` table. Ids grow in commit order within a domain
    (the stores serialize the inserts of a domain), so `events_after(id)` resumes a stream
    without gaps: the SSE `id:` field (`Last-Event-ID`)."""

    model_config = _FROZEN

    id: StrictInt
    event: Event


class EventReader(Protocol):
    """What the service's event stream reads (P2-05 R2, R6). Only the PostgreSQL and in-memory
    stores implement it: the stream is served by the metadata service, never read over HTTP as
    a store."""

    def events_after(
        self,
        after: int,
        *,
        build: BuildId | None = None,
        domain: str | None = None,
        limit: int = EVENT_PAGE_MAX,
    ) -> list[StoredEvent]:
        """The events with an id above `after` of `build`, or of every build of `domain`
        (exactly one of the two), oldest first, at most `limit` (1..EVENT_PAGE_MAX)."""
        ...

    def prune_events(self, before: datetime, *, dry_run: bool = False) -> list[str]:
        """Drop every month of events stored (by the store's clock, not the events' `ts`) that
        ends at or before `before` (UTC months, the partitions of the `events` table); return
        them as `YYYY-MM`, oldest first."""
        ...


# --- dashboard reads (P2-06) ---------------------------------------------------------------------

LOG_CHUNK_MAX: Final = 4 << 20
"""Most log bytes one `GET /builds/{b}/actions/{a}/log` answers (and its default `limit`)."""

STATS_WINDOW_MAX_DAYS: Final = 366
"""The longest `since..until` window `domain_stats` aggregates over."""


class BuildFilter(BaseModel):
    """Which builds of a domain `list_builds` answers; every field set must match. `since` and
    `until` bound `created_at` (`since <= created_at < until`)."""

    model_config = _FROZEN

    user: _Name | None = None
    status: BuildStatus | None = None
    project: _Name | None = None
    since: AwareDatetime | None = None
    until: AwareDatetime | None = None


class StepStats(BaseModel):
    """One step's actions in a stats window: those served from the cache and those that ran.
    `cached_cpu_s` sums the CPU time recorded in the cached results (what the cache saved)."""

    model_config = _FROZEN

    step: _Name
    cached: _NonNegative
    ran: _NonNegative  # executed to a result: done or failed
    cached_cpu_s: _NonNegative
    cached_wall_s: _NonNegative
    ran_cpu_s: _NonNegative


class PendingWait(BaseModel):
    """Time actions spent pending for one reason; `feature` splits `licenses` waits by license
    feature (the `licenses` of the `pending` event), "" when the event named none."""

    model_config = _FROZEN

    reason: PendingReason
    feature: StrictStr | None
    waits: _NonNegative
    wait_s: _NonNegative


class DomainStats(BaseModel):
    """What the dashboard's metrics pages show for the builds created in `since..until`."""

    model_config = _FROZEN

    domain: _Domain
    since: AwareDatetime
    until: AwareDatetime
    builds: _NonNegative
    steps: list[StepStats]
    waits: list[PendingWait]


class ResultSummary(BaseModel):
    """What the regression grid shows of an action's ResultManifest (`summary` holds e.g. the
    test, seed and failure `signature` of a `questa.sim` result)."""

    model_config = _FROZEN

    action_id: ActionId
    status: Literal["passed", "failed"]
    exit_code: StrictInt
    log: DigestField | None
    summary: dict[_Name, StrictStr | StrictInt]


class DashboardReader(Protocol):
    """What the dashboard reads beyond the store protocol (P2-06). Only the PostgreSQL and
    in-memory stores implement it: it is served by the metadata service, like `EventReader`."""

    def list_builds(self, domain: str, flt: BuildFilter) -> list[BuildView]:
        """The builds of `domain` matching `flt`, newest (highest id) first."""
        ...

    def result_summaries(self, build: BuildId, *, step: str | None = None) -> list[ResultSummary]:
        """The recorded results of `build`'s actions (of `step` only, if given), by action id,
        without their inputs and outputs: the regression grid of one build."""
        ...

    def domain_stats(self, domain: str, *, since: datetime, until: datetime) -> DomainStats:
        """Cache use and pending waits of the builds of `domain` created in `since..until`;
        a still pending wait counts up to `until` or the store's clock, whichever is earlier."""
        ...


class ResultSummaryList(BaseModel):
    """Response of `GET /builds/{build}/results`."""

    model_config = _WIRE

    results: list[ResultSummary]
    next: StrictStr | None = None  # the cursor of the next page (P2-05 R3)


class BuildViewList(BaseModel):
    """Response of `GET /domains/{domain}/builds`."""

    model_config = _WIRE

    builds: list[BuildView]
    next: StrictStr | None = None  # the cursor of the next page (P2-05 R3)
