"""Logic shared by the metadata store implementations: validation on write and state changes."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from ebs.core.canon import canonical_json
from ebs.core.digest import Digest
from ebs.core.errors import CanonError, DigestError, MetadataError
from ebs.meta.api import (
    CHANNEL_PATTERN,
    EVENT_PAGE_MAX,
    FINAL_BUILD_STATUSES,
    MAX_LEASE_TTL_S,
    TOUCH_INTERVAL_S,
    ActionRow,
    ActionUpdate,
    BlobRef,
    BuildView,
    GcRun,
    ReleaseCreate,
    ReleaseView,
    ResultManifest,
    ToolchainCreate,
    check_domain,
    check_transition,
    release_output_name,
)

_M = TypeVar("_M", bound=BaseModel)


def revalidate(model: _M, cls: type[_M], what: str) -> _M:
    """Validate `model` again, even one built with `model_construct`, and check that its JSON
    form is canonical (R8). Raises MetadataError naming `what`."""
    if not isinstance(model, cls):
        raise MetadataError(f"{what}: expected {cls.__name__}, got {type(model).__name__}")
    try:
        checked = cls.model_validate({n: getattr(model, n) for n in cls.model_fields})
        canonical_json(checked.model_dump(mode="json"))
    except (ValidationError, CanonError, AttributeError) as exc:
        raise MetadataError(f"{what} is invalid and was not written: {exc}") from None
    return checked


def check_digest(value: object, what: str) -> Digest:
    """`value` if it is a Digest, else MetadataError (stores never take digest strings)."""
    if not isinstance(value, Digest):
        raise MetadataError(f"{what}: expected a Digest, got {type(value).__name__} {value!r}")
    return value


def check_result(key: Digest, result: ResultManifest, what: str) -> ResultManifest:
    """The re-validated manifest, which must describe the action `key`."""
    checked = revalidate(result, ResultManifest, what)
    if checked.action_key != key:
        raise MetadataError(
            f"{what}: the manifest's action key {checked.action_key} does not match the "
            f"action key {key}"
        )
    return checked


def check_final_status(status: object) -> None:
    if status not in FINAL_BUILD_STATUSES:
        raise MetadataError(
            f"finish_build: {status!r} is not a final build status; expected one of "
            f"{sorted(FINAL_BUILD_STATUSES)} (a build is 'running' until it is finished)"
        )


def touch_cutoff(now: datetime) -> datetime:
    """`last_access` at or before this instant is stale enough to be rewritten (R3)."""
    return now - timedelta(seconds=TOUCH_INTERVAL_S)


def parse_update(action_id: str, state: str, fields: Mapping[str, object]) -> ActionUpdate:
    """Validate `set_action_state` keyword fields against the target state."""
    try:
        update = ActionUpdate.model_validate(dict(fields))
    except ValidationError as exc:
        raise MetadataError(
            f"action {action_id!r}: invalid fields for state {state}: {exc}"
        ) from None
    given = update.model_fields_set
    if "pending_reason" in given and state != "pending":
        raise MetadataError(
            f"action {action_id!r}: pending_reason is only valid with state pending"
        )
    if "infra_reason" in given and state != "infra_failed":
        raise MetadataError(
            f"action {action_id!r}: infra_reason is only valid with state infra_failed"
        )
    return update


def transition(
    row: ActionRow, state: str, fields: Mapping[str, object], now: datetime
) -> ActionRow:
    """The action row after moving it to `state` with `fields`, or MetadataError (R7).

    Entering `running` counts an attempt; a final state stamps `finished_at`; `queued` (a retry)
    restamps `queued_at` and clears the previous attempt's job, reasons and times.
    """
    check_transition(row.action_id, row.state, state)
    update = parse_update(row.action_id, state, fields)
    changes: dict[str, object] = {"state": state}
    if state == "queued":
        changes |= {
            "queued_at": now,
            "started_at": None,
            "finished_at": None,
            "slurm_job_id": None,
            "pending_reason": None,
            "infra_reason": None,
        }
    elif state == "running":
        changes |= {"started_at": now, "attempts": row.attempts + 1, "pending_reason": None}
    elif state != "pending":
        changes["finished_at"] = now
    if state == "cached":
        changes["cached"] = True
    if "key" in update.model_fields_set and row.key is not None and update.key != row.key:
        raise MetadataError(
            f"action {row.action_id!r}: its action key is already {row.key}; refusing to change "
            f"it to {update.key}"
        )
    changes |= {name: getattr(update, name) for name in update.model_fields_set}
    return row.model_copy(update=changes)


def edge_rows(result: ResultManifest) -> list[tuple[str, str, str, str | None]]:
    """Provenance edges of a result as (direction, logical_path, object_id, content_digest):
    `in` for each input id, `out` for each output with its passed-down id and content digest."""
    rows: list[tuple[str, str, str, str | None]] = [
        ("in", path, str(ident), None) for path, ident in sorted(result.inputs.items())
    ]
    rows += [
        ("out", name, str(out.id), str(out.digest)) for name, out in sorted(result.outputs.items())
    ]
    return rows


def stored_result(payload: object, build: int, action_id: str) -> ResultManifest:
    """A manifest stored on an action row, validated again on read like any database row."""
    try:
        return ResultManifest.model_validate(payload)
    except ValueError as exc:
        raise MetadataError(
            f"build {build} action {action_id!r} holds an invalid result manifest; rerun the "
            f"action: {exc}"
        ) from None


def check_toolchain(spec: ToolchainCreate, manifest: object) -> ToolchainCreate:
    """The re-validated toolchain; `manifest` must be bytes (P1-07).

    The store does not recompute the toolchain id (the runner imports the store, and must not
    import `ebs.toolchain`); `ebs.toolchain.registry` computes it on write and checks it on read.
    """
    checked = revalidate(spec, ToolchainCreate, f"register_toolchain {spec.module!r}")
    if not isinstance(manifest, bytes):
        raise MetadataError(
            f"register_toolchain {checked.module!r}: the manifest must be bytes, "
            f"got {type(manifest).__name__}"
        )
    return checked


# --- garbage collection, leases and pins (P1-09) -------------------------------------------------


def check_blobs(blobs: Iterable[BlobRef]) -> list[BlobRef]:
    """Re-validated blob references for `register_blobs`."""
    return [revalidate(b, BlobRef, "register_blobs entry") for b in blobs]


def check_lease_ttl(ttl_s: object) -> int:
    if type(ttl_s) is not int or not 1 <= ttl_s <= MAX_LEASE_TTL_S:
        raise MetadataError(
            f"lease ttl_s must be a whole number of seconds from 1 to {MAX_LEASE_TTL_S}, got "
            f"{ttl_s!r}; renew a lease on a heartbeat instead of asking for a long one"
        )
    return ttl_s


def check_pin(by: object, reason: object) -> tuple[str, str]:
    if not isinstance(by, str) or not by or len(by) > 256:
        raise MetadataError(f"pin_build: 'by' must name who pins the build, got {by!r}")
    if not isinstance(reason, str) or len(reason) > 4096:
        raise MetadataError("pin_build: the reason must be text of at most 4096 characters")
    return by, reason


def check_run(run: GcRun) -> GcRun:
    """A GC run record whose report is canonical JSON."""
    try:
        canonical_json(run.report)  # type: ignore[arg-type]  # validated here
    except CanonError as exc:
        raise MetadataError(f"gc_record_run: the report is not canonical JSON: {exc}") from None
    if min(run.marked, run.swept_rows, run.swept_bytes) < 0:
        raise MetadataError("gc_record_run: counts must not be negative")
    return run


def _manifest_refs(payload: object) -> tuple[list[str], str | None]:
    if not isinstance(payload, dict):
        return [], None
    outputs = payload.get("outputs")
    found = []
    if isinstance(outputs, dict):
        for out in outputs.values():
            if isinstance(out, dict) and isinstance(out.get("digest"), str):
                found.append(out["digest"])
    log = payload.get("log")
    return found, log if isinstance(log, str) else None


def cache_refs(payload: object) -> tuple[tuple[Digest, ...], Digest | None]:
    """(output content digests, log digest) of a stored result manifest (JSON form): what a
    cache entry needs to exist. Unparseable digests are skipped (the row is invalid anyway)."""
    outputs, log = _manifest_refs(payload)
    parsed = sorted({d for d in (_parse(o) for o in outputs) if d is not None}, key=str)
    return tuple(parsed), None if log is None else _parse(log)


def attribution_digests(payload: object) -> list[Digest]:
    """Every object a stored result manifest references: its outputs and its log."""
    outputs, log = cache_refs(payload)
    return [*outputs, *([log] if log is not None else [])]


def _parse(value: str) -> Digest | None:
    try:
        return Digest.parse(value)
    except DigestError:
        return None


# --- releases (P2-02) ----------------------------------------------------------------------------

PASSED_STATES: frozenset[str] = frozenset({"done", "cached"})
"""Action states whose recorded result may be released without `allow_failed`."""


def check_by(by: object, what: str) -> str:
    if not isinstance(by, str) or not by or len(by) > 256:
        raise MetadataError(f"{what}: the author must be a non-empty name, got {by!r}")
    return by


def check_release(
    spec: ReleaseCreate,
    build: BuildView,
    rows: Mapping[str, ActionRow],
    results: Mapping[str, ResultManifest],
) -> None:
    """Raise MetadataError unless `spec` may be created from `build` (P2-02 R1): the build is
    in the release's domain and finished, and every output is exactly what its action recorded,
    from an action that passed (or `allow_failed` says why not)."""
    what = f"release {spec.name} {spec.version}"
    if build.domain != spec.domain:
        raise MetadataError(
            f"{what}: build {build.id} is in domain {build.domain!r}, not {spec.domain!r}; a "
            "release is created in its build's domain (`ebs release export` copies it to another)"
        )
    if build.status == "running":
        raise MetadataError(
            f"{what}: build {build.id} is still running; release it once it has finished"
        )
    failed: list[str] = []
    for name, out in sorted(spec.outputs.items()):
        if name != release_output_name(out.action_id, out.output):
            raise MetadataError(
                f"{what}: output {name!r} must be named "
                f"{release_output_name(out.action_id, out.output)!r}"
            )
        row, result = rows.get(out.action_id), results.get(out.action_id)
        if row is None:
            raise MetadataError(f"{what}: build {build.id} has no action {out.action_id!r}")
        recorded = None if result is None else result.outputs.get(out.output)
        if recorded is None:
            raise MetadataError(
                f"{what}: action {out.action_id!r} of build {build.id} recorded no output "
                f"{out.output!r}"
            )
        if (recorded.digest, recorded.id, recorded.type, recorded.size) != (
            out.digest, out.id, out.type, out.size,
        ):  # fmt: skip
            raise MetadataError(
                f"{what}: output {name!r} does not match what build {build.id} recorded "
                f"({recorded.type} {recorded.digest}, id {recorded.id})"
            )
        assert result is not None
        if row.state not in PASSED_STATES or result.status != "passed":
            failed.append(f"{out.action_id} ({row.state}, {result.status})")
    if failed and spec.allow_failed is None:
        raise MetadataError(
            f"{what}: these actions did not pass: {', '.join(failed)}; release only passed "
            "outputs, or give --allow-failed REASON (the reason is stored with the release)"
        )


def check_set_channel(release: ReleaseView | None, domain: str, name: str, channel: str) -> None:
    check_domain(domain)
    where = f"channel {name}:{channel} in domain {domain!r}"
    if not isinstance(channel, str) or re.fullmatch(CHANNEL_PATTERN, channel) is None:
        raise MetadataError(
            f"{where}: invalid channel name; use lower-case letters, digits, '_' or '-', "
            "starting with a letter"
        )
    if release is None:
        raise MetadataError(f"{where}: no such release")
    if (release.domain, release.name) != (domain, name):
        raise MetadataError(
            f"{where}: release {release.id} is {release.name} {release.version} in domain "
            f"{release.domain!r}; a channel points at releases of its own name and domain"
        )
    if release.yanked_at is not None:
        raise MetadataError(
            f"{where}: release {release.name} {release.version} is yanked "
            f"({release.yank_reason}); point the channel at another release"
        )


def check_exportable(release: ReleaseView) -> None:
    if release.yanked_at is not None:
        raise MetadataError(
            f"release {release.name} {release.version} is yanked ({release.yank_reason}); a "
            "yanked release is not exported"
        )


def check_event_query(build: object, domain: object, limit: object) -> None:
    """The arguments of `EventReader.events_after`: exactly one of build and domain."""
    if (build is None) == (domain is None):
        raise MetadataError("events_after: give exactly one of build and domain")
    if domain is not None:
        check_domain(domain)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= EVENT_PAGE_MAX:
        raise MetadataError(f"events_after: limit must be 1..{EVENT_PAGE_MAX}, not {limit!r}")


def month_start(ts: datetime) -> datetime:
    """The first instant of the UTC month of `ts` (the partition of an event, P2-05 R6)."""
    utc = ts.astimezone(UTC)
    return utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def next_month(start: datetime) -> datetime:
    return (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )


def month_name(start: datetime) -> str:
    return f"{start.year:04}-{start.month:02}"
