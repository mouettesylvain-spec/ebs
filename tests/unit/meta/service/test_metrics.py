"""Prometheus metrics and request-id logging of the metadata service (P2-05 R5)."""

from __future__ import annotations

import io
import json
import re

import pytest

from ebs.core.clock import FakeClock
from ebs.core.digest import hash_bytes
from ebs.core.log import configure_logging
from ebs.meta.api import API_PREFIX
from ebs.meta.memory import InMemoryMetadataStore
from ebs.meta.service.metrics import Metrics, PoolStats
from tests.helpers.meta_http import FakeMunge, Service, make_service
from tests.helpers.release import Action, finish, out

ALICE = FakeMunge()  # one credential source: a new one would replay serial 0

_SAMPLE = re.compile(r'^([a-z_:][a-z0-9_:]*)(\{(?:[a-z_]+="(?:[^"\\]|\\.)*",?)*\})? (\S+)$')


def _service() -> Service:
    store = InMemoryMetadataStore(clock=FakeClock())
    return make_service(
        store,
        metrics=Metrics(pool=lambda: PoolStats(size=5, checked_out=2, overflow=1)),
    )


def _samples(text: str) -> dict[str, float]:
    """`name{labels}` -> value; every non-comment line must be a valid sample."""
    found: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            assert re.match(r"^# (HELP|TYPE) [a-z_]+ ", line) or not line, line
            continue
        m = _SAMPLE.match(line)
        assert m, f"not a Prometheus sample: {line!r}"
        found[m.group(1) + (m.group(2) or "")] = float(m.group(3))
    return found


def _metrics(svc: Service) -> dict[str, float]:
    r = svc.client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain; version=0.0.4")
    return _samples(r.text)


# R5: request latency per route template (never per id), method and status.
def test_request_latency_histogram() -> None:
    svc = _service()
    build = finish(svc.deps.store, {}, status=None)
    for _ in range(3):
        assert svc.client.get(f"{API_PREFIX}/builds/{build}", auth=ALICE).status_code == 200
    svc.client.get(f"{API_PREFIX}/builds/999", auth=ALICE)
    m = _metrics(svc)
    labels = f'method="GET",route="{API_PREFIX}/builds/{{build}}"'
    assert m[f'ebs_http_request_duration_seconds_count{{{labels},status="200"}}'] == 3
    assert m[f'ebs_http_request_duration_seconds_count{{{labels},status="404"}}'] == 1
    assert m[f'ebs_http_request_duration_seconds_bucket{{{labels},status="200",le="+Inf"}}'] == 3
    assert f'ebs_http_request_duration_seconds_sum{{{labels},status="200"}}' in m
    assert not [k for k in m if f"/builds/{build}" in k]


# R5: cache lookups by outcome, from which the hit ratio is computed.
def test_cache_hit_ratio() -> None:
    svc = _service()
    store = svc.deps.store
    build = finish(store, {"a": Action({"o": out(b"x")})})
    key = store.list_actions(build)[0].key
    result = store.get_result(build, "a")
    assert key is not None
    assert result is not None
    store.cache_put("test", key, result)
    client = svc.store()
    assert client.cache_get("test", key) is not None
    assert client.cache_get("test", hash_bytes(b"nothing")) is None
    client.cache_get_many("test", [key, hash_bytes(b"other"), hash_bytes(b"third")])
    # A peek (`ebs plan`'s prediction) is no lookup: it counts neither way.
    assert client.cache_peek("test", key) is not None
    assert client.cache_peek("test", hash_bytes(b"nothing")) is None
    m = _metrics(svc)
    assert m['ebs_cache_lookups_total{result="hit"}'] == 2
    assert m['ebs_cache_lookups_total{result="miss"}'] == 3


# R5: the database pool's size, connections in use and overflow.
def test_pool_gauges() -> None:
    m = _metrics(_service())
    assert m["ebs_db_pool_size"] == 5
    assert m["ebs_db_pool_checked_out"] == 2
    assert m["ebs_db_pool_overflow"] == 1


# R5: label values are bounded: any method token outside HTTP's own is counted as "other".
def test_method_label_bounded() -> None:
    svc = _service()
    for i in range(3):
        svc.client.request(f"XQ{i}", "/healthz")
    text = svc.client.get("/metrics").text
    assert "XQ" not in text
    assert 'method="other",route="/healthz"' in text


# R5: no domain names in the metrics: they may be confidential themselves.
def test_no_domains_in_labels() -> None:
    svc = _service()
    svc.client.get(f"{API_PREFIX}/domains/secret-nda/cache/{hash_bytes(b'k')}", auth=ALICE)
    text = svc.client.get("/metrics").text
    assert "secret-nda" not in text
    assert f'route="{API_PREFIX}/domains/{{domain}}/cache/{{key}}"' in text


@pytest.fixture
def log() -> io.StringIO:
    stream = io.StringIO()
    configure_logging("INFO", json=True, stream=stream)
    return stream  # tests/conftest.py restores the logging configuration


def _lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


# R5: every request gets an id (the caller's X-Request-ID if well formed), returned in the
# response and on every JSON log line written while serving it.
def test_request_ids_in_json_logs(log: io.StringIO) -> None:
    svc = _service()
    r = svc.client.get(f"{API_PREFIX}/whoami", auth=ALICE, headers={"X-Request-ID": "ci-42.a"})
    assert r.headers["X-Request-ID"] == "ci-42.a"
    access = [line for line in _lines(log) if line.get("event") == "request"]
    assert access[-1]["request_id"] == "ci-42.a"
    assert access[-1]["status"] == 200
    assert access[-1]["principal"] == "alice"
    assert access[-1]["route"] == f"{API_PREFIX}/whoami"
    assert isinstance(access[-1]["duration_ms"], float)

    r = svc.client.get(f"{API_PREFIX}/whoami", headers={"X-Request-ID": "bad id\n"})
    generated = r.headers["X-Request-ID"]
    assert re.fullmatch(r"[0-9a-f]{32}", generated)
    failed = [line for line in _lines(log) if line.get("event") == "authentication failed"]
    assert failed[-1]["request_id"] == generated  # a log line of the route itself
    assert all(line.get("request_id") != "bad id\n" for line in _lines(log))
