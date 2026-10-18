"""Server-Sent Events of builds and domains (P2-05 R2), backed by the `events` table.

    GET /api/v1/builds/{build}/events      one build (404 outside the caller's domains)
    GET /api/v1/domains/{domain}/events    every build of a domain (403 for non-members)

Each event is one frame: `id:` is its row id, `event:` its type, `data:` the `StoredEvent` JSON.
A client resumes after the last id it saw with `Last-Event-ID` (EventSource sends it when it
reconnects) or `?after=`. Ids commit in order within a domain (the stores serialize a domain's
inserts), so resuming never skips an event.

An idle stream waits on a `Wakeup` rather than polling: the routes that write events notify it
in-process, and `PgEventListener` turns NOTIFY `ebs_events` (sent by every PostgreSQL store, in
any process) into the same wake-ups. A slow poll (`poll_s`) covers a lost notification. A build's
stream ends once the build is finished and all its events are sent; every stream ends after
`max_stream_s`, and the client reconnects with `Last-Event-ID` (the `retry:` field sets its
delay), so no connection is held forever.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Annotated, Final

import psycopg
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import URL
from starlette.concurrency import run_in_threadpool

from ebs.core.clock import Clock, SystemClock
from ebs.core.log import get_logger
from ebs.meta.api import EVENT_PAGE_MAX, FINAL_BUILD_STATUSES, BuildId, StoredEvent
from ebs.meta.pg import EVENTS_CHANNEL

if TYPE_CHECKING:
    from ebs.meta.service.deps import ServiceDeps

__all__ = [
    "EventStreamConfig",
    "PgEventListener",
    "Subscription",
    "Wakeup",
    "build_key",
    "domain_key",
    "frame",
    "stream_events",
]

_log = get_logger(__name__)

MEDIA_TYPE: Final = "text/event-stream"


class EventStreamConfig(BaseModel):
    """`[meta_service.events]`: the stream's timing and the retention of the `events` table."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    poll_s: Annotated[float, Field(gt=0)] = 15.0
    keepalive_s: Annotated[float, Field(gt=0)] = 15.0
    max_stream_s: Annotated[float, Field(ge=0)] = 300.0
    retry_ms: Annotated[int, Field(ge=0)] = 3000
    retention_days: Annotated[int, Field(ge=1)] = 180


def build_key(build: int) -> str:
    return f"build:{int(build)}"


def domain_key(domain: str) -> str:
    return f"domain:{domain}"


class Subscription:
    """What one waiting stream listens for; set from any thread by `Wakeup.notify`."""

    def __init__(self, keys: frozenset[str], loop: asyncio.AbstractEventLoop) -> None:
        self.keys = keys
        self.loop = loop
        self._event = asyncio.Event()

    def is_set(self) -> bool:
        return self._event.is_set()

    def clear(self) -> None:
        self._event.clear()

    def set(self) -> None:
        self._event.set()

    async def wait(self, timeout: float) -> bool:
        """True if woken within `timeout` seconds."""
        try:
            await asyncio.wait_for(self._event.wait(), timeout)
        except TimeoutError:
            return False
        return True


class Wakeup:
    """Wakes the streams subscribed to a key (`build:<id>`, `domain:<name>`); thread-safe."""

    def __init__(self) -> None:
        self._subs: set[Subscription] = set()
        self._lock = threading.Lock()

    @property
    def subscriptions(self) -> int:
        with self._lock:
            return len(self._subs)

    @contextmanager
    def subscribe(self, keys: set[str] | frozenset[str]) -> Iterator[Subscription]:
        sub = Subscription(frozenset(keys), asyncio.get_running_loop())
        with self._lock:
            self._subs.add(sub)
        try:
            yield sub
        finally:
            with self._lock:
                self._subs.discard(sub)

    def notify(self, *keys: str) -> None:
        wanted = set(keys)
        with self._lock:
            subs = [s for s in self._subs if s.keys & wanted]
        for sub in subs:
            with contextlib.suppress(RuntimeError):  # its loop is closed: the stream is gone
                sub.loop.call_soon_threadsafe(sub.set)

    def notify_all(self) -> None:
        with self._lock:
            subs = list(self._subs)
        for sub in subs:
            with contextlib.suppress(RuntimeError):
                sub.loop.call_soon_threadsafe(sub.set)


def frame(stored: StoredEvent) -> str:
    return f"id: {stored.id}\nevent: {stored.event.type}\ndata: {stored.model_dump_json()}\n\n"


async def stream_events(
    deps: ServiceDeps,
    *,
    build: int | None,
    domain: str | None,
    after: int,
    clock: Clock | None = None,
) -> AsyncIterator[str]:
    """The frames of one stream (authorization is the route's job, before this starts)."""
    config: EventStreamConfig = deps.events
    monotonic = (clock or SystemClock()).monotonic
    deadline = monotonic() + config.max_stream_s
    keys = {build_key(build)} if build is not None else {domain_key(str(domain))}
    store = deps.store
    with deps.wakeup.subscribe(keys) as sub:
        yield f"retry: {config.retry_ms}\n\n"
        while True:
            sub.clear()  # before reading: a wake-up during the read is not lost
            finished = False
            if build is not None:
                # The status first: events emitted before the finish are then all visible.
                view = await run_in_threadpool(store.get_build, BuildId(build))
                finished = view.status in FINAL_BUILD_STATUSES
            rows = await run_in_threadpool(
                store.events_after,
                after,
                build=None if build is None else BuildId(build),
                domain=domain,
                limit=EVENT_PAGE_MAX,
            )
            for row in rows:
                yield frame(row)
                after = row.id
            if len(rows) == EVENT_PAGE_MAX:
                continue
            if finished:
                return
            remaining = deadline - monotonic()
            if remaining <= 0:
                return
            # A wake-up re-reads at once; a timeout re-reads too (the slow poll), after a
            # keepalive comment if the stream has been quiet for `keepalive_s`.
            woke = await sub.wait(min(config.poll_s, config.keepalive_s, remaining))
            if not woke and monotonic() < deadline:
                yield ": keepalive\n\n"


# --- PostgreSQL wake-ups ------------------------------------------------------------------------


class PgEventListener:
    """A thread that LISTENs on `ebs_events` and wakes the streams of the notified build and
    domain. It reconnects after an error; every (re)connect wakes all streams, which re-read,
    since notifications sent while it was disconnected are lost."""

    def __init__(
        self, url: URL, wakeup: Wakeup, *, reconnect_s: float = 2.0, poll_s: float = 1.0
    ) -> None:
        self._conninfo = url.set(drivername="postgresql").render_as_string(hide_password=False)
        self._wakeup = wakeup
        self._reconnect_s = reconnect_s
        self._poll_s = poll_s
        self._stop = threading.Event()
        self._listening = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ebs-events-listen", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    def wait_listening(self, timeout: float) -> bool:
        return self._listening.wait(timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with psycopg.connect(self._conninfo, autocommit=True) as conn:
                    conn.execute(f"LISTEN {EVENTS_CHANNEL}")
                    self._listening.set()
                    self._wakeup.notify_all()
                    while not self._stop.is_set():
                        for note in conn.notifies(timeout=self._poll_s):
                            domain, _, build = note.payload.partition(" ")
                            self._wakeup.notify(build_key(int(build)), domain_key(domain))
            except (psycopg.Error, ValueError) as exc:
                self._listening.clear()
                _log.warning("event listener lost its database connection", error=str(exc))
                self._stop.wait(self._reconnect_s)
