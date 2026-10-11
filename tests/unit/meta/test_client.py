"""HttpMetadataStore transport behaviour: retries, timeouts, errors, credentials (P1-06 R5)."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator, Sequence
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import respx

from ebs.core.digest import Digest, hash_bytes
from ebs.core.errors import ConfigError, MetadataError
from ebs.meta.api import (
    API_PREFIX,
    BuildCreate,
    BuildId,
    Changed,
    OptionalResult,
    OutputResult,
    ResourceUsage,
    ResultManifest,
    RunnerInfo,
)
from ebs.meta.client import (
    DEFAULT_TIMEOUT_S,
    BearerCredential,
    HttpMetadataConfig,
    HttpMetadataStore,
    MungeCredential,
    munge_payload,
)

URL = "https://meta.example.invalid"
API = URL + API_PREFIX


def dg(label: str) -> Digest:
    return hash_bytes(label.encode())


def make_result(key: Digest) -> ResultManifest:
    return ResultManifest(
        action_key=key,
        status="passed",
        exit_code=0,
        inputs={},
        outputs={"r": OutputResult(digest=dg("r"), id=dg("r"), type="file", size=1)},
        log=None,
        summary={},
        resources=ResourceUsage(max_rss_kb=1, cpu_s=1, wall_s=1),
        runner=RunnerInfo(version="0.1.0", host="node-a"),
    )


class Creds:
    def __init__(self) -> None:
        self.n = 0

    def headers(self, method: str, path: str) -> dict[str, str]:
        self.n += 1
        return {"X-EBS-Munge": f"cred-{self.n}"}


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def creds() -> Creds:
    return Creds()


@pytest.fixture
def store(sleeps: list[float], creds: Creds) -> Iterator[HttpMetadataStore]:
    s = HttpMetadataStore(URL, credentials=creds, retries=3, backoff_s=0.5, sleep=sleeps.append)
    yield s
    s.close()


KEY = dg("k")
CACHE = f"{API}/domains/test/cache/{KEY}"
OK_RESULT = httpx.Response(200, content=OptionalResult(result=make_result(KEY)).model_dump_json())


# R5
@respx.mock
def test_retry_idempotent(store: HttpMetadataStore, sleeps: list[float], creds: Creds) -> None:
    route = respx.get(CACHE).mock(
        side_effect=[
            httpx.ConnectError("refused"),
            httpx.Response(503, json={"detail": "database down"}),
            httpx.Response(502),
            OK_RESULT,
        ]
    )
    assert store.cache_get("test", KEY) == make_result(KEY)
    assert route.call_count == 4
    assert sleeps == [0.5, 1.0, 2.0]  # exponential backoff
    # A fresh credential per attempt: MUNGE refuses replays.
    seen = [c.request.headers["X-EBS-Munge"] for c in route.calls]
    assert len(set(seen)) == 4

    # cache_put and record_result are idempotent by design and retried on 5xx too.
    put = respx.put(CACHE).mock(
        side_effect=[httpx.Response(500), httpx.Response(200, json={"changed": False})]
    )
    assert store.cache_put("test", KEY, make_result(KEY)) is False
    assert put.call_count == 2
    rec = respx.put(f"{API}/builds/7/actions/a/result").mock(
        side_effect=[httpx.ReadTimeout("slow"), httpx.Response(204)]
    )
    store.record_result(BuildId(7), "a", make_result(KEY))
    assert rec.call_count == 2


# R5
@respx.mock
def test_retries_exhausted(store: HttpMetadataStore, sleeps: list[float]) -> None:
    route = respx.get(CACHE).mock(return_value=httpx.Response(503, json={"detail": "db down"}))
    with pytest.raises(MetadataError, match=r"meta\.example\.invalid.*db down"):
        store.cache_get("test", KEY)
    assert route.call_count == 4  # first try + 3 retries
    assert len(sleeps) == 3


# R5: a non-idempotent call is retried only when the request never reached the server.
@respx.mock
def test_non_idempotent_not_retried_after_send(store: HttpMetadataStore) -> None:
    state = respx.post(f"{API}/builds/7/actions/a/state").mock(
        side_effect=[httpx.ConnectError("refused"), httpx.Response(503), httpx.Response(204)]
    )
    with pytest.raises(MetadataError, match="503"):
        store.set_action_state(BuildId(7), "a", "running")
    assert state.call_count == 2  # connect error retried, 503 not
    finish = respx.post(f"{API}/builds/7/finish").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(MetadataError, match="timed out"):
        store.finish_build(BuildId(7), "passed")
    assert finish.call_count == 1


# R5: 4xx answers are final and carry the server's message.
@respx.mock
def test_client_errors_not_retried(store: HttpMetadataStore) -> None:
    route = respx.get(f"{API}/builds/9").mock(
        return_value=httpx.Response(409, json={"detail": "no build with id 9"})
    )
    with pytest.raises(MetadataError, match="no build with id 9"):
        store.get_build(BuildId(9))
    assert route.call_count == 1
    respx.get(f"{API}/builds/8").mock(
        return_value=httpx.Response(401, json={"detail": "MUNGE credential refused"})
    )
    with pytest.raises(MetadataError, match=r"authentication.*MUNGE credential refused"):
        store.get_build(BuildId(8))


# R5: every request times out after 10 s.
def test_timeout_10s(store: HttpMetadataStore) -> None:
    assert DEFAULT_TIMEOUT_S == 10.0
    timeout = store._client.timeout
    assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (10.0,) * 4


@respx.mock
def test_retry_idempotent_post_lookups(store: HttpMetadataStore) -> None:
    route = respx.post(f"{API}/domains/test/cache/lookup").mock(
        side_effect=[httpx.Response(500), httpx.Response(200, json={"results": {}})]
    )
    assert store.cache_get_many("test", [KEY]) == {}
    assert route.call_count == 2
    touch = respx.post(f"{API}/domains/test/touch").mock(
        side_effect=[httpx.Response(502), httpx.Response(204)]
    )
    store.touch("test", [KEY])
    assert touch.call_count == 2


@respx.mock
def test_paths_are_quoted(store: HttpMetadataStore) -> None:
    route = respx.get(f"{API}/builds/3/actions/sim%5Bt%3Da%2Fb%3Fx%5D/result").mock(
        return_value=httpx.Response(200, json={"result": None})
    )
    assert store.get_result(BuildId(3), "sim[t=a/b?x]") is None
    assert route.called


def test_local_validation_before_sending(store: HttpMetadataStore) -> None:
    with pytest.raises(MetadataError, match="domain"):
        store.cache_get("../x", KEY)
    with pytest.raises(MetadataError, match="Digest"):
        store.cache_get("test", "sha256:" + "0" * 64)  # type: ignore[arg-type]


@respx.mock
def test_unexpected_response_is_metadata_error(store: HttpMetadataStore) -> None:
    respx.put(CACHE).mock(return_value=httpx.Response(200, text="<html>proxy</html>"))
    with pytest.raises(MetadataError, match="unexpected response"):
        store.cache_put("test", KEY, make_result(KEY))
    assert Changed(changed=True)  # model used by the happy path


# --- credentials and config -----------------------------------------------------------------------


def test_bearer_credential() -> None:
    assert BearerCredential("tok").headers("GET", "/x") == {"Authorization": "Bearer tok"}
    with pytest.raises(ConfigError):
        BearerCredential(" ")


def test_munge_credential_runs_munge() -> None:
    calls: list[list[str]] = []

    def run(argv: Sequence[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, "MUNGE:AwQ:\n", "")

    cred = MungeCredential("meta.example.invalid", run=run)
    assert cred.headers("POST", "/builds/7/actions/a b/state") == {"X-EBS-Munge": "MUNGE:AwQ:"}
    # The payload binds the credential to this service and request (D1).
    payload = '["ebs-meta/1","meta.example.invalid","POST","/builds/7/actions/a b/state"]'
    assert calls == [["munge", "--string", payload]]
    assert munge_payload("meta.example.invalid", "post", "/builds/7/actions/a b/state") == payload

    def fail(argv: Sequence[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(argv), 1, "", "munge: Error: munged not running")

    with pytest.raises(MetadataError, match="munged not running"):
        MungeCredential("meta.example.invalid", run=fail).headers("GET", "/whoami")


def test_config(tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_text("secret-token\n")
    token.chmod(0o600)
    cfg = HttpMetadataConfig.from_mapping({"url": URL, "token_file": str(token)})
    store = HttpMetadataStore.from_config(cfg, environ={})
    assert isinstance(store._credentials, BearerCredential)
    assert store._credentials.headers("GET", "/x") == {"Authorization": "Bearer secret-token"}
    store.close()
    munge = HttpMetadataStore.from_config({"url": URL}, environ={})
    assert isinstance(munge._credentials, MungeCredential)
    assert munge._credentials.audience == "meta.example.invalid"  # host of [metadata].url
    munge.close()
    env = HttpMetadataStore.from_config({"url": URL}, environ={"EBS_META_TOKEN": "t2"})
    assert env._credentials.headers("GET", "/x") == {"Authorization": "Bearer t2"}  # type: ignore[union-attr]
    env.close()
    for bad in ({"url": "ftp://x"}, {"url": URL, "colour": 1}, {"url": URL, "auth": "magic"}):
        with pytest.raises(ConfigError):
            HttpMetadataConfig.from_mapping(bad)
    with pytest.raises(ConfigError, match="token"):
        HttpMetadataStore.from_config({"url": URL, "auth": "token"}, environ={})
    token.chmod(0o644)
    with pytest.raises(ConfigError, match="readable by others"):
        HttpMetadataStore.from_config({"url": URL, "token_file": str(token)}, environ={})


def test_build_create_round_trip_over_wire() -> None:
    spec = BuildCreate(
        uuid=uuid4(),
        domain="d",
        project="p",
        plan_digest=dg("p"),
        flow_repo="r",
        flow_commit="c",
        flow_dirty=False,
        user_name="u",
        cache_mode="read",
    )
    assert BuildCreate.model_validate_json(spec.model_dump_json()) == spec
