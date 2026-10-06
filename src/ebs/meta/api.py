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
    "DIGEST_SQL_PATTERN",
    "DOMAIN_PATTERN",
    "EVENT_TYPE_PATTERN",
    "FINAL_BUILD_STATUSES",
    "TERMINAL_STATES",
    "TOUCH_INTERVAL_S",
    "TRANSITIONS",
    "ActionId",
    "ActionRow",
    "ActionState",
    "ActionUpdate",
    "BuildCreate",
    "BuildId",
    "BuildStatus",
    "BuildView",
    "CacheMode",
    "DigestField",
    "Event",
    "EventType",
    "InfraReason",
    "MetadataStore",
    "OutputResult",
    "PendingReason",
    "ResourceUsage",
    "ResultManifest",
    "RunnerInfo",
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


class MetadataStore(Protocol):
    """Shared build state. Every method is one short transaction; errors raise MetadataError.

    The lease methods of interfaces.md § 6 (`acquire_lease`, `renew_lease`) arrive with GC (P1-09).
    """

    # action cache
    def cache_get(self, domain: str, key: Digest) -> ResultManifest | None:
        """The cached result of `key` in `domain`, or None. Counts a hit and touches
        `last_access` at most once per hour."""
        ...

    def cache_put(self, domain: str, key: Digest, result: ResultManifest) -> bool:
        """Insert if absent; True if this call inserted, False if the key was already cached."""
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

    # events
    def emit(self, event: Event) -> None: ...
