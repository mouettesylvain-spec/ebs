"""Cursor pagination of the list routes (P2-05 R3)."""

from __future__ import annotations

from typing import Any

import pytest

from ebs.core.clock import FakeClock
from ebs.core.digest import hash_bytes
from ebs.meta.api import (
    API_PREFIX,
    ActionRow,
    BuildId,
    ReleaseCreate,
    ReleaseImport,
    ReleaseOutput,
    ToolchainCreate,
)
from ebs.meta.memory import InMemoryMetadataStore
from ebs.meta.service.limits import LimitsConfig
from tests.helpers.meta_http import FakeMunge, Service, make_service
from tests.helpers.release import Action, finish, out

ALICE = FakeMunge()  # one credential source: a new one would replay serial 0

# Routes that answer one object (by id or name) in a list-shaped body: nothing to page.
SINGLE = {
    "GET /releases/{release}",
    "GET /toolchains/current",
    "GET /toolchains/{toolchain_id}",
    "GET /channels",
}
# Bodies with a list that are bounded by the request itself (a batch of ids), or aggregates
# (DomainStats: one row per step and per pending reason and license feature, P2-06).
BOUNDED = {"BuildList", "DomainStats"}


def _service(max_page_size: int = 1000) -> tuple[Service, InMemoryMetadataStore]:
    store = InMemoryMetadataStore(clock=FakeClock())
    svc = make_service(
        store, toolchain_admins=["alice"], limits=LimitsConfig(max_page_size=max_page_size)
    )
    return svc, store


def _build(store: InMemoryMetadataStore, n: int) -> BuildId:
    build = finish(store, {}, status=None)
    store.add_actions(build, [ActionRow(action_id=f"sim[t={i:02}]", step="sim") for i in range(n)])
    return build


def _page(svc: Service, path: str, **params: Any) -> dict[str, Any]:
    r = svc.client.get(f"{API_PREFIX}{path}", params=params, auth=ALICE)
    assert r.status_code == 200, r.text
    doc: dict[str, Any] = r.json()
    return doc


# R3: every list route takes `limit` and `cursor` and answers `next`; a new list route without
# them fails here.
def test_every_list_route_is_paginated() -> None:
    svc, _ = _service()
    schema = svc.client.get(f"{API_PREFIX}/openapi.json").json()
    models = schema["components"]["schemas"]
    paged = []
    for path, ops in schema["paths"].items():
        get = ops.get("get")
        if not get or "200" not in get["responses"]:
            continue
        content = get["responses"]["200"].get("content", {}).get("application/json", {})
        model = content.get("schema", {}).get("$ref", "").rsplit("/", 1)[-1]
        props = models.get(model, {}).get("properties", {})
        if model in BOUNDED or not any(p.get("type") == "array" for p in props.values()):
            continue
        route = f"GET {path.removeprefix(API_PREFIX)}"
        if route in SINGLE:
            continue
        assert "next" in props, f"{route}: {model} has no `next` cursor"
        names = {p["name"] for p in get.get("parameters", [])}
        assert {"limit", "cursor"} <= names, f"{route} is not paginated"
        paged.append(route)
    assert sorted(paged) == [
        "GET /builds/{build}/actions",
        "GET /builds/{build}/release-imports",
        "GET /builds/{build}/results",
        "GET /channels/history",
        "GET /domains/{domain}/builds",
        "GET /releases",
        "GET /releases/{release}/archives",
        "GET /releases/{release}/exports",
        "GET /releases/{release}/revalidations",
        "GET /toolchains",
    ]


# R3: pages follow each other without gaps or repeats, also when rows are added in between.
def test_pages_of_actions() -> None:
    svc, store = _service()
    build = _build(store, 7)
    path = f"/builds/{build}/actions"
    first = _page(svc, path, limit=3)
    assert [a["action_id"] for a in first["actions"]] == ["sim[t=00]", "sim[t=01]", "sim[t=02]"]
    store.add_actions(
        build, [ActionRow(action_id="aaa", step="aaa"), ActionRow(action_id="zzz", step="zzz")]
    )
    second = _page(svc, path, limit=3, cursor=first["next"])
    third = _page(svc, path, limit=3, cursor=second["next"])
    ids = [a["action_id"] for p in (first, second, third) for a in p["actions"]]
    assert ids == [f"sim[t={i:02}]" for i in range(7)] + ["zzz"]  # "aaa" sorts before the cursor
    assert third["next"] is None
    whole = _page(svc, path)
    assert whole["next"] is None
    assert len(whole["actions"]) == 9


# R3: the server's maximum page size is the default and the limit; larger asks are refused.
def test_max_page_size_enforced() -> None:
    svc, store = _service(max_page_size=2)
    build = _build(store, 5)
    path = f"{API_PREFIX}/builds/{build}/actions"
    default = _page(svc, path.removeprefix(API_PREFIX))
    assert len(default["actions"]) == 2
    assert default["next"]
    r = svc.client.get(path, params={"limit": 3}, auth=ALICE)
    assert r.status_code == 422
    assert "at most 2" in r.json()["detail"]
    assert svc.client.get(path, params={"limit": 0}, auth=ALICE).status_code == 422


# R3: a cursor is opaque and bound to its query: a forged or foreign one is refused.
@pytest.mark.parametrize("cursor", ["garbage", "eyJrIjpbXX0", ""])
def test_bad_cursor(cursor: str) -> None:
    svc, store = _service()
    build = _build(store, 3)
    r = svc.client.get(
        f"{API_PREFIX}/builds/{build}/actions", params={"cursor": cursor}, auth=ALICE
    )
    assert r.status_code == 422, r.text
    assert "cursor" in r.json()["detail"]


def test_cursor_of_another_query() -> None:
    svc, store = _service()
    one, two = _build(store, 3), _build(store, 3)
    page = _page(svc, f"/builds/{one}/actions", limit=1)
    for path, params in (
        (f"/builds/{two}/actions", {}),
        (f"/builds/{one}/actions", {"state": "queued"}),
        ("/releases", {"domain": "test"}),
    ):
        r = svc.client.get(
            f"{API_PREFIX}{path}", params={"cursor": page["next"], **params}, auth=ALICE
        )
        assert r.status_code == 422, (path, r.text)
        assert "another query" in r.json()["detail"]


def _toolchain(module: str, version: str) -> ToolchainCreate:
    from ebs.toolchain.model import toolchain_id

    fp = hash_bytes(module.encode())
    env = {"PATH": "/opt/eda/bin"}
    return ToolchainCreate(
        id=toolchain_id(module, version, fp, env), name=module.split("/")[0], module=module,
        version=version, fingerprint=fp, env=env, install_roots=["/opt/eda"],
        registered_by="alice",
    )  # fmt: skip


# R3: the HTTP store follows `next` until the last page: callers still get whole lists.
def test_client_reads_every_page() -> None:
    svc, store = _service(max_page_size=2)
    build = _build(store, 5)
    client = svc.store()
    assert [a.action_id for a in client.list_actions(build)] == [
        a.action_id for a in store.list_actions(build)
    ]
    assert len(client.list_actions(build, state="queued")) == 5
    lib = out(b"lib")
    builds = [finish(store, {"c": Action({"lib": lib})}) for _ in range(3)]
    releases = []
    for i, b in enumerate(builds):
        spec = ReleaseCreate(
            domain="test", name="team/cpu", version=f"1.{i}", build=b, notes="",
            outputs={"c.lib": ReleaseOutput(action_id="c", output="lib", **lib.model_dump())},
        )  # fmt: skip
        releases.append(store.create_release(spec, by="alice"))
    assert [r.version for r in client.list_releases("test")] == ["1.0", "1.1", "1.2"]
    for r in releases:
        store.set_channel("test", "team/cpu", "stable", r.id, by="alice")
    history = client.channel_history("test", "team/cpu", "stable")
    assert [m.version for m in history] == ["1.0", "1.1", "1.2"]
    for i in range(3):
        store.register_toolchain(_toolchain(f"tool{i}/1", "1"), b"m")
    assert [t.module for t in client.list_toolchains()] == ["tool0/1", "tool1/1", "tool2/1"]
    for target in ("d1", "d2", "d3"):
        store.record_export(releases[0].id, target, by="alice")
    assert len(client.list_exports(releases[0].id)) == 3
    importer = finish(store, {}, status=None)
    rows = [ReleaseImport(alias=a, release=releases[0].id) for a in ("c", "a", "b")]
    store.record_release_imports(importer, rows)
    assert [r.alias for r in client.list_release_imports(importer)] == ["a", "b", "c"]


# R3: a server that keeps answering the same cursor is an error, not an endless loop.
def test_client_stops_on_a_repeated_cursor() -> None:
    import httpx

    from ebs.core.errors import MetadataError
    from ebs.meta.client import HttpMetadataStore

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"actions": [], "next": "same"})

    client = HttpMetadataStore(
        "http://meta.test", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(MetadataError, match="same page cursor twice"):
        client.list_actions(BuildId(1))
