"""The I20 matrix (P2-01 R2), run on PostgreSQL (tests/integration/test_authz_matrix.py) and in
memory (tests/unit/meta/service/test_authz_matrix_memory.py).

I20 matrix (P2-01 R2): through every domain-scoped API route, a principal outside a domain
gets 403 or 404, never data, and a cache lookup is a plain miss; members get through.

Principals: alice {A}, bob {B}, carol {A, B}, dave {} (MUNGE users, NSS groups faked) and a CI
token scoped to A by `[meta_service].token_domains`. The objects live in domain A on PostgreSQL;
each principal owns its own builds there, so only the domain decides. `test_matrix_covers_
every_route` fails if a route is added without a row here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from ebs.core.clock import FakeClock
from ebs.core.digest import hash_bytes
from ebs.domains import Domains
from ebs.meta.api import (
    API_PREFIX,
    PROVENANCE_DEPTH_MAX,
    ActionRow,
    BlobRef,
    BuildId,
    Event,
    LeaseId,
    ReleaseCreate,
    ReleaseImport,
    ReleaseOutput,
    ReleaseView,
)
from ebs.meta.client import BearerCredential, HttpMetadataStore, MetadataHttpError
from ebs.meta.service.auth import Policy
from ebs.meta.service.authz import DomainPolicy
from ebs.meta.service.deps import ServiceBackend
from ebs.meta.service.events_stream import EventStreamConfig
from ebs.meta.service.logs import LogSource
from ebs.meta.service.tokens import InMemoryTokenStore
from tests.helpers.meta_http import FakeMunge, Service, make_service
from tests.helpers.release import Action, finish, out

A, B = "dom-a", "dom-b"
LIB = out(b"worklib")
KEY = hash_bytes(b"cache key in A")
LOG_ROOT = Path("/nonexistent/ebs-authz-matrix")  # the log route finds no file under it


GROUPS: dict[str, set[str]] = {"alice": {"grp-a"}, "bob": {"grp-b"}, "carol": {"grp-a", "grp-b"}}


class Groups:
    def groups_of(self, user: str) -> frozenset[str]:
        return frozenset(GROUPS.get(user, set()))


@dataclass
class Who:
    name: str
    user: str  # the builds' user_name
    domains: frozenset[str]
    client: HttpMetadataStore


@dataclass
class Mine:
    """The principal's own objects in domain A."""

    running: BuildId
    finished: BuildId
    release: ReleaseView
    lease: LeaseId


def _service(pg: ServiceBackend) -> tuple[Service, list[Who]]:
    tokens = InMemoryTokenStore()
    domains = Domains.from_config(
        {"domains": {A: {"unix_group": "grp-a"}, B: {"unix_group": "grp-b"}}}
    )
    policy = DomainPolicy(Policy(), domains, [A, B], Groups(), {"ci-a": [A]})
    svc = make_service(
        pg, tokens=tokens, authorize=policy, events=EventStreamConfig(max_stream_s=0.0),
        logs=LogSource(lambda _d: LOG_ROOT, lambda _d: LOG_ROOT),
    )  # fmt: skip
    ci = BearerCredential(tokens.add("ci-a", kind="ci", team="demo"))
    who = [
        Who("alice", "alice", frozenset({A}), svc.store(FakeMunge(uid=1000))),
        Who("bob", "bob", frozenset({B}), svc.store(FakeMunge(uid=1001))),
        Who("carol", "carol", frozenset({A, B}), svc.store(FakeMunge(uid=1002))),
        Who("dave", "dave", frozenset(), svc.store(FakeMunge(uid=1003))),
        Who("ci-a", "ci-bot", frozenset({A}), svc.store(ci)),
    ]
    return svc, who


def _mine(pg: ServiceBackend, who: Who) -> Mine:
    running = finish(pg, {}, domain=A, user=who.user, status=None)
    pg.add_actions(
        running, [ActionRow(action_id="a", step="a"), ActionRow(action_id="b", step="b")]
    )
    pg.set_action_state(running, "a", "running", key=KEY)  # record_result's target
    lease = pg.acquire_lease(A, running, [LIB.digest], ttl_s=600)
    done = finish(pg, {"compile": Action({"worklib": LIB})}, domain=A, user=who.user)
    release = pg.create_release(
        ReleaseCreate(
            domain=A, name="rtl/cpu", version=f"{who.name}-1", build=done,
            outputs={"compile.worklib": ReleaseOutput(
                action_id="compile", output="worklib", **LIB.model_dump())},
        ),
        by=who.user,
    )  # fmt: skip
    pg.set_channel(A, "rtl/cpu", f"ch-{who.name.replace('-', '')}", release.id, by=who.user)
    return Mine(running, done, release, lease)


Op = Callable[[HttpMetadataStore, Mine, Who], object]


def _release_spec(m: Mine, w: Who) -> ReleaseCreate:
    return ReleaseCreate(
        domain=A, name="rtl/cpu", version=f"{w.name}-2", build=m.finished,
        outputs={"compile.worklib": ReleaseOutput(
            action_id="compile", output="worklib", **LIB.model_dump())},
    )  # fmt: skip


# (route, op, domains needed). Reads that must look like a miss are in MISSES.
OPS: list[tuple[str, Op, frozenset[str]]] = [
    (
        "PUT /domains/{domain}/cache/{key}",
        lambda c, m, w: c.cache_put(A, KEY, _result(m, w)),
        frozenset({A}),
    ),
    (
        "POST /domains/{domain}/cache/{key}/replace-failed",
        lambda c, m, w: c.cache_replace_failed(A, KEY, _result(m, w)),
        frozenset({A}),
    ),
    ("POST /domains/{domain}/touch", lambda c, m, w: c.touch(A, [LIB.digest]), frozenset({A})),
    (
        "POST /domains/{domain}/blobs",
        lambda c, m, w: c.register_blobs(A, [BlobRef(digest=LIB.digest, size=7, kind="file")]),
        frozenset({A}),
    ),
    ("GET /builds/{build}", lambda c, m, w: c.get_build(m.finished), frozenset({A})),
    ("POST /builds", lambda c, m, w: c.create_build(_build(w)), frozenset({A})),
    (
        "POST /builds/{build}/actions",
        lambda c, m, w: c.add_actions(m.running, [ActionRow(action_id="new", step="new")]),
        frozenset({A}),
    ),
    ("GET /builds/{build}/actions", lambda c, m, w: c.list_actions(m.finished), frozenset({A})),
    # The event streams (P2-05 R2): the finished build's ends at once, the domain's after its
    # backlog (max_stream_s = 0). The store has no stream method: raw calls, same credentials.
    (
        "GET /builds/{build}/events",
        lambda c, m, w: _stream(c, f"/builds/{m.finished}/events"),
        frozenset({A}),
    ),
    (
        "GET /domains/{domain}/events",
        lambda c, m, w: _stream(c, f"/domains/{A}/events"),
        frozenset({A}),
    ),
    (
        "POST /builds/{build}/actions/{action_id}/state",
        lambda c, m, w: c.set_action_state(m.running, "b", "running"),
        frozenset({A}),
    ),
    (
        "PUT /builds/{build}/actions/{action_id}/result",
        lambda c, m, w: c.record_result(m.running, "a", _result(m, w)),
        frozenset({A}),
    ),
    (
        "GET /builds/{build}/actions/{action_id}/result",
        lambda c, m, w: c.get_result(m.finished, "compile"),
        frozenset({A}),
    ),
    (
        "POST /builds/{build}/events",
        lambda c, m, w: c.emit(
            Event(ts=FakeClock().now(), build=m.running, type="build_started", data={})
        ),
        frozenset({A}),
    ),
    (
        "POST /builds/{build}/leases",
        lambda c, m, w: c.acquire_lease(A, m.running, [LIB.digest], 600),
        frozenset({A}),
    ),
    (
        "POST /builds/{build}/leases/{lease}/renew",
        lambda c, m, w: c.renew_lease(m.running, m.lease, 600),
        frozenset({A}),
    ),
    ("PUT /builds/{build}/pin", lambda c, m, w: c.pin_build(m.finished, by=w.user), frozenset({A})),
    ("DELETE /builds/{build}/pin", lambda c, m, w: c.unpin_build(m.finished), frozenset({A})),
    (
        "POST /builds/{build}/release-imports",
        lambda c, m, w: c.record_release_imports(
            m.running, [ReleaseImport(alias="rtl", release=m.release.id)]
        ),
        frozenset({A}),
    ),
    (
        "GET /builds/{build}/release-imports",
        lambda c, m, w: c.list_release_imports(m.finished),
        frozenset({A}),
    ),
    (
        "POST /builds/{build}/finish",
        lambda c, m, w: c.finish_build(m.running, "passed"),
        frozenset({A}),
    ),
    (
        "POST /releases",
        lambda c, m, w: c.create_release(_release_spec(m, w), by=w.user),
        frozenset({A}),
    ),
    ("GET /releases", lambda c, m, w: _found(c.list_releases(A)), frozenset({A})),
    (
        "PUT /channels",
        lambda c, m, w: c.set_channel(A, "rtl/cpu", "stable", m.release.id, by=w.user),
        frozenset({A}),
    ),
    ("GET /channels", lambda c, m, w: c.get_channel(A, "rtl/cpu", "stable"), frozenset({A})),
    (
        "GET /channels/history",
        lambda c, m, w: c.channel_history(A, "rtl/cpu", "stable"),
        frozenset({A}),
    ),
    (
        "POST /domains/{domain}/gate-builds",
        lambda c, m, w: c.gate_builds(A, "gate", [LIB.id]),
        frozenset({A}),
    ),
    (  # P2-04 R4
        "POST /domains/{domain}/provenance/upstream",
        lambda c, m, w: _found_actions(
            c.provenance_upstream(A, ids=[LIB.id], keys=[], depth=PROVENANCE_DEPTH_MAX)
        ),
        frozenset({A}),
    ),
    (
        "POST /domains/{domain}/provenance/downstream",
        lambda c, m, w: c.provenance_downstream(A, ids=[LIB.id], keys=[], depth=1),
        frozenset({A}),
    ),
    # P2-06: the dashboard's reads (raw calls: the store client has no such methods).
    (
        "GET /domains/{domain}/builds",
        lambda c, m, w: _found(_get(c, f"/domains/{A}/builds")["builds"]),
        frozenset({A}),
    ),
    (
        "GET /domains/{domain}/stats",
        lambda c, m, w: _get(c, f"/domains/{A}/stats"),
        frozenset({A}),
    ),
    (
        "GET /builds/{build}/results",
        lambda c, m, w: _found(_get(c, f"/builds/{m.finished}/results")["results"]),
        frozenset({A}),
    ),
    (
        "GET /builds/{build}/actions/{action_id}/log",
        lambda c, m, w: _no_log_yet(c, f"/builds/{m.finished}/actions/compile/log"),
        frozenset({A}),
    ),
    (
        "GET /releases/{release}/exports",
        lambda c, m, w: c.list_exports(m.release.id),
        frozenset({A}),
    ),
    (
        "POST /releases/{release}/exports",
        lambda c, m, w: c.record_export(m.release.id, B, by=w.user),
        frozenset({A, B}),
    ),
    (
        "POST /releases/{release}/yank",
        lambda c, m, w: c.yank_release(m.release.id, by=w.user, reason="matrix"),
        frozenset({A}),
    ),
]

# Reads whose non-member answer must be the same as for a missing object.
MISSES: list[tuple[str, Op]] = [
    ("GET /domains/{domain}/cache/{key}", lambda c, m, w: c.cache_get(A, KEY)),
    ("POST /domains/{domain}/cache/lookup", lambda c, m, w: c.cache_get_many(A, [KEY]) or None),
    ("GET /domains/{domain}/outputs/{object_id}", lambda c, m, w: c.resolve_output(A, LIB.id)),
    ("GET /releases/{release}", lambda c, m, w: c.get_release_by_id(m.release.id)),
]

# Routes without a domain: global toolchain registry and the caller's own identity.
DOMAIN_FREE = {
    "GET /whoami", "POST /toolchains", "GET /toolchains", "GET /toolchains/current",
    "GET /toolchains/{toolchain_id}", "GET /toolchains/{toolchain_id}/manifest",
    "POST /toolchains/{toolchain_id}/touch",
}  # fmt: skip


def _found_actions(graph: Any) -> Any:
    assert graph.actions, "a member walks the domain's provenance"
    return graph


def _stream(c: HttpMetadataStore, path: str) -> str:
    text = c._call("GET", path, f"GET {path}", idempotent=True).text
    assert text.startswith("retry:"), text
    return text


def _get(c: HttpMetadataStore, path: str) -> Any:
    return c._call("GET", path, f"GET {path}", idempotent=True).json()


def _no_log_yet(c: HttpMetadataStore, path: str) -> None:
    """A member reaches the log reader, which finds no file (`LOG_ROOT` does not exist); a
    non-member gets the 404 of a missing build instead (run_matrix checks the status)."""
    try:
        c._call("GET", path, f"GET {path}", idempotent=True)
    except MetadataHttpError as exc:
        if exc.status != 404 or "no log of action" not in str(exc):
            raise
        return
    raise AssertionError(f"{path}: a log was served from a root that does not exist")


def _found(value: list[Any]) -> list[Any]:
    assert value, "a member sees the domain's releases"
    return value


def _build(w: Who) -> Any:
    from uuid import uuid4

    from ebs.meta.api import BuildCreate

    return BuildCreate(
        uuid=uuid4(), domain=A, project="demo", plan_digest=hash_bytes(b"plan"), flow_repo="",
        flow_commit="", flow_dirty=False, user_name=w.user, cache_mode="write",
    )  # fmt: skip


def _result(m: Mine, w: Who) -> Any:
    from ebs.meta.api import ResourceUsage, ResultManifest, RunnerInfo

    return ResultManifest(
        action_key=KEY, status="passed", exit_code=0,
        inputs={}, outputs={}, log=None, summary={},
        resources=ResourceUsage(max_rss_kb=0, cpu_s=0, wall_s=0),
        runner=RunnerInfo(version="0", host="node-a"),
    )  # fmt: skip


def covers_every_route(store: ServiceBackend) -> None:
    svc, _ = _service(store)
    paths = svc.client.get(f"{API_PREFIX}/openapi.json").json()["paths"]
    routes = {f"{m.upper()} {p.removeprefix(API_PREFIX)}" for p, ops in paths.items() for m in ops}
    covered = {r for r, _, _ in OPS} | {r for r, _ in MISSES} | DOMAIN_FREE
    assert routes == covered


def _expected_status(route: str, needed: frozenset[str], who: Who) -> int:
    """404 for an object named by id outside the caller's domains, else 403."""
    by_id = "{build}" in route or "{release}" in route
    return 404 if by_id and A not in who.domains else 403


def run_matrix(store: ServiceBackend) -> None:
    svc, everyone = _service(store)
    mine = {w.name: _mine(store, w) for w in everyone}
    store.cache_put(A, KEY, _result(mine["alice"], everyone[0]))
    outcomes: dict[str, dict[str, str]] = {}
    for w in everyone:
        m = mine[w.name]
        row = outcomes.setdefault(w.name, {})
        for route, op in MISSES:
            got = op(w.client, m, w)
            if A in w.domains:
                assert got, f"{w.name}: {route} must find the object"
                row[route] = "ok"
            else:
                assert not got, f"{w.name}: {route} revealed {got!r}"
                row[route] = "miss"
        for route, op, needed in OPS:
            if needed <= w.domains:
                op(w.client, m, w)  # members get through (the owner rules hold: own objects)
                row[route] = "ok"
                continue
            with pytest.raises(MetadataHttpError) as info:
                op(w.client, m, w)
            want = _expected_status(route, needed, w)
            assert info.value.status == want, (w.name, route, info.value.status, str(info.value))
            row[route] = "refused"
    # Nothing a non-member tried changed domain A.
    for name in ("bob", "dave"):
        m = mine[name]
        assert store.get_build(m.running).status == "running"
        assert store.get_build(m.finished).pinned is False
        assert store.get_release_by_id(m.release.id) == m.release
        assert store.list_actions(m.running)[-1].action_id == "b"
        assert store.get_release(A, "rtl/cpu", f"{name}-2") is None
    movers = [mv.moved_by for mv in store.channel_history(A, "rtl/cpu", "stable")]
    assert sorted(movers) == ["alice", "carol", "ci-a"]  # the authenticated callers
    assert outcomes["carol"]["POST /releases/{release}/exports"] == "ok"
    assert outcomes["alice"]["POST /releases/{release}/exports"] == "refused"  # not in B
    assert outcomes["ci-a"]["POST /domains/{domain}/touch"] == "ok"
    assert set(outcomes["dave"].values()) == {"miss", "refused"}
    assert set(outcomes["bob"].values()) == {"miss", "refused"}
    # A hidden object looks exactly like a missing one.
    dave = next(w for w in everyone if w.name == "dave")
    hidden_build, hidden_release = mine["alice"].running, mine["alice"].release.id
    for path, real, missing in [
        ("/builds/{}", hidden_build, 999_999),
        ("/releases/{}/exports", hidden_release, 999_999),
    ]:
        answers = []
        for ident in (real, missing):
            r = svc.client.get(f"{API_PREFIX}{path.format(ident)}", auth=FakeMunge(uid=1003))
            answers.append((r.status_code, r.json()["detail"].replace(str(ident), "N")))
        assert answers[0] == answers[1] == (404, answers[0][1]), (path, answers)
    assert dave.client.get_release_by_id(hidden_release) is None
    # An exported release is visible by id to members of its new domain only (bob is in B).
    bob = next(w for w in everyone if w.name == "bob")
    exported = store.get_release(B, "rtl/cpu", "carol-1")
    assert exported is not None
    assert bob.client.get_release_by_id(exported.id) == exported
    assert [e.exported for e in bob.client.list_exports(exported.id)] == [exported.id]
    # create_release by a non-member says nothing about the build id (existing or not).
    for build in (mine["bob"].finished, BuildId(999_999)):
        spec = _release_spec(mine["alice"], dave).model_copy(update={"build": build})
        with pytest.raises(MetadataHttpError) as info:
            dave.client.create_release(spec, by="dave")
        assert info.value.status == 403
