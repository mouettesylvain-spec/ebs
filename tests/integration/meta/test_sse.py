"""The event stream on PostgreSQL (P2-05 R2): resuming with `Last-Event-ID`, and wake-ups
through LISTEN/NOTIFY from another process's emits."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator

import pytest
from sqlalchemy.engine import URL

from ebs.core.clock import FakeClock
from ebs.meta.api import API_PREFIX, Event
from ebs.meta.pg import PgMetadataStore
from ebs.meta.service.events_stream import EventStreamConfig, PgEventListener, stream_events
from tests.helpers.meta_http import FakeMunge, make_service
from tests.helpers.pg import url_string
from tests.helpers.release import finish
from tests.helpers.sse import parse

ALICE = FakeMunge()  # one credential source: a new one would replay serial 0

pytestmark = pytest.mark.integration


@pytest.fixture
def pg(pg_url: URL) -> Iterator[PgMetadataStore]:
    store = PgMetadataStore.from_config({"url": url_string(pg_url)}, clock=FakeClock())
    try:
        yield store
    finally:
        store.close()


def _event(build: int, type_: str) -> Event:
    return Event(ts=FakeClock().now(), build=build, type=type_, data={"t": type_})  # type: ignore[arg-type]


# R2
def test_resume_last_event_id(pg: PgMetadataStore) -> None:
    svc = make_service(pg, events=EventStreamConfig(max_stream_s=0.0))
    build = finish(pg, {}, status=None)
    other = finish(pg, {}, status=None)
    for t in ("build_started", "running", "finished"):
        pg.emit(_event(build, t))
        pg.emit(_event(other, t))  # interleaved ids: the stream skips them
    pg.emit(_event(build, "build_finished"))
    pg.finish_build(build, "passed")
    url = f"{API_PREFIX}/builds/{build}/events"
    frames = [f for f in parse(svc.client.get(url, auth=ALICE).text) if "data" in f]
    assert [f["event"] for f in frames] == [
        "build_started",
        "running",
        "finished",
        "build_finished",
    ]
    resumed = svc.client.get(url, auth=ALICE, headers={"Last-Event-ID": frames[1]["id"]})
    rest = [f for f in parse(resumed.text) if "data" in f]
    assert [f["event"] for f in rest] == ["finished", "build_finished"]
    assert [json.loads(f["data"])["id"] for f in rest] == [int(f["id"]) for f in frames[2:]]


# R2: an emit by another store (another service worker, or the driver writing directly) wakes
# the stream through NOTIFY; the stream never polls here.
def test_notify_wakes_stream(pg: PgMetadataStore, pg_url: URL) -> None:
    svc = make_service(pg, events=EventStreamConfig(max_stream_s=60.0, poll_s=3600.0))
    build = finish(pg, {}, status=None)
    listener = PgEventListener(pg.engine.url, svc.deps.wakeup)
    writer = PgMetadataStore.from_config({"url": url_string(pg_url)}, clock=FakeClock())

    async def scenario() -> list[str]:
        frames = stream_events(svc.deps, build=build, domain=None, after=0)
        got = [await anext(frames)]  # retry: the stream is subscribed and waiting
        waiting = asyncio.ensure_future(anext(frames))
        await asyncio.sleep(0)
        writer.emit(_event(build, "running"))
        got.append(await asyncio.wait_for(waiting, 20))
        writer.finish_build(build, "passed")
        got.extend([f async for f in frames])
        return got

    listener.start()
    try:
        assert listener.wait_listening(20)
        out = asyncio.run(scenario())
    finally:
        listener.stop()
        writer.close()
    assert [f.get("event") for f in parse("".join(out))] == [None, "running"]
