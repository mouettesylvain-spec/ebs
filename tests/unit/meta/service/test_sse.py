"""Server-Sent Events of builds and domains (P2-05 R2), on the in-memory store."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from ebs.core.clock import FakeClock
from ebs.meta.api import API_PREFIX, BuildId, Event
from ebs.meta.memory import InMemoryMetadataStore
from ebs.meta.service.events_stream import EventStreamConfig, Wakeup, stream_events
from tests.helpers.meta_http import FakeMunge, Service, make_service
from tests.helpers.release import finish
from tests.helpers.sse import parse

ALICE = FakeMunge()  # one credential source: a new one would replay serial 0


def _service(**config: Any) -> tuple[Service, InMemoryMetadataStore]:
    store = InMemoryMetadataStore(clock=FakeClock())
    svc = make_service(store, events=EventStreamConfig(**({"max_stream_s": 0.0} | config)))
    return svc, store


def _emit(store: InMemoryMetadataStore, build: BuildId, type_: str) -> None:
    store.emit(Event(ts=FakeClock().now(), build=build, type=type_, data={"k": type_}))  # type: ignore[arg-type]


def _events(frames: list[dict[str, str]]) -> list[dict[str, str]]:
    return [f for f in frames if "data" in f]


# R2: a finished build's stream sends its events in order with their ids, then ends.
def test_build_stream_sends_events_and_ends() -> None:
    svc, store = _service()
    build = finish(store, {}, status=None)
    for t in ("build_started", "running", "build_finished"):
        _emit(store, build, t)
    store.finish_build(build, "passed")
    r = svc.client.get(f"{API_PREFIX}/builds/{build}/events", auth=ALICE)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache"
    frames = parse(r.text)
    assert frames[0] == {"retry": "3000"}
    sent = _events(frames)
    assert [f["event"] for f in sent] == ["build_started", "running", "build_finished"]
    docs = [json.loads(f["data"]) for f in sent]
    assert [d["id"] for d in docs] == [int(f["id"]) for f in sent]
    assert docs[1]["event"]["type"] == "running"
    assert docs[1]["event"]["build"] == build


# R2: `Last-Event-ID` (EventSource reconnects) or `?after=` resume after an id, nothing before.
def test_resume_after_last_event_id() -> None:
    svc, store = _service()
    build = finish(store, {}, status=None)
    for t in ("build_started", "running", "finished", "build_finished"):
        _emit(store, build, t)
    store.finish_build(build, "passed")
    url = f"{API_PREFIX}/builds/{build}/events"
    first = _events(parse(svc.client.get(url, auth=ALICE).text))
    second_id = first[1]["id"]
    resumed = svc.client.get(url, auth=ALICE, headers={"Last-Event-ID": second_id})
    assert [f["event"] for f in _events(parse(resumed.text))] == ["finished", "build_finished"]
    by_query = svc.client.get(url, auth=ALICE, params={"after": first[2]["id"]})
    assert [f["event"] for f in _events(parse(by_query.text))] == ["build_finished"]
    # EventSource reconnects to the same URL with Last-Event-ID: the later of the two wins.
    both = svc.client.get(
        url, auth=ALICE, params={"after": first[0]["id"]}, headers={"Last-Event-ID": first[2]["id"]}
    )
    assert [f["event"] for f in _events(parse(both.text))] == ["build_finished"]
    for bad_id in ("x", "-1", "1e3", "9" * 30):
        bad = svc.client.get(url, auth=ALICE, headers={"Last-Event-ID": bad_id})
        assert bad.status_code == 422, bad_id


# R2: a domain's stream carries every build of that domain, no other.
def test_domain_stream() -> None:
    svc, store = _service()
    one = finish(store, {}, status=None)
    other = finish(store, {}, status=None, domain="other")
    _emit(store, one, "build_started")
    _emit(store, other, "build_started")
    _emit(store, one, "running")
    r = svc.client.get(f"{API_PREFIX}/domains/test/events", auth=ALICE)
    assert r.status_code == 200
    builds = [json.loads(f["data"])["event"]["build"] for f in _events(parse(r.text))]
    assert builds == [one, one]


# R2: an idle stream waits for a wake-up (not a poll), then sends what was emitted; a
# keepalive comment goes out when nothing happens.
def test_stream_wakes_on_emit() -> None:
    svc, store = _service(max_stream_s=60.0, poll_s=3600.0, keepalive_s=3600.0)
    build = finish(store, {}, status=None)
    _emit(store, build, "build_started")

    async def scenario() -> list[str]:
        frames = stream_events(svc.deps, build=build, domain=None, after=0)
        got = [await anext(frames), await anext(frames)]  # retry, build_started
        waiting = asyncio.ensure_future(anext(frames))
        await asyncio.sleep(0)  # let it subscribe and block
        assert not waiting.done()
        _emit(store, build, "running")
        svc.deps.wakeup.notify(f"build:{build}")
        got.append(await asyncio.wait_for(waiting, 10))
        store.finish_build(build, "passed")
        svc.deps.wakeup.notify(f"build:{build}")
        got.extend([frame async for frame in frames])  # ends: the build is finished
        return got

    out = asyncio.run(scenario())
    assert [f.get("event") for f in parse("".join(out))] == [None, "build_started", "running"]

    async def idle() -> str:
        quiet = EventStreamConfig(max_stream_s=60.0, poll_s=3600.0, keepalive_s=0.001)
        svc.deps.events = quiet
        frames = stream_events(svc.deps, build=None, domain="test", after=10**9)
        await anext(frames)
        return await asyncio.wait_for(anext(frames), 10)

    assert asyncio.run(idle()) == ": keepalive\n\n"


# R2: the route's emit and finish wake the streams of that build and of its domain.
def test_routes_notify() -> None:
    svc, store = _service()
    build = finish(store, {}, status=None)
    seen: list[tuple[str, ...]] = []
    svc.deps.wakeup = _Recorder(seen)
    client = svc.store()
    client.emit(Event(ts=FakeClock().now(), build=build, type="running", data={}))
    client.finish_build(build, "passed")
    assert seen == [(f"build:{build}", "domain:test")] * 2


class _Recorder(Wakeup):
    def __init__(self, seen: list[tuple[str, ...]]) -> None:
        super().__init__()
        self.seen = seen

    def notify(self, *keys: str) -> None:
        self.seen.append(keys)


# R2: subscriptions wake only for their keys.
def test_wakeup_keys() -> None:
    async def scenario() -> tuple[bool, bool]:
        wakeup = Wakeup()
        with wakeup.subscribe({"build:1"}) as one, wakeup.subscribe({"domain:d"}) as dom:
            wakeup.notify("build:2", "domain:e")
            await asyncio.sleep(0)
            assert not one.is_set()
            assert not dom.is_set()
            wakeup.notify("build:1", "domain:d")
            woke = await one.wait(10)
            return woke, await dom.wait(10)
        raise AssertionError("unreachable")

    assert asyncio.run(scenario()) == (True, True)
    assert Wakeup().subscriptions == 0
