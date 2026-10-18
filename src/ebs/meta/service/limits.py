"""Request limits of the metadata service (P2-05 R3, R4).

- Rate: a token bucket per principal (`rate_per_s` tokens per second, at most `burst`), checked
  right after authentication; an empty bucket answers 429 with `Retry-After`. The HTTP store
  waits and retries, so a burst of runner jobs of one user is smoothed, not failed.
- Bodies: larger than `max_body_bytes` answers 413, from `Content-Length` before anything is
  read, or while a chunked body streams in.
- Pages: list routes return at most `max_page_size` items per page (`ebs.meta.service.pagination`).

Buckets live in the process: with several uvicorn workers each enforces the limit on its own
share of the requests, so the effective rate is up to `workers` times `rate_per_s`.
"""

from __future__ import annotations

import json
import math
import threading
from collections import OrderedDict
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ebs.core.clock import Clock, SystemClock
from ebs.meta.api import ErrorBody

__all__ = [
    "BodyLimit",
    "LimitsConfig",
    "RateLimitedError",
    "RateLimiter",
    "TokenBucket",
    "retry_after",
]


class LimitsConfig(BaseModel):
    """`[meta_service.limits]` of the service's ebs.toml."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    rate_per_s: Annotated[float, Field(gt=0)] = 100.0
    burst: Annotated[int, Field(ge=1)] = 2000
    max_body_bytes: Annotated[int, Field(ge=1024)] = 16 << 20
    max_page_size: Annotated[int, Field(ge=1, le=100_000)] = 1000
    max_principals: Annotated[int, Field(ge=1)] = 10_000  # buckets kept (least recent dropped)


class RateLimitedError(Exception):
    """A principal's bucket is empty (HTTP 429); `retry_after_s` is when a token is back."""

    def __init__(self, message: str, retry_after_s: float) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class TokenBucket:
    def __init__(self, *, rate: float, burst: int, now: float) -> None:
        self.rate = rate
        self.burst = burst
        self.tokens = float(burst)
        self.at = now

    def take(self, now: float) -> float:
        """Take a token: 0.0 if there was one, else the seconds until there is (none taken)."""
        self.tokens = min(float(self.burst), self.tokens + (now - self.at) * self.rate)
        self.at = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return 0.0
        return (1.0 - self.tokens) / self.rate


class RateLimiter:
    """One bucket per principal key (`munge:alice`, `token:ci-cpu`, `session:bob`)."""

    def __init__(self, config: LimitsConfig, clock: Clock | None = None) -> None:
        self.config = config
        self._clock = clock or SystemClock()
        self._buckets: OrderedDict[str, TokenBucket] = OrderedDict()
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        """Count a request of `key`; RateLimitedError if its bucket is empty."""
        now = self._clock.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = TokenBucket(rate=self.config.rate_per_s, burst=self.config.burst, now=now)
                self._buckets[key] = bucket
                while len(self._buckets) > self.config.max_principals:
                    self._buckets.popitem(last=False)
            else:
                self._buckets.move_to_end(key)
            wait = bucket.take(now)
        if wait > 0:
            name = key.partition(":")[2]
            raise RateLimitedError(
                f"rate limit of {name!r} exceeded ({self.config.rate_per_s:g} requests/s, "
                f"bursts of {self.config.burst}; [meta_service.limits]): retry in {wait:.2f} s",
                wait,
            )


def retry_after(seconds: float) -> str:
    """The `Retry-After` header: whole seconds, at least 1."""
    return str(max(1, math.ceil(seconds)))


class BodyLimit:
    """ASGI middleware refusing request bodies over `max_bytes` with 413."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    def _message(self) -> str:
        return (
            f"request body larger than {self.max_bytes} bytes "
            "([meta_service.limits].max_body_bytes); send smaller batches"
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        length = dict(scope.get("headers", [])).get(b"content-length")
        if length is not None and length.isdigit() and int(length) > self.max_bytes:
            await _send_json(send, 413, ErrorBody(detail=self._message()).model_dump())
            return
        # Read the body first (the routes read it whole anyway), so an oversized one is refused
        # here, before any route or middleware runs, then hand it on unchanged.
        chunks: list[bytes] = []
        seen = 0
        more = True
        while more:
            message = await receive()
            if message["type"] != "http.request":  # the client went away
                await self.app(scope, _replay([], message, receive), send)
                return
            body = message.get("body", b"")
            seen += len(body)
            if seen > self.max_bytes:
                await _send_json(send, 413, ErrorBody(detail=self._message()).model_dump())
                return
            chunks.append(body)
            more = bool(message.get("more_body", False))
        whole: Message = {"type": "http.request", "body": b"".join(chunks), "more_body": False}
        await self.app(scope, _replay([whole], None, receive), send)


def _replay(first: list[Message], last: Message | None, receive: Receive) -> Receive:
    """A receive that returns `first`, then `last`, then whatever `receive` returns."""
    queue = [*first, *([] if last is None else [last])]

    async def replayed() -> Message:
        if queue:
            return queue.pop(0)
        return await receive()

    return replayed


async def _send_json(send: Send, status: int, doc: dict[str, Any]) -> None:
    body = json.dumps(doc).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
