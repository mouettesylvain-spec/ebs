"""Canonical JSON: the RFC 8785 (JCS) subset every action key is hashed from.

The output must stay byte-stable forever (invariants I1, I2). Supported values are dicts with
str keys, lists and tuples (both encode as arrays), NFC strings, integers within the IEEE-754
safe range, booleans and None. Everything else raises `CanonError` naming the JSON path.

Deliberately not `json.dumps(sort_keys=True)`: that sorts keys by code point, while JCS sorts by
UTF-16 code units, which differ for characters outside the BMP.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

from ebs.core.digest import Algo, Digest, hash_bytes
from ebs.core.errors import CanonError
from ebs.core.types import JsonValue

MAX_SAFE_INTEGER: Final = 2**53 - 1
"""Larger magnitudes are not exactly representable as JCS numbers; pass them as strings."""

_IDENTIFIER_RE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_REPR_LIMIT: Final = 80
# JCS (RFC 8785 § 3.2.2.2): escape '"', '\' and C0 controls; the five with short forms use them.
_ESCAPES: Final[dict[int, str]] = {
    **{c: f"\\u{c:04x}" for c in range(0x20)},
    0x08: "\\b",
    0x09: "\\t",
    0x0A: "\\n",
    0x0C: "\\f",
    0x0D: "\\r",
    0x22: '\\"',
    0x5C: "\\\\",
}


def canonical_json(obj: JsonValue) -> bytes:
    """Encode `obj` as canonical JSON (UTF-8, no whitespace, keys in UTF-16 code-unit order)."""
    parts: list[str] = []
    try:
        _encode(obj, "$", parts)
    except RecursionError:
        raise CanonError(
            "value nested too deeply to canonicalize (Python recursion limit reached); "
            "flatten the structure"
        ) from None
    return "".join(parts).encode("utf-8")


def digest_json(obj: JsonValue, algo: Algo = "sha256") -> Digest:
    return hash_bytes(canonical_json(obj), algo)


def _encode(value: object, path: str, out: list[str]) -> None:
    # bool before int: bool is an int subclass.
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int):
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            raise CanonError(
                f"integer out of range at {path}: {_short_repr(value)} exceeds ±(2**53 - 1), "
                "the largest exactly representable JSON number; pass it as a string instead"
            )
        out.append(int.__repr__(value))
    elif isinstance(value, str):
        out.append(_string(value, path))
    elif isinstance(value, dict):
        _encode_object(value, path, out)
    elif isinstance(value, list | tuple):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _encode(item, f"{path}[{i}]", out)
        out.append("]")
    elif isinstance(value, float):
        raise CanonError(
            f"float not allowed at {path}: {_short_repr(value)}; canonical JSON takes integers "
            "only, so write the parameter as a string or an integer"
        )
    else:
        raise CanonError(
            f"unsupported type {type(value).__name__} at {path}: {_short_repr(value)}; expected "
            "dict, list, tuple, str, int, bool or None"
        )


def _encode_object(value: dict[object, object], path: str, out: list[str]) -> None:
    for key in value:
        if not isinstance(key, str):
            raise CanonError(
                f"non-string key at {path}: {_short_repr(key)} ({type(key).__name__}); "
                "JSON object keys must be strings"
            )
    keys: list[str] = sorted(value, key=lambda k: _utf16_units(k, path))  # type: ignore[arg-type]
    out.append("{")
    for i, key in enumerate(keys):
        if i:
            out.append(",")
        out.append(_string(key, path, is_key=True))
        out.append(":")
        _encode(value[key], _child_path(path, key), out)
    out.append("}")


def _utf16_units(key: str, path: str) -> bytes:
    # Big-endian UTF-16 bytes compare exactly like sequences of UTF-16 code units.
    try:
        return key.encode("utf-16-be")
    except UnicodeEncodeError:
        raise CanonError(_surrogate_message(key, path, is_key=True)) from None


def _string(value: str, path: str, *, is_key: bool = False) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise CanonError(_surrogate_message(value, path, is_key=is_key)) from None
    if not unicodedata.is_normalized("NFC", value):
        what = "key" if is_key else "string"
        raise CanonError(
            f"{what} is not NFC-normalized at {path}: {_short_repr(value)}; normalize it with "
            "unicodedata.normalize('NFC', …) where the value enters ebs"
        )
    return '"' + value.translate(_ESCAPES) + '"'


def _surrogate_message(value: str, path: str, *, is_key: bool) -> str:
    what = "key" if is_key else "string"
    return (
        f"{what} contains a lone surrogate at {path}: {_short_repr(value)}; "
        "it cannot be encoded as UTF-8"
    )


def _child_path(path: str, key: str) -> str:
    if _IDENTIFIER_RE.fullmatch(key):
        return f"{path}.{key}"
    return f"{path}[{_string(key, path, is_key=True)}]"


def _short_repr(value: object) -> str:
    text = repr(value)
    if len(text) <= _REPR_LIMIT:
        return text
    return text[:_REPR_LIMIT] + "…"
