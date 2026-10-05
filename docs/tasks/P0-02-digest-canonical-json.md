# P0-02 — Digests and canonical JSON

Status: review · Phase: 0 · Depends on: P0-01 · Parallel-safe with: P0-04, P0-11 · Size: S

## Goal
The hashing foundation every cache decision rests on: a `Digest` type, streaming file hashing and a
canonical JSON encoder whose output is byte-stable forever.

## Read first
- docs/architecture.md § "Hashing, action keys and caching" (first two paragraphs)
- docs/design/interfaces.md §1
- docs/design/invariants.md I1, I2

## Scope (files)
- create `src/ebs/core/digest.py`, `src/ebs/core/canon.py`, `src/ebs/core/types.py` (`JsonValue` alias)
- create `tests/unit/core/test_digest.py`, `tests/unit/core/test_canon.py`,
  `tests/fixtures/canon/*.json` (vectors)

## Out of scope
Tree manifests (P0-03), action keys (P0-08).

## Contract
Exactly interfaces.md §1.

## Requirements
- R1 `Digest.parse(str(d)) == d` for all valid digests; parse rejects uppercase hex, wrong length,
  unknown algorithm, missing prefix — with a `DigestError` that quotes the bad value (truncated to 80 chars).
- R2 `hash_file` streams in chunks (never reads the whole file), returns `(digest, size)`, and equals
  `hash_bytes(path.read_bytes())`; works on 0-byte files and files > 2 GiB (test with a sparse file).
- R3 `blake3` works when the optional package is installed and raises `DigestError("blake3 not installed…")` otherwise.
- R4 `canonical_json` follows RFC 8785 for the supported subset: key ordering by UTF-16 code units,
  JCS string escaping, integers only. Passes the vectors in `tests/fixtures/canon/` (include the RFC 8785
  examples that use only ints/strings/bools/null, plus non-BMP characters for key ordering).
- R5 Rejects floats, NaN, non-str keys, non-NFC strings and any object other than
  dict/list/tuple/str/int/bool/None, with a `CanonError` naming the JSON path (`$.params.seed`).
  Tuples are accepted and encoded exactly like lists.
- R6 I1 and I2 hold (Hypothesis over recursive JSON strategies).
- R7 `StreamingHasher` equals `hash_bytes` for any split of the input (Hypothesis).
- R8 `Digest.shard()` returns `(hex[0:2], hex[2:4], hex)`.

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `test_digest.py::test_roundtrip`, `::test_parse_rejects[...]` | R1 | unit/property |
| `test_digest.py::test_hash_file_streams` (patch `open` to assert chunked reads), `::test_empty`, `::test_sparse_3gib` (mark `slow`) | R2 | unit |
| `test_digest.py::test_blake3_optional` | R3 | unit |
| `test_canon.py::test_vectors[...]` | R4 | unit |
| `test_canon.py::test_rejects[...]` | R5 | unit |
| `test_canon.py::test_order_independence`, `::test_injective` | R6 | property |
| `test_digest.py::test_streaming_split_invariance` | R7 | property |
| `test_digest.py::test_shard` | R8 | unit |

## Done when
- [x] `make check` passes; coverage of `ebs.core` ≥ 95 % (100 % line and branch)
- [x] invariants I1/I2 point at the real test names

## Notes
Do not use `json.dumps(sort_keys=True)` as the implementation: its key order is by code point, which
differs from UTF-16 order for non-BMP characters, and its escaping differs from JCS.

Implementation notes (P0-02):
- RFC 8785 §3.2.3's sorting example contains U+FB33, which is not NFC (it decomposes to
  U+05D3 U+05BC), so R5 rejects it. `tests/fixtures/canon/rfc8785_sort_utf16.json` substitutes
  U+FF21, which still separates UTF-16 order from code-point order, and
  `test_rfc_sort_example_is_rejected_as_non_nfc` pins the rejection of the verbatim input.
- Integers are limited to ±(2**53 − 1) (the JCS exact range), so output is byte-identical to any
  RFC 8785 implementation. Relaxing this later changes no existing key; tightening it would.
  Documented in interfaces.md §1. Confirmed by the human on 2026-09-28.
- Int subclasses (IntEnum) are accepted and encoded as plain digits; str subclasses as plain strings.
- Nesting beyond the Python recursion limit raises `CanonError`, not `RecursionError`.
- `JsonValue` uses invariant `dict`/`list`, so callers holding e.g. `dict[str, str]` must annotate
  it as `dict[str, JsonValue]` (P0-08 will meet this). Chosen over Mapping/Sequence because those
  would type-check values the encoder rejects at runtime.
- Follow-up: nightly mutmut on canon.py — the redundant `not sep` check in `Digest.parse` was
  removed so it can't produce an equivalent mutant.
