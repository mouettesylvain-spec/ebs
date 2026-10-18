"""Cursor pagination of the list routes (P2-05 R3).

A list route takes `limit` (1..`[meta_service.limits].max_page_size`, which is also the
default) and `cursor`, and answers its items in a stable order with `next`, the cursor of the
following page, or null on the last page. A cursor is opaque to clients: the sort key of the
last item sent plus a fingerprint of the query, so a page resumes after that key even when rows
were added in between (no repeats, no gaps among existing rows), and a cursor used with another
query is refused (422).

The page is cut from the store's full answer: every listed table is bounded per build, release
or channel, so this bounds the responses, not the database work. Pushing the limit into the
store queries is a follow-up.
"""

from __future__ import annotations

import base64
import binascii
import bisect
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Annotated, TypeVar

from fastapi import Query
from fastapi.exceptions import RequestValidationError

__all__ = ["Cursor", "Key", "Limit", "paginate", "sort_ts"]

_T = TypeVar("_T")

Key = tuple[str | int, ...]

Limit = Annotated[
    int | None, Query(description="Items per page (default and maximum: the server's limit).")
]
Cursor = Annotated[str | None, Query(description="The `next` of the previous page.")]


def sort_ts(ts: datetime) -> str:
    """A timestamp as a fixed-width UTC string, which sorts like the instant."""
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _invalid(field: str, msg: str) -> RequestValidationError:
    return RequestValidationError([{"loc": ("query", field), "msg": msg}])


def _fingerprint(query: Mapping[str, object]) -> str:
    canon = json.dumps(dict(query), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


def _encode(key: Key, fingerprint: str) -> str:
    raw = json.dumps({"k": list(key), "q": fingerprint}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _decode(cursor: str, fingerprint: str) -> Key:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        doc = json.loads(raw)
        key, q = doc["k"], doc["q"]
    except (binascii.Error, ValueError, TypeError, KeyError):
        raise _invalid("cursor", "invalid cursor: pass the `next` of the previous page") from None
    if (
        not isinstance(key, list)
        or not key
        or not all(isinstance(k, str | int) and not isinstance(k, bool) for k in key)
    ):
        raise _invalid("cursor", "invalid cursor: pass the `next` of the previous page")
    if q != fingerprint:
        raise _invalid("cursor", "the cursor is of another query; start again without it")
    return tuple(key)


def paginate(
    items: Sequence[_T],
    key: Callable[[_T], Key],
    *,
    cursor: str | None,
    limit: int | None,
    max_page_size: int,
    query: Mapping[str, object],
) -> tuple[list[_T], str | None]:
    """One page of `items` in `key` order (keys are unique), and the cursor of the next."""
    size = max_page_size if limit is None else limit
    if not 1 <= size <= max_page_size:
        raise _invalid("limit", f"limit must be 1 to at most {max_page_size}, not {limit}")
    fingerprint = _fingerprint(query)
    ordered = sorted(items, key=key)
    start = 0
    if cursor is not None:
        after = _decode(cursor, fingerprint)
        try:
            start = bisect.bisect_right([key(i) for i in ordered], after)
        except TypeError:  # a key of other types than this list's
            raise _invalid("cursor", "the cursor is of another query; start again") from None
    page = list(ordered[start : start + size])
    more = start + size < len(ordered)
    return page, (_encode(key(page[-1]), fingerprint) if more and page else None)
