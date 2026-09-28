from __future__ import annotations

import json
import random
import sys
import unicodedata
from enum import IntEnum
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ebs.core.canon import MAX_SAFE_INTEGER, canonical_json, digest_json
from ebs.core.digest import hash_bytes
from ebs.core.errors import CanonError
from ebs.core.types import JsonValue

VECTORS = Path(__file__).resolve().parents[2] / "fixtures" / "canon"
VECTOR_FILES = sorted(VECTORS.glob("*.json"))


# R4
def test_vector_directory_is_populated() -> None:
    assert len(VECTOR_FILES) >= 8


# R4
@pytest.mark.parametrize("path", VECTOR_FILES, ids=lambda p: p.stem)
def test_vectors(path: Path) -> None:
    vector = json.loads(path.read_text(encoding="utf-8"))
    assert canonical_json(vector["input"]) == vector["expected"].encode("utf-8")


# R4
def test_rfc_sort_example_is_rejected_as_non_nfc() -> None:
    # The verbatim RFC 8785 §3.2.3 input contains U+FB33, whose NFC form is U+05D3 U+05BC,
    # so it is outside the canonicalizable domain (hence U+FF21 in the fixture instead).
    with pytest.raises(CanonError, match="NFC"):
        canonical_json({"\u20ac": "Euro Sign", "\ufb33": "Hebrew Letter Dalet With Dagesh"})


# R4
def test_output_is_compact_utf8() -> None:
    assert canonical_json({"a": [1, "\u00e9"], "b": None}) == '{"a":[1,"\u00e9"],"b":null}'.encode()


# R5
def test_tuples_encode_like_lists() -> None:
    assert canonical_json((1, ("a", True), [])) == canonical_json([1, ["a", True], []])


# R5
def test_bool_is_not_confused_with_int() -> None:
    assert canonical_json([True, False, 1, 0]) == b"[true,false,1,0]"


# R5
def test_accepts_the_safe_integer_bounds() -> None:
    assert canonical_json([MAX_SAFE_INTEGER, -MAX_SAFE_INTEGER]) == (
        b"[9007199254740991,-9007199254740991]"
    )


class _Opaque:
    pass


# R5
@pytest.mark.parametrize(
    ("value", "path", "reason"),
    [
        pytest.param({"params": {"seed": 1.5}}, "$.params.seed", "float not allowed", id="float"),
        pytest.param({"x": [0, float("nan")]}, "$.x[1]", "float not allowed", id="nan"),
        pytest.param([float("inf")], "$[0]", "float not allowed", id="inf"),
        pytest.param({"n": 2**53}, "$.n", "range", id="int-too-big"),
        pytest.param({"n": -(2**53)}, "$.n", "range", id="int-too-small"),
        pytest.param({"params": {1: "a"}}, "$.params", "key", id="int-key"),
        pytest.param({"a": {None: 1}}, "$.a", "key", id="none-key"),
        pytest.param({"a": {("t",): 1}}, "$.a", "key", id="tuple-key"),
        pytest.param({"s": "e\u0301"}, "$.s", "string is not NFC", id="non-nfc-value"),
        pytest.param({"e\u0301": 1}, "$", "key is not NFC", id="non-nfc-key"),
        pytest.param(["\ud800"], "$[0]", "surrogate", id="lone-surrogate"),
        pytest.param({"a": {"k\udc00": 1, "b": 2}}, "$.a", "surrogate", id="lone-surrogate-key"),
        pytest.param({"a": 1.5, "b\udc00": 1}, "$", "surrogate", id="key-checked-before-values"),
        pytest.param({"seed2": 1.5}, "$.seed2", "float", id="identifier-path-with-digit"),
        pytest.param({"1a": 1.5}, '$["1a"]', "float", id="quoted-path-leading-digit"),
        pytest.param({"s": {1, 2}}, "$.s", "set", id="set"),
        pytest.param({"b": b"x"}, "$.b", "bytes", id="bytes"),
        pytest.param({"a b": [_Opaque()]}, '$["a b"][0]', "_Opaque", id="object"),
        pytest.param({"a": {"b": [[{"c": 1.0}]]}}, "$.a.b[0][0].c", "float not allowed", id="deep"),
    ],
)
def test_rejects(value: object, path: str, reason: str) -> None:
    with pytest.raises(CanonError) as excinfo:
        canonical_json(value)  # type: ignore[arg-type]
    message = str(excinfo.value)
    assert f"at {path}" in message
    assert reason in message


# R5
def test_int_subclasses_encode_as_plain_digits() -> None:
    class Level(IntEnum):
        HIGH = 7

    class Weird(int):
        def __str__(self) -> str:
            return "x"

        def __repr__(self) -> str:
            return "x"

    assert canonical_json([Level.HIGH, Weird(5)]) == b"[7,5]"


# R5
def test_rejects_excessive_nesting_with_canon_error() -> None:
    value: JsonValue = []
    for _ in range(sys.getrecursionlimit() + 10):
        value = [value]
    with pytest.raises(CanonError, match="nested too deeply"):
        canonical_json(value)


# R5
def test_rejection_message_keeps_values_up_to_the_limit() -> None:
    at_limit = "e\u0301" + "x" * 76  # repr() is exactly 80 characters
    with pytest.raises(CanonError) as excinfo:
        canonical_json([at_limit])
    assert f"{at_limit!r};" in str(excinfo.value)  # quoted in full, no ellipsis


# R5
def test_rejection_message_truncates_huge_values() -> None:
    with pytest.raises(CanonError) as excinfo:
        canonical_json({"s": {"x" * 1000}})  # type: ignore[dict-item]
    assert len(str(excinfo.value)) < 300


# --- properties (I1, I2) ------------------------------------------------------------------------

nfc_text = st.text(
    alphabet=st.characters(exclude_categories=["Cs"]),  # no lone surrogates
    max_size=12,
).map(lambda s: unicodedata.normalize("NFC", s))
json_scalars = (
    st.none()
    | st.booleans()
    | st.integers(min_value=-MAX_SAFE_INTEGER, max_value=MAX_SAFE_INTEGER)
    | nfc_text
)
json_values = st.recursive(
    json_scalars,
    lambda children: (
        st.lists(children, max_size=5)
        | st.lists(children, max_size=5).map(tuple)
        | st.dictionaries(nfc_text, children, max_size=5)
    ),
    max_leaves=40,
)


def _reinsert(value: JsonValue, rnd: random.Random) -> JsonValue:
    """An equal value whose dicts (at every level) were built in a shuffled insertion order."""
    if isinstance(value, dict):
        items = list(value.items())
        rnd.shuffle(items)
        return {k: _reinsert(v, rnd) for k, v in items}
    if isinstance(value, list | tuple):
        return [_reinsert(v, rnd) for v in value]
    return value


def _strict(value: object) -> object:
    """Type-tagged form, so that True != 1 and tuple == list, as in JSON."""
    if isinstance(value, dict):
        return ("object", {k: _strict(v) for k, v in value.items()})
    if isinstance(value, list | tuple):
        return ("array", [_strict(v) for v in value])
    return (type(value).__name__, value)


# R6 / I1
@given(value=json_values, rnd=st.randoms(use_true_random=False))
def test_order_independence(value: JsonValue, rnd: random.Random) -> None:
    assert canonical_json(_reinsert(value, rnd)) == canonical_json(value)
    assert digest_json(_reinsert(value, rnd)) == digest_json(value)


# R6 / I2: decoding is a left inverse of canonical_json, so different values give different bytes.
@given(value=json_values)
def test_injective(value: JsonValue) -> None:
    encoded = canonical_json(value)
    assert _strict(json.loads(encoded.decode("utf-8"))) == _strict(value)


# R6 / I2
@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("1", 1),
        (True, 1),
        (False, 0),
        (None, "null"),
        ([], {}),
        ([[]], [[], []]),
        ({"a": "b"}, {"b": "a"}),
        ({"a": 1, "b": 2}, {"a": 1}),
        (['a","b'], ["a", "b"]),
        (["a\\", "b"], ['a\\",\\"b']),
        ({"a": {"b": 1}}, {"a.b": 1}),
        ("\u00e9", "\u00c9"),
    ],
)
def test_near_collisions(a: JsonValue, b: JsonValue) -> None:
    assert canonical_json(a) != canonical_json(b)


# R6
def test_digest_json_honours_algo() -> None:
    pytest.importorskip("blake3", reason="missing dependency: install the `blake3` extra")
    digest = digest_json({"a": 1}, "blake3")
    assert digest.algo == "blake3"
    assert digest == hash_bytes(b'{"a":1}', "blake3")


# R6
@given(value=json_values)
def test_digest_json_hashes_canonical_bytes(value: JsonValue) -> None:
    assert digest_json(value) == hash_bytes(canonical_json(value))
