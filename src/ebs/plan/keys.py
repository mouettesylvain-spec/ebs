"""Action keys (docs/design/interfaces.md § 4, invariants I3, I4, I14).

The action key is the SHA-256 of the canonical JSON *key document*: exactly the fields below,
nothing else. Resources, licenses, debug settings, the domain, the action id, the step name,
runtime env and input provenance never reach the document, so they can never change a key.

Changing what goes into the document, or how a field is encoded, changes keys: bump
`KEY_SCHEMA_VERSION` and regenerate the golden fixtures (scripts/regen_golden_keys.py).
"""

from __future__ import annotations

import dataclasses
from typing import Final

from ebs.core.canon import digest_json
from ebs.core.digest import Digest, hash_bytes
from ebs.core.errors import CanonError, PlanError
from ebs.core.types import JsonValue
from ebs.plan.types import ActionOutputInput, ActionSpec, ParamValue

__all__ = [
    "KEY_SCHEMA_VERSION",
    "action_key_document",
    "compute_key",
    "nondeterministic_output_id",
    "with_key",
]

KEY_SCHEMA_VERSION: Final = 1


def _param(value: ParamValue) -> JsonValue:
    return list(value) if isinstance(value, tuple) else value


def action_key_document(spec: ActionSpec) -> JsonValue:
    """The document hashed into the key; an input without an id is encoded as null."""
    return {
        "schema": KEY_SCHEMA_VERSION,
        "rule": {"kind": spec.rule.kind, "version": spec.rule.version},
        "argv": list(spec.argv),
        "params": {name: _param(value) for name, value in spec.params.items()},
        "env": dict(spec.env),
        "toolchain": None if spec.toolchain is None else str(spec.toolchain.id),
        "config_files": {
            path: str(hash_bytes(content.encode("utf-8")))
            for path, content in spec.config_files.items()
        },
        "inputs": {
            ref.logical_path: None if ref.id is None else str(ref.id) for ref in spec.inputs
        },
        "outputs": {out.name: {"path": out.path, "type": out.type} for out in spec.outputs},
    }


def compute_key(spec: ActionSpec) -> Digest:
    """The action key; PlanError while an input id is unknown or text is not NFC."""
    pending = [ref.logical_path for ref in spec.inputs if ref.id is None]
    if pending:
        producers = sorted(
            {
                ref.source.action_id
                for ref in spec.inputs
                if ref.id is None and isinstance(ref.source, ActionOutputInput)
            }
        )
        raise PlanError(
            f"action {spec.action_id!r}: inputs {', '.join(pending)} have no id yet, so the key "
            f"is unknown until their producers ran ({', '.join(producers) or 'none recorded'}); "
            "Planner.refine fills them in"
        )
    try:
        return digest_json(action_key_document(spec))
    except CanonError as exc:
        raise PlanError(
            f"action {spec.action_id!r} cannot be hashed: {exc}. Text in commands, params, env "
            "and paths must be Unicode NFC; re-save the flow or table in NFC form"
        ) from exc


def with_key(spec: ActionSpec) -> ActionSpec:
    """`spec` with its key computed, or None while any input id is unknown."""
    key = None if any(ref.id is None for ref in spec.inputs) else compute_key(spec)
    return spec if key == spec.key else dataclasses.replace(spec, key=key)


def nondeterministic_output_id(producer_key: Digest, output_name: str) -> Digest:
    """The id a `deterministic: false` output passes downstream: derived from the producer's key,
    so re-running the producer (new bytes) never changes consumer keys (I14).
    """
    return digest_json({"nd": 1, "producer": str(producer_key), "output": output_name})
