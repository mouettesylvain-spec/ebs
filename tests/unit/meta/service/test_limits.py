"""Rate limits per principal and request body limits (P2-05 R4)."""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import httpx
import pytest

from ebs.core.clock import FakeClock
from ebs.core.digest import hash_bytes
from ebs.core.errors import ConfigError
from ebs.meta.api import API_PREFIX, BuildCreate
from ebs.meta.client import HttpMetadataStore
from ebs.meta.memory import InMemoryMetadataStore
from ebs.meta.service.app import ServiceConfig
from ebs.meta.service.limits import LimitsConfig, RateLimiter, TokenBucket
from tests.helpers.meta_http import FakeMunge, Service, make_service
from tests.helpers.release import finish

ALICE = FakeMunge()  # one credential source: a new one would replay serial 0

WHOAMI = f"{API_PREFIX}/whoami"


# R4: a bucket holds `burst` tokens and refills at `rate` per second.
def test_token_bucket() -> None:
    clock = FakeClock()
    bucket = TokenBucket(rate=2.0, burst=3, now=clock.monotonic())
    assert [bucket.take(clock.monotonic()) for _ in range(4)] == [0.0, 0.0, 0.0, 0.5]
    clock.advance(0.25)
    assert bucket.take(clock.monotonic()) == pytest.approx(0.25)  # half a token so far
    clock.advance(0.25)
    assert bucket.take(clock.monotonic()) == 0.0
    clock.advance(100)
    assert [bucket.take(clock.monotonic()) for _ in range(4)][-1] > 0  # never above burst


def _service(clock: FakeClock, **limits: object) -> Service:
    config = LimitsConfig(**({"rate_per_s": 1.0, "burst": 2} | limits))  # type: ignore[arg-type]
    return make_service(
        InMemoryMetadataStore(clock=clock), limits=config, limiter=RateLimiter(config, clock)
    )


# R4: past its burst a principal gets 429 with Retry-After; others are not affected; tokens
# come back with time.
def test_rate_limited_per_principal() -> None:
    clock = FakeClock()
    svc = _service(clock)
    alice, bob = FakeMunge(1000), FakeMunge(1001)
    assert [svc.client.get(WHOAMI, auth=alice).status_code for _ in range(2)] == [200, 200]
    r = svc.client.get(WHOAMI, auth=alice)
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "1"
    assert "alice" in r.json()["detail"]
    assert "[meta_service.limits]" in r.json()["detail"]
    assert svc.client.get(WHOAMI, auth=bob).status_code == 200
    clock.advance(1.0)
    assert svc.client.get(WHOAMI, auth=alice).status_code == 200
    assert svc.client.get(WHOAMI, auth=alice).status_code == 429


# R4: the probes and unauthenticated requests are not counted against anyone.
def test_probes_not_limited() -> None:
    svc = _service(FakeClock(), burst=1)
    assert all(svc.client.get("/healthz").status_code == 200 for _ in range(5))
    assert svc.client.get(WHOAMI).status_code == 401
    assert svc.client.get(WHOAMI, auth=ALICE).status_code == 200


# R4: bodies over `max_body_bytes` get 413, by Content-Length or while streaming.
def test_body_size_limit() -> None:
    clock = FakeClock()
    svc = _service(clock, rate_per_s=1000.0, burst=1000, max_body_bytes=1024)
    store = svc.deps.store
    build = finish(store, {}, status=None)
    url = f"{API_PREFIX}/builds/{build}/actions"
    big = b'{"actions": [' + b" " * 2048 + b"]}"
    r = svc.client.post(url, content=big, auth=ALICE)
    assert r.status_code == 413
    assert "1024 bytes" in r.json()["detail"]

    def chunks() -> Iterator[bytes]:
        yield b'{"actions": ['
        yield b" " * 2048
        yield b"]}"

    r = svc.client.post(url, content=chunks(), auth=ALICE)
    assert r.status_code == 413
    ok = svc.client.post(url, content=b'{"actions": []}', auth=ALICE)
    assert ok.status_code == 204


# R4: the HTTP store waits Retry-After and retries a 429, also for non-idempotent calls: the
# service refused the request before doing anything.
def test_client_retries_429() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if len(calls) < 3:
            return httpx.Response(429, headers={"Retry-After": "7"}, json={"detail": "slow down"})
        return httpx.Response(200, json={"id": 5})

    slept: list[float] = []
    client = HttpMetadataStore(
        "http://meta.test", client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=slept.append, retries=4,
    )  # fmt: skip
    spec = BuildCreate(
        uuid=uuid4(), domain="test", project="demo", plan_digest=hash_bytes(b"p"), flow_repo="",
        flow_commit="", flow_dirty=False, user_name="alice", cache_mode="write",
    )  # fmt: skip
    assert client.create_build(spec) == 5
    assert calls == ["POST"] * 3
    assert slept == [7.0, 7.0]


# R4: the limits are configured in [meta_service.limits].
def test_limits_config() -> None:
    cfg = ServiceConfig.from_mapping(
        {"domains": [], "limits": {"rate_per_s": 5.5, "burst": 10, "max_body_bytes": 2048}}
    )
    assert cfg.limits == LimitsConfig(rate_per_s=5.5, burst=10, max_body_bytes=2048)
    assert ServiceConfig.from_mapping({}).limits == LimitsConfig()
    with pytest.raises(ConfigError, match="limits"):
        ServiceConfig.from_mapping({"limits": {"burst": 0}})
