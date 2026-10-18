"""Dashboard routes of the metadata service (P2-06): builds and stats of a domain, action logs
and the caller's domains. Domain scoping of each route is in the I20 matrix (authz_matrix.py)."""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pytest

from ebs.cas.fs import FsCAS
from ebs.core.clock import FakeClock
from ebs.core.digest import hash_bytes
from ebs.core.errors import MetadataError
from ebs.domains import Domains
from ebs.meta.api import API_PREFIX, LOG_CHUNK_MAX, ActionRow, BuildId
from ebs.meta.memory import InMemoryMetadataStore
from ebs.meta.service.auth import Policy
from ebs.meta.service.authz import DomainPolicy
from ebs.meta.service.logs import LogSource
from ebs.runner.collect import debug_action_dir
from tests.helpers.dashboard import DOMAIN, new_build, result, run_action
from tests.helpers.meta_http import FakeMunge, Service, make_service

ALICE = FakeMunge()  # one credential source: a new one would replay serial 0


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(clock: FakeClock) -> InMemoryMetadataStore:
    return InMemoryMetadataStore(clock=clock)


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    cas, debug = tmp_path / "cas", tmp_path / "debug"
    (debug / DOMAIN).mkdir(parents=True)
    return cas, debug


@pytest.fixture
def svc(store: InMemoryMetadataStore, roots: tuple[Path, Path]) -> Service:
    cas, debug = roots
    return make_service(store, logs=LogSource(lambda _d: cas, lambda _d: debug))


def _get(svc: Service, path: str, **params: object) -> object:
    r = svc.client.get(f"{API_PREFIX}{path}", params=params, auth=ALICE)
    assert r.status_code == 200, r.text
    return r.json()


# R1: the build list route filters and pages newest first, with the action counts.
def test_list_builds_route(svc: Service, store: InMemoryMetadataStore, clock: FakeClock) -> None:
    t0 = clock.now()
    built = []
    for i in range(5):
        built.append(new_build(store, user="alice" if i % 2 else "bob"))
        clock.advance(60)
    run_action(store, built[4], "lint")
    doc = _get(svc, f"/domains/{DOMAIN}/builds", limit=2)
    assert isinstance(doc, dict)
    assert [b["id"] for b in doc["builds"]] == [built[4], built[3]]
    assert doc["builds"][0]["action_counts"] == {"done": 1}
    second = _get(svc, f"/domains/{DOMAIN}/builds", limit=2, cursor=doc["next"])
    assert isinstance(second, dict)
    assert [b["id"] for b in second["builds"]] == [built[2], built[1]]
    alice = _get(svc, f"/domains/{DOMAIN}/builds", user="alice", status="running")
    assert isinstance(alice, dict)
    assert [b["id"] for b in alice["builds"]] == [built[3], built[1]]
    assert alice["next"] is None
    since = (t0 - timedelta(seconds=1)).isoformat()
    window = _get(svc, f"/domains/{DOMAIN}/builds", since=since, until=t0.isoformat())
    assert window == {"builds": [], "next": None}  # every build was created at t0 or later
    assert _get(svc, f"/domains/{DOMAIN}/builds", project="nope") == {"builds": [], "next": None}


# R1: a cursor of another filter, a naive date or an unknown status is a 422.
def test_list_builds_route_refuses_bad_queries(svc: Service, store: InMemoryMetadataStore) -> None:
    for _ in range(3):
        new_build(store)
    doc = _get(svc, f"/domains/{DOMAIN}/builds", limit=1)
    assert isinstance(doc, dict)
    for params in (
        {"limit": 1, "cursor": doc["next"], "user": "bob"},
        {"since": "2026-10-01T00:00:00"},
        {"status": "exploded"},
    ):
        r = svc.client.get(f"{API_PREFIX}/domains/{DOMAIN}/builds", params=params, auth=ALICE)
        assert r.status_code == 422, (params, r.text)


# R4: the stats route aggregates the window it is given, 30 days by default.
def test_stats_route(svc: Service, store: InMemoryMetadataStore, clock: FakeClock) -> None:
    b = new_build(store)
    run_action(store, b, "compile", cached=True, cpu_s=3600, wall_s=4000)
    since, until = clock.now() - timedelta(hours=1), clock.now() + timedelta(hours=1)
    doc = _get(svc, f"/domains/{DOMAIN}/stats", since=since.isoformat(), until=until.isoformat())
    assert isinstance(doc, dict)
    assert doc["builds"] == 1
    assert doc["steps"] == [
        {"step": "compile", "cached": 1, "ran": 0, "cached_cpu_s": 3600, "cached_wall_s": 4000,
         "ran_cpu_s": 0}
    ]  # fmt: skip
    default = _get(svc, f"/domains/{DOMAIN}/stats", until=until.isoformat())
    assert isinstance(default, dict)
    assert default["since"].startswith((until - timedelta(days=30)).date().isoformat())
    clocked = make_service(store, clock=clock)  # no window: the 30 days before the clock
    now = clocked.client.get(f"{API_PREFIX}/domains/{DOMAIN}/stats", auth=FakeMunge()).json()
    assert (now["since"], now["until"]) == (
        (clock.now() - timedelta(days=30)).isoformat().replace("+00:00", "Z"),
        clock.now().isoformat().replace("+00:00", "Z"),
    )
    assert now["builds"] == 0  # the build was created at the clock's now: outside [since, now)
    for params in (
        {"since": until.isoformat(), "until": since.isoformat()},
        {"until": "x"},
        {"until": "2026-10-01T00:00:00"},  # no time zone
    ):
        r = svc.client.get(f"{API_PREFIX}/domains/{DOMAIN}/stats", params=params, auth=ALICE)
        assert r.status_code == 422, (params, r.text)


def _log_build(store: InMemoryMetadataStore) -> BuildId:
    b = new_build(store)
    store.add_actions(b, [ActionRow(action_id="sim[t1/s=1]", step="sim")])
    return b


# R3: a finished action's log is its CAS blob, by byte range, complete.
def test_log_route_serves_the_stored_log(
    svc: Service, store: InMemoryMetadataStore, roots: tuple[Path, Path]
) -> None:
    b = _log_build(store)
    text = b"".join(f"line {i}\n".encode() for i in range(1000)) + b"** Error: boom\n"
    digest = FsCAS(roots[0], DOMAIN).put_bytes(text)
    manifest = result("k", cpu_s=1, wall_s=1).model_copy(update={"log": digest})
    store.set_action_state(b, "sim[t1/s=1]", "running", key=manifest.action_key)
    store.record_result(b, "sim[t1/s=1]", manifest)
    # A live log left behind is not used once the stored one exists.
    live = debug_action_dir(roots[1], DOMAIN, str(b), "sim[t1/s=1]") / "live.log"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"stale live log")
    url = f"{API_PREFIX}/builds/{b}/actions/sim[t1/s=1]/log"
    r = svc.client.get(url, auth=ALICE)
    assert r.status_code == 200, r.text
    assert r.content == text
    assert r.headers["x-ebs-log-source"] == "cas"
    assert r.headers["x-ebs-log-size"] == str(len(text))
    assert r.headers["x-ebs-log-complete"] == "true"
    assert r.headers["content-type"].startswith("text/plain")
    assert r.headers["x-content-type-options"] == "nosniff"  # a log with HTML is not rendered
    assert r.headers["cache-control"] == "no-store"
    assert svc.client.get(url, params={"offset": -1}, auth=ALICE).status_code == 422
    part = svc.client.get(url, params={"offset": 5, "limit": 6}, auth=ALICE)
    assert part.content == text[5:11]
    assert part.headers["x-ebs-log-size"] == str(len(text))
    past = svc.client.get(url, params={"offset": len(text) + 10}, auth=ALICE)
    assert (past.status_code, past.content) == (200, b"")
    too_big = svc.client.get(url, params={"limit": LOG_CHUNK_MAX + 1}, auth=ALICE)
    assert too_big.status_code == 422


# R3: a running action's log is the runner's live log, which may still grow.
def test_log_route_serves_the_live_log(
    svc: Service, store: InMemoryMetadataStore, roots: tuple[Path, Path]
) -> None:
    b = _log_build(store)
    url = f"{API_PREFIX}/builds/{b}/actions/sim[t1/s=1]/log"
    assert svc.client.get(url, auth=ALICE).status_code == 404  # not started: no log yet
    live = debug_action_dir(roots[1], DOMAIN, str(b), "sim[t1/s=1]") / "live.log"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"compiling\n")
    r = svc.client.get(url, auth=ALICE)
    assert (r.status_code, r.content) == (200, b"compiling\n")
    assert r.headers["x-ebs-log-source"] == "live"
    assert r.headers["x-ebs-log-complete"] == "false"
    with live.open("ab") as f:
        f.write(b"running\n")
    tail = svc.client.get(url, params={"offset": 10}, auth=ALICE)
    assert tail.content == b"running\n"
    assert tail.headers["x-ebs-log-size"] == "18"


# R3: an unknown action or a service without log roots answers 404 with the reason.
def test_log_route_not_found(store: InMemoryMetadataStore, svc: Service) -> None:
    b = _log_build(store)
    r = svc.client.get(f"{API_PREFIX}/builds/{b}/actions/nope/log", auth=ALICE)
    assert r.status_code == 404
    assert "no action 'nope'" in r.json()["detail"]
    bare = make_service(store)
    r = bare.client.get(f"{API_PREFIX}/builds/{b}/actions/sim[t1/s=1]/log", auth=FakeMunge())
    assert r.status_code == 404
    assert "[cas].root" in r.json()["detail"]


# R7: whoami lists the caller's domains, so the dashboard offers only those.
def test_whoami_lists_member_domains(store: InMemoryMetadataStore) -> None:
    class Groups:
        def groups_of(self, user: str) -> frozenset[str]:
            return frozenset({"grp-a"} if user == "alice" else set())

    domains = Domains.from_config(
        {"domains": {"dom-a": {"unix_group": "grp-a"}, "dom-b": {"unix_group": "grp-b"}}}
    )
    policy = DomainPolicy(Policy(), domains, ["dom-a", "dom-b"], Groups())
    svc = make_service(store, authorize=policy)
    assert svc.client.get(f"{API_PREFIX}/whoami", auth=FakeMunge()).json()["domains"] == ["dom-a"]
    dave = svc.client.get(f"{API_PREFIX}/whoami", auth=FakeMunge(uid=1003)).json()
    assert dave["domains"] == []
    unscoped = make_service(store)
    assert unscoped.client.get(f"{API_PREFIX}/whoami", auth=FakeMunge()).json()["domains"] is None


# R3, R7 (review): a user owns their action's debug dir; pointing it (or a parent) at another
# domain's files must not make the service, a member of every served domain, serve them.
def test_log_route_follows_no_symlink(
    svc: Service, store: InMemoryMetadataStore, roots: tuple[Path, Path]
) -> None:
    b = _log_build(store)
    secret = roots[1] / "dom-b" / "9" / "x"
    secret.mkdir(parents=True)
    (secret / "live.log").write_bytes(b"other domain's log")
    action_dir = debug_action_dir(roots[1], DOMAIN, str(b), "sim[t1/s=1]")
    action_dir.parent.mkdir(parents=True)
    action_dir.symlink_to(secret, target_is_directory=True)
    url = f"{API_PREFIX}/builds/{b}/actions/sim[t1/s=1]/log"
    r = svc.client.get(url, auth=ALICE)
    assert r.status_code == 404
    assert b"other domain" not in r.content
    action_dir.unlink()  # a symlinked build dir, and a symlinked live.log, likewise
    action_dir.parent.rmdir()
    action_dir.parent.symlink_to(secret.parent, target_is_directory=True)
    (secret.parent / action_dir.name).mkdir()
    (secret.parent / action_dir.name / "live.log").write_bytes(b"other domain's log")
    assert svc.client.get(url, auth=ALICE).status_code == 404
    action_dir.parent.unlink()
    action_dir.mkdir(parents=True)
    (action_dir / "live.log").symlink_to(secret / "live.log")
    assert svc.client.get(url, auth=ALICE).status_code == 404


# R3 (review): a FIFO or a directory planted as the live log is no log, not a hang or a 500.
@pytest.mark.parametrize("kind", ["fifo", "dir"])
def test_log_route_serves_regular_files_only(
    svc: Service, store: InMemoryMetadataStore, roots: tuple[Path, Path], kind: str
) -> None:
    b = _log_build(store)
    live = debug_action_dir(roots[1], DOMAIN, str(b), "sim[t1/s=1]") / "live.log"
    live.parent.mkdir(parents=True)
    if kind == "fifo":
        os.mkfifo(live)
    else:
        live.mkdir()
    r = svc.client.get(f"{API_PREFIX}/builds/{b}/actions/sim[t1/s=1]/log", auth=ALICE)
    assert r.status_code == 404


# R2 (review): the results route filters by step and pages; a cursor is bound to its query.
def test_results_route_filters_and_pages(svc: Service, store: InMemoryMetadataStore) -> None:
    b = new_build(store)
    for a in ("sim[t2]", "lint", "sim[t1]"):
        run_action(store, b, a)
    doc = _get(svc, f"/builds/{b}/results", step="sim")
    assert isinstance(doc, dict)
    assert [r["action_id"] for r in doc["results"]] == ["sim[t1]", "sim[t2]"]
    first = _get(svc, f"/builds/{b}/results", step="sim", limit=1)
    assert isinstance(first, dict)
    assert [r["action_id"] for r in first["results"]] == ["sim[t1]"]
    second = _get(svc, f"/builds/{b}/results", step="sim", limit=1, cursor=first["next"])
    assert isinstance(second, dict)
    assert [r["action_id"] for r in second["results"]] == ["sim[t2]"]
    for step in ("lint", None):
        params = {"limit": 1, "cursor": first["next"]} | ({"step": step} if step else {})
        r = svc.client.get(f"{API_PREFIX}/builds/{b}/results", params=params, auth=ALICE)
        assert r.status_code == 422, (step, r.text)


# R3 (review): a database outage on the log route is a retryable 503, not "no such action".
def test_log_route_store_outage_is_503(svc: Service, store: InMemoryMetadataStore) -> None:
    b = _log_build(store)

    def broken(*args: object) -> None:
        raise MetadataError("get_result: metadata database error") from ConnectionError("down")

    store.get_result = broken  # type: ignore[method-assign,assignment]
    r = svc.client.get(f"{API_PREFIX}/builds/{b}/actions/sim[t1/s=1]/log", auth=ALICE)
    assert r.status_code == 503


# R3 (review): a result whose log blob is gone (GC) falls back to the live log.
def test_log_route_falls_back_to_live(
    svc: Service, store: InMemoryMetadataStore, roots: tuple[Path, Path]
) -> None:
    b = _log_build(store)
    manifest = result("k", cpu_s=1, wall_s=1).model_copy(update={"log": hash_bytes(b"gone")})
    store.set_action_state(b, "sim[t1/s=1]", "running", key=manifest.action_key)
    store.record_result(b, "sim[t1/s=1]", manifest)
    live = debug_action_dir(roots[1], DOMAIN, str(b), "sim[t1/s=1]") / "live.log"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"live copy\n")
    r = svc.client.get(f"{API_PREFIX}/builds/{b}/actions/sim[t1/s=1]/log", auth=ALICE)
    assert (r.status_code, r.content) == (200, b"live copy\n")
    assert (r.headers["x-ebs-log-source"], r.headers["x-ebs-log-complete"]) == ("live", "false")


# R3 (review): a path that leaves its base or climbs is refused before any file is opened.
def test_open_under_refuses_dot_parts(tmp_path: Path) -> None:
    from ebs.meta.service.logs import _open_under

    (tmp_path / "y").write_text("x")
    assert _open_under(tmp_path, tmp_path / "x" / ".." / "y") is None
    assert _open_under(tmp_path / "x", tmp_path / "y") is None
    fd = _open_under(tmp_path, tmp_path / "y")
    assert fd is not None
    os.close(fd)
