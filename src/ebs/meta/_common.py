"""Logic shared by the metadata store implementations: validation on write and state changes."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from ebs.core.canon import canonical_json
from ebs.core.digest import Digest
from ebs.core.errors import CanonError, MetadataError
from ebs.meta.api import (
    FINAL_BUILD_STATUSES,
    TOUCH_INTERVAL_S,
    ActionRow,
    ActionUpdate,
    ResultManifest,
    check_transition,
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
