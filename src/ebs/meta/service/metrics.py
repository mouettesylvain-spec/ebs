"""Observability of the metadata service (P2-05 R5): Prometheus metrics and request ids.

`GET /metrics` (no credentials, like the probes; firewall it to the scraper) serves the text
exposition format (version 0.0.4), written here rather than with a client library:

    ebs_http_request_duration_seconds{method,route,status}   histogram, per route template
    ebs_cache_lookups_total{result="hit"|"miss"}             hit ratio = hit / (hit + miss)
    ebs_db_pool_size, ebs_db_pool_checked_out, ebs_db_pool_overflow   SQLAlchemy pool gauges

Labels never carry ids or domain names (domain names can be confidential themselves). Counters
are per process: scrape each worker, or run one worker per port.

`ObservabilityMiddleware` gives every request an id (the caller's `X-Request-ID` if it is
well formed, else a new one), returns it in `X-Request-ID`, binds it into the structlog context
so every log line of the request carries it, times the request and writes one `request` access
log line (method, route, status, duration, principal).
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ebs.core.log import get_logger
from ebs.meta.api import API_PREFIX

__all__ = [
    "BUCKETS",
    "REQUEST_ID_HEADER",
    "Metrics",
    "ObservabilityMiddleware",
    "PoolStats",
    "route_template",
]

_log = get_logger(__name__)

REQUEST_ID_HEADER: Final = "X-Request-ID"
_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
_CONVERTER = re.compile(r"\{(\w+):\w+\}")
_METHODS: Final = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})

BUCKETS: Final[tuple[float, ...]] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 300.0,
)  # fmt: skip


@dataclass(frozen=True)
class PoolStats:
    size: int
    checked_out: int
    overflow: int


@dataclass
class _Histogram:
    counts: list[int]
    total: float = 0.0
    n: int = 0


def _labels(pairs: Sequence[tuple[str, str]]) -> str:
    def esc(v: str) -> str:
        return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    return "{" + ",".join(f'{k}="{esc(v)}"' for k, v in pairs) + "}"


class Metrics:
    """The service's metrics; `pool` reports the database connection pool, if any."""

    def __init__(self, pool: Callable[[], PoolStats] | None = None) -> None:
        self.pool = pool
        self._lock = threading.Lock()
        self._requests: dict[tuple[str, str, str], _Histogram] = {}
        self._cache = {"hit": 0, "miss": 0}

    def observe_request(self, method: str, route: str, status: int, seconds: float) -> None:
        key = (method, route, str(status))
        with self._lock:
            h = self._requests.get(key)
            if h is None:
                h = self._requests[key] = _Histogram(counts=[0] * len(BUCKETS))
            for i, bound in enumerate(BUCKETS):
                if seconds <= bound:
                    h.counts[i] += 1
            h.total += seconds
            h.n += 1

    def count_cache(self, *, hits: int = 0, misses: int = 0) -> None:
        with self._lock:
            self._cache["hit"] += hits
            self._cache["miss"] += misses

    def render(self) -> str:
        lines = [
            "# HELP ebs_http_request_duration_seconds Request latency by route template.",
            "# TYPE ebs_http_request_duration_seconds histogram",
        ]
        with self._lock:
            requests = {k: (list(h.counts), h.total, h.n) for k, h in self._requests.items()}
            cache = dict(self._cache)
        name = "ebs_http_request_duration_seconds"
        for (method, route, status), (counts, total, n) in sorted(requests.items()):
            base = [("method", method), ("route", route), ("status", status)]
            for bound, count in zip(BUCKETS, counts, strict=True):
                lines.append(f"{name}_bucket{_labels([*base, ('le', f'{bound:g}')])} {count}")
            lines.append(f"{name}_bucket{_labels([*base, ('le', '+Inf')])} {n}")
            lines.append(f"{name}_sum{_labels(base)} {total:.6f}")
            lines.append(f"{name}_count{_labels(base)} {n}")
        lines += [
            "# HELP ebs_cache_lookups_total Action cache lookups by result.",
            "# TYPE ebs_cache_lookups_total counter",
            *(f'ebs_cache_lookups_total{{result="{r}"}} {cache[r]}' for r in ("hit", "miss")),
        ]
        if self.pool is not None:
            stats = self.pool()
            for metric, value, text in (
                ("size", stats.size, "Connections the pool keeps."),
                ("checked_out", stats.checked_out, "Connections in use."),
                ("overflow", stats.overflow, "Connections beyond the pool size."),
            ):
                lines += [
                    f"# HELP ebs_db_pool_{metric} {text}",
                    f"# TYPE ebs_db_pool_{metric} gauge",
                    f"ebs_db_pool_{metric} {value}",
                ]
        return "\n".join(lines) + "\n"


def route_template(scope: Scope) -> str:
    """The matched route's path template (`/api/v1/builds/{build}`), never the actual path."""
    route = scope.get("route")
    template = getattr(route, "path", None)
    if not isinstance(template, str):
        return "unmatched"
    if str(scope.get("path", "")).startswith(API_PREFIX) and not template.startswith(API_PREFIX):
        template = API_PREFIX + template  # routers included with the prefix keep their own path
    return _CONVERTER.sub(r"{\1}", template)


class ObservabilityMiddleware:
    def __init__(self, app: ASGIApp, metrics: Metrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        given = dict(scope.get("headers", [])).get(REQUEST_ID_HEADER.lower().encode(), b"")
        text = given.decode("latin-1")
        request_id = text if _REQUEST_ID.fullmatch(text) else uuid.uuid4().hex
        status = 500
        start = time.perf_counter()

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                headers = list(message.get("headers", []))
                headers.append((REQUEST_ID_HEADER.lower().encode(), request_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        with structlog.contextvars.bound_contextvars(request_id=request_id):
            try:
                await self.app(scope, receive, send_with_id)
            finally:
                seconds = time.perf_counter() - start
                route = route_template(scope)
                given_method = str(scope.get("method", ""))
                # Any token is a method to the HTTP parser: keep the label set bounded.
                method = given_method if given_method in _METHODS else "other"
                self.metrics.observe_request(method, route, status, seconds)
                principal = (scope.get("state") or {}).get("principal")
                _log.info(
                    "request",
                    method=method,
                    route=route,
                    status=status,
                    duration_ms=round(seconds * 1000, 3),
                    principal=getattr(principal, "name", None),
                )
