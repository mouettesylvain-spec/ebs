"""Shared type aliases of the core layer (docs/design/interfaces.md § 1)."""

from __future__ import annotations

from typing import TypeAlias

JsonValue: TypeAlias = (
    "dict[str, JsonValue] | list[JsonValue] | tuple[JsonValue, ...] | str | int | bool | None"
)
"""A value `ebs.core.canon.canonical_json` accepts: JSON without floats; tuples encode as arrays."""
