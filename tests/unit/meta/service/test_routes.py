"""REST routes of the metadata service (P1-06 R2, R4, R6)."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from uuid import uuid4

import pytest

from ebs.core.clock import FakeClock
from ebs.core.digest import Digest, hash_bytes
from ebs.core.errors import ConfigError, MetadataError
from ebs.meta.api import (
    API_PREFIX,
    BATCH_MAX,
    ActionRow,
    BuildCreate,
    BuildId,
    BuildView,
    Event,
    OutputResult,
    ResourceUsage,
    ResultManifest,
    RunnerInfo,
    ToolchainCreate,
)
from ebs.meta.client import BearerCredential, HttpMetadataStore
from ebs.meta.memory import InMemoryMetadataStore
from ebs.meta.service.auth import BuildOwner, Operation, Principal
from ebs.meta.service.tokens import TokenKind
from tests.helpers.meta_http import BASE_URL, FakeMunge, Service, make_service

B = f"{API_PREFIX}/builds"


def dg(label: str) -> Digest:
    return hash_bytes(label.encode())


def make_result(key: Digest) -> ResultManifest:
    return ResultManifest(
        action_key=key,
        status="passed",
        exit_code=0,
        inputs={"src/a.sv": dg("a")},
        outputs={"r": OutputResult(digest=dg("r"), id=dg("r"), type="file", size=1)},
        log=None,
        summary={},
        resources=ResourceUsage(max_rss_kb=1, cpu_s=1, wall_s=1),
        runner=RunnerInfo(version="0.1.0", host="node-a"),
    )


def make_build(user: str = "alice", project: str = "demo") -> BuildCreate:
    return BuildCreate(
        uuid=uuid4(),
        domain="test",
        project=project,
        plan_digest=dg("plan"),
        flow_repo="git@example.invalid:demo/flow.git",
        flow_commit="0" * 40,
        flow_dirty=False,
        user_name=user,
        cache_mode="write",
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def backing(clock: FakeClock) -> InMemoryMetadataStore:
    return InMemoryMetadataStore(clock=clock)


@pytest.fixture
def svc(backing: InMemoryMetadataStore) -> Service:
    return make_service(backing)


ALICE = FakeMunge(uid=1000)  # an httpx.Auth: a fresh credential bound to each request


# --- R2: OpenAPI and wire models ------------------------------------------------------------------


# R2
def test_openapi_models(svc: Service) -> None:
    r = svc.client.get(f"{API_PREFIX}/openapi.json")
    assert r.status_code == 200
    doc = r.json()
    paths = doc["paths"]
    assert all(p.startswith(API_PREFIX) for p in paths), sorted(paths)
    schemas = doc["components"]["schemas"]
    for name in (
        "ResultManifest",
        "BuildCreate",
        "BuildView",
        "ActionRow",
        "ActionBatch",
        "DigestBatch",
        "Event",
        "StateChange",
        "CacheHits",
        "ErrorBody",
    ):
        assert name in schemas, name

    def body_ref(path: str, method: str) -> str:
        content = paths[path][method]["requestBody"]["content"]["application/json"]
        return str(content["schema"]["$ref"]).rsplit("/", 1)[-1]

    def ok_ref(path: str, method: str) -> str:
        responses = paths[path][method]["responses"]
        content = responses["200"]["content"]["application/json"]
        return str(content["schema"]["$ref"]).rsplit("/", 1)[-1]

    assert body_ref(f"{B}", "post") == "BuildCreate"
    assert body_ref(f"{B}/{{build}}/actions", "post") == "ActionBatch"
    assert body_ref(f"{B}/{{build}}/actions/{{action_id}}/result", "put") == "ResultManifest"
    assert body_ref(f"{B}/{{build}}/events", "post") == "Event"
    assert body_ref(f"{API_PREFIX}/domains/{{domain}}/cache/{{key}}", "put") == "ResultManifest"
    assert body_ref(f"{API_PREFIX}/domains/{{domain}}/cache/lookup", "post") == "DigestBatch"
    assert body_ref(f"{API_PREFIX}/domains/{{domain}}/touch", "post") == "DigestBatch"
    assert ok_ref(f"{B}/{{build}}", "get") == "BuildView"
    # The published schemas are the models' own JSON schemas (same fields and limits).
    assert set(schemas["ResultManifest"]["properties"]) == set(ResultManifest.model_fields)
    assert schemas["ActionBatch"]["properties"]["actions"]["maxItems"] == BATCH_MAX
    assert schemas["DigestBatch"]["properties"]["digests"]["maxItems"] == BATCH_MAX


# R2
def test_responses_are_api_models(svc: Service, backing: InMemoryMetadataStore) -> None:
    build = backing.create_build(make_build())
    r = svc.client.get(f"{B}/{build}", auth=ALICE)
    assert r.status_code == 200
    assert BuildView.model_validate_json(r.content) == backing.get_build(build)


# R2
@pytest.mark.parametrize("route", ["actions", "touch", "lookup"])
def test_batch_limits(svc: Service, backing: InMemoryMetadataStore, route: str) -> None:
    build = backing.create_build(make_build())

    def post(n: int) -> int:
        if route == "actions":
            rows = [
                ActionRow(action_id=f"a{i}", step="lint").model_dump(mode="json") for i in range(n)
            ]
            url, body = f"{B}/{build}/actions", json.dumps({"actions": rows})
        else:
            digests = [str(dg(str(i))) for i in range(n)]
            path = "touch" if route == "touch" else "cache/lookup"
            url = f"{API_PREFIX}/domains/test/{path}"
            body = json.dumps({"digests": digests})
        r = svc.client.post(url, content=body, auth=ALICE)
        return int(r.status_code)

    assert post(BATCH_MAX + 1) == 422
    assert post(BATCH_MAX) in (200, 204)
    if route == "actions":
        assert backing.get_build(build).action_counts == {"queued": BATCH_MAX}


# R2: the client splits big calls into batches of at most BATCH_MAX.
def test_client_batches(
    svc: Service, backing: InMemoryMetadataStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = svc.store()
    build = store.create_build(make_build())
    sizes: list[int] = []
    original = backing.add_actions

    def spy(b: BuildId, rows: Sequence[ActionRow]) -> None:
        sizes.append(len(rows))
        original(b, rows)

    monkeypatch.setattr(backing, "add_actions", spy)
    store.add_actions(build, [ActionRow(action_id=f"s{i:05}", step="sim") for i in range(1_234)])
    assert sizes == [500, 500, 234]
    assert backing.get_build(build).action_counts == {"queued": 1_234}
    # Duplicate ids anywhere in the sequence are refused before anything is sent.
    with pytest.raises(MetadataError, match="s00001"):
        store.add_actions(build, [ActionRow(action_id="s00001", step="sim")] * 2)
    assert sizes == [500, 500, 234]

    key = dg("k")
    store.cache_put("test", key, make_result(key))
    keys = [key, *(dg(f"miss{i}") for i in range(700))]
    assert store.cache_get_many("test", keys) == {key: make_result(key)}


def test_request_validation_is_422_with_message(svc: Service) -> None:
    r = svc.client.post(B, content=b'{"uuid": "nope"}', auth=ALICE)
    assert r.status_code == 422
    assert "uuid" in r.json()["detail"]
    r = svc.client.post(B, content=b"{not json", auth=ALICE)
    assert r.status_code == 422


def test_store_errors_are_409_with_message(svc: Service) -> None:
    build = svc.store().create_build(make_build())
    svc.store().finish_build(build, "passed")
    r = svc.client.post(f"{B}/{build}/finish", json={"status": "failed"}, auth=ALICE)
    assert r.status_code == 409
    assert "already finished" in r.json()["detail"]
    # P2-01: an unknown build id is 404, like a build of another domain (nothing is revealed).
    r = svc.client.get(f"{B}/424242", auth=ALICE)
    assert r.status_code == 404
    assert "424242" in r.json()["detail"]


# --- R4: ownership --------------------------------------------------------------------------------

Write = Callable[[HttpMetadataStore, BuildId], object]

WRITES: dict[str, Write] = {
    "add_actions": lambda s, b: s.add_actions(b, [ActionRow(action_id="new", step="lint")]),
    "set_action_state": lambda s, b: s.set_action_state(b, "a", "running"),
    "record_result": lambda s, b: s.record_result(b, "a", make_result(dg("k"))),
    "finish_build": lambda s, b: s.finish_build(b, "passed"),
    "emit": lambda s, b: s.emit(
        Event(ts=FakeClock().now(), build=b, type="build_started", data={})
    ),
}


def _principal_store(svc: Service, who: str) -> HttpMetadataStore:
    if who == "alice":
        return svc.store(FakeMunge(uid=1000))
    if who == "bob":
        return svc.store(FakeMunge(uid=1001))
    tokens: dict[str, tuple[TokenKind, str | None]] = {
        "ci-same-team": ("ci", "demo"),
        "ci-other-team": ("ci", "other"),
        "service-token": ("service", None),
    }
    kind, team = tokens[who]
    return svc.store(BearerCredential(svc.tokens.add(f"{who}-acct", kind=kind, team=team)))


# R4
@pytest.mark.parametrize("write", sorted(WRITES))
@pytest.mark.parametrize(
    ("who", "allowed"),
    [
        ("alice", True),
        ("bob", False),
        ("ci-same-team", True),
        ("ci-other-team", False),
        ("service-token", False),
    ],
)
def test_ownership_enforced(
    svc: Service, backing: InMemoryMetadataStore, write: str, who: str, allowed: bool
) -> None:
    build = backing.create_build(make_build(user="alice", project="demo"))
    backing.add_actions(build, [ActionRow(action_id="a", step="lint")])
    before = (backing.get_build(build), backing.list_actions(build))
    store = _principal_store(svc, who)
    if allowed:
        WRITES[write](store, build)
        return
    with pytest.raises(MetadataError, match="not allowed") as err:
        WRITES[write](store, build)
    assert f"build {build}" in str(err.value)
    assert (backing.get_build(build), backing.list_actions(build)) == before
    assert store.get_build(build).id == build  # reading stays allowed


# R4
@pytest.mark.parametrize(
    ("who", "user", "allowed"),
    [
        ("alice", "alice", True),
        ("alice", "bob", False),
        ("ci-same-team", "ci-same-team-acct", True),
        ("ci-same-team", "alice", True),  # CI token acting for its team's project
        ("ci-other-team", "alice", False),
        ("service-token", "service-token-acct", False),
    ],
)
def test_ownership_on_create_build(svc: Service, who: str, user: str, allowed: bool) -> None:
    store = _principal_store(svc, who)
    spec = make_build(user=user, project="demo")
    if allowed:
        assert store.get_build(store.create_build(spec)).user_name == user
    else:
        with pytest.raises(MetadataError, match="not allowed"):
            store.create_build(spec)


# R4: one `authorize()` hook sees every route (P2-01 adds domain membership there).
def test_authorize_called_everywhere(backing: InMemoryMetadataStore) -> None:
    calls: list[tuple[str, Operation, str | None, bool]] = []

    def spy(
        principal: Principal, op: Operation, *, domain: str | None, owner: BuildOwner | None
    ) -> None:
        calls.append((principal.name, op, domain, owner is not None))

    svc = make_service(backing)
    svc.deps.authorize = spy
    s = svc.store()
    key = dg("k")
    build = s.create_build(make_build())
    s.add_actions(build, [ActionRow(action_id="a", step="lint")])
    s.set_action_state(build, "a", "running")
    s.record_result(build, "a", make_result(key))
    s.get_result(build, "a")
    s.list_actions(build)
    s.get_build(build)
    s.cache_put("test", key, make_result(key))
    s.cache_get("test", key)
    s.cache_get_many("test", [key])
    s.cache_replace_failed("test", key, make_result(key))
    s.touch("test", [key])
    s.resolve_output("test", key)
    s.emit(Event(ts=FakeClock().now(), build=build, type="build_started", data={}))
    s.finish_build(build, "passed")
    ops = [(op, owned) for _, op, _, owned in calls]
    assert [c[0] for c in calls] == ["alice"] * 15
    assert ops.count(("build_write", True)) == 6  # create, add, state, result, emit, finish
    assert ops.count(("read", True)) == 3  # get_result, list_actions, get_build
    assert ops.count(("cache_write", False)) == 3  # put, replace_failed, touch
    assert ops.count(("read", False)) == 3  # cache_get, cache_get_many, resolve_output
    assert {d for _, _, d, _ in calls} == {"test"}  # every call names the domain (P2-01)


# --- R6: probes -----------------------------------------------------------------------------------


# R6
def test_health_ready(svc: Service) -> None:
    assert svc.client.get("/healthz").json() == {"status": "ok"}
    r = svc.client.get("/readyz")
    assert (r.status_code, r.json()) == (200, {"status": "ready"})
    svc.ready.reason = "database at head 0003, expected 0004: run the migrations"
    r = svc.client.get("/readyz")
    assert r.status_code == 503
    assert r.json() == {"status": "not ready", "detail": svc.ready.reason}
    assert svc.client.get("/healthz").status_code == 200  # the process itself is fine


# R5 (server side): a database outage is 503, which the client may retry; other store errors 409.
def test_database_outage_is_503(svc: Service, backing: InMemoryMetadataStore) -> None:
    class Down(Exception):
        pass

    def broken(*args: object) -> None:
        raise MetadataError("cache_get: metadata database error") from ConnectionError("down")

    backing.cache_get = broken  # type: ignore[method-assign,assignment]
    r = svc.client.get(f"{API_PREFIX}/domains/test/cache/{dg('k')}", auth=ALICE)
    assert r.status_code == 503
    assert r.json()["detail"] == "metadata database unavailable; retry later"


def test_unexpected_errors_are_500_without_internals(
    svc: Service, backing: InMemoryMetadataStore
) -> None:
    def bug(*args: object) -> None:
        raise RuntimeError("secret internals")

    backing.cache_get = bug  # type: ignore[method-assign,assignment]
    r = svc.client.get(f"{API_PREFIX}/domains/test/cache/{dg('k')}", auth=ALICE)
    assert r.status_code == 500
    assert "secret" not in r.text
    assert json.loads(r.text)["detail"]


def test_state_fields_cannot_shadow_arguments(svc: Service, backing: InMemoryMetadataStore) -> None:
    build = backing.create_build(make_build())
    backing.add_actions(build, [ActionRow(action_id="a", step="lint")])
    body = json.dumps({"state": "running", "fields": {"build": 3}})
    r = svc.client.post(f"{B}/{build}/actions/a/state", content=body, auth=ALICE)
    assert r.status_code == 409
    assert "build" in r.json()["detail"]
    assert backing.list_actions(build)[0].state == "queued"


def test_event_must_match_path_build(svc: Service, backing: InMemoryMetadataStore) -> None:
    build = backing.create_build(make_build())
    event = Event(ts=FakeClock().now(), build=BuildId(build + 1), type="plan_ready", data={})
    r = svc.client.post(f"{B}/{build}/events", content=event.model_dump_json(), auth=ALICE)
    assert r.status_code == 422
    assert f"not {build}" in r.json()["detail"]


def test_app_from_config(tmp_path: Path) -> None:
    from ebs.meta.pg import PgMetadataStore
    from ebs.meta.service.app import app_from_config

    config = tmp_path / "config.toml"
    # P2-01: a deployed service serves explicitly listed, configured domains.
    domains = '[[domains]]\nname = "test"\nunix_group = "test-nda"\n'
    config.write_text(
        '[metadata]\nurl = "postgresql+psycopg://ebs@db.example.invalid/ebs"\n'
        '[meta_service]\nmunge = false\ndomains = ["test"]\n' + domains
    )
    env = {"EBS_CONFIG": str(config), "HOME": str(tmp_path)}
    app = app_from_config(env)
    store = app.state.deps.store
    assert isinstance(store, PgMetadataStore)
    store.close()
    config.write_text(
        '[metadata]\nurl = "postgresql+psycopg://ebs@db.example.invalid/ebs"\n'
        '[meta_service]\naudience = "ebs-meta.example"\ntoolchain_admins = ["tcadmin"]\n'
        'domains = ["test"]\n' + domains
    )
    app = app_from_config(env)
    deps = app.state.deps
    assert deps.authorize.base.toolchain_admins == {"tcadmin"}
    assert deps.authorize.served == {"test"}
    deps.store.close()
    for bad, match in (
        ("[meta_service]\nmunge = false\nbearer = false\n", "no authentication"),
        ("[meta_service]\nmunge = true\n", "audience"),  # D1: MUNGE needs the binding
        ("[meta_service]\ncolour = 1\n", r"\[meta_service\]"),
        ("[meta_service]\nmunge = false\n", r"\[meta_service\].domains must list"),
        ('[meta_service]\nmunge = false\ndomains = ["other"]\n' + domains, "not configured"),
    ):
        config.write_text(
            '[metadata]\nurl = "postgresql+psycopg://ebs@db.example.invalid/ebs"\n' + bad
        )
        with pytest.raises(ConfigError, match=match):
            app_from_config(env)


# P2-06 R3: with [cas].root the service reads logs; with dashboard_dir it serves the dashboard.
def test_app_from_config_dashboard(tmp_path: Path) -> None:
    from ebs.meta.service.app import app_from_config
    from tests.helpers.meta_http import TestClient  # type: ignore[attr-defined]

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>EBS</title>")
    config = tmp_path / "config.toml"
    base = (
        '[metadata]\nurl = "postgresql+psycopg://ebs@db.example.invalid/ebs"\n'
        '[[domains]]\nname = "test"\nunix_group = "test-nda"\n'
        '[meta_service]\nmunge = false\ndomains = ["test"]\n'
    )
    config.write_text(base + f'dashboard_dir = "{dist}"\n[cas]\nroot = "{tmp_path / "cas"}"\n')
    env = {"EBS_CONFIG": str(config), "HOME": str(tmp_path)}
    app = app_from_config(env)
    try:
        assert app.state.deps.logs is not None
        client = TestClient(app, base_url=BASE_URL)
        assert client.get("/ui/").text == "<!doctype html><title>EBS</title>"
        home = client.get("/", follow_redirects=False)
        assert (home.status_code, home.headers["location"]) == (307, "/ui/")
    finally:
        app.state.deps.store.close()
    config.write_text(base)
    app = app_from_config(env)
    assert app.state.deps.logs is None  # no [cas].root: no logs
    app.state.deps.store.close()
    config.write_text(base + f'dashboard_dir = "{tmp_path}"\n')
    with pytest.raises(ConfigError, match=r"no index\.html"):
        app_from_config(env)


# R1 (review): action ids that look like URL syntax survive the path, as in the other stores.
@pytest.mark.parametrize("action_id", [".", "..", "a/b", "x/result", "a b?c#d", "sim[t=a,s=1]"])
def test_awkward_action_ids(svc: Service, action_id: str) -> None:
    store = svc.store()
    build = store.create_build(make_build())
    store.add_actions(build, [ActionRow(action_id=action_id, step="lint")])
    store.set_action_state(build, action_id, "running")
    store.record_result(build, action_id, make_result(dg("k")))
    assert store.get_result(build, action_id) == make_result(dg("k"))
    assert [a.action_id for a in store.list_actions(build)] == [action_id]
    assert store.list_actions(build)[0].state == "running"


# --- toolchain registry over HTTP (P1-07 methods) -------------------------------------------------


def _toolchain(registered_by: str = "alice") -> ToolchainCreate:
    from ebs.toolchain.model import toolchain_id

    env = {"PATH": "/opt/eda/bin:/usr/bin"}
    fingerprint = dg("fp")
    return ToolchainCreate(
        id=toolchain_id("questa/2025.2", "2025.2", fingerprint, env),
        name="questa",
        module="questa/2025.2",
        version="2025.2",
        fingerprint=fingerprint,
        env=env,
        install_roots=["/opt/eda"],
        registered_by=registered_by,
    )


# R4: registering a toolchain makes it current for every later build, so only toolchain admins
# (users listed in [meta_service].toolchain_admins) and service tokens may.
@pytest.mark.parametrize(
    ("who", "allowed"),
    [
        ("alice", True),  # listed admin
        ("bob", False),
        ("ci-same-team", False),
        ("service-token", True),
    ],
)
def test_toolchain_registration_restricted(
    backing: InMemoryMetadataStore, who: str, allowed: bool
) -> None:
    svc = make_service(backing, toolchain_admins=("alice",))
    store = _principal_store(svc, who)
    spec = _toolchain()
    if allowed:
        assert store.register_toolchain(spec, b"\x00\xffmanifest").inserted is True
        assert backing.toolchain_manifest(spec.id) == b"\x00\xffmanifest"
    else:
        with pytest.raises(MetadataError, match="not allowed to register toolchains"):
            store.register_toolchain(spec, b"m")
        assert backing.list_toolchains() == []
        backing.register_toolchain(spec, b"m")
    # Reading and touching stay open to every authenticated principal.
    assert store.current_toolchain("questa/2025.2") == backing.get_toolchain(spec.id)
    store.touch_toolchain(spec.id)
    assert store.get_toolchain(dg("unknown")) is None
    assert store.toolchain_manifest(dg("unknown")) is None


def test_toolchain_manifest_must_be_base64(svc: Service) -> None:
    body = json.dumps({"spec": _toolchain().model_dump(mode="json"), "manifest_b64": "%%%"})
    admin = make_service(InMemoryMetadataStore(), toolchain_admins=("alice",))
    r = admin.client.post(f"{API_PREFIX}/toolchains", content=body, auth=ALICE)
    assert r.status_code == 409
    assert "base64" in r.json()["detail"]
