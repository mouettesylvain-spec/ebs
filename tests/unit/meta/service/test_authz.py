"""Domain authorization of the metadata service (P2-01 R1, R2, R5): route coverage, the
fail-closed guard, `DomainPolicy` and the NSS group cache. The full matrix over PostgreSQL is
tests/integration/test_authz_matrix.py."""

from __future__ import annotations

import grp
import pwd
import re
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import APIRouter, Depends, FastAPI, Response

from ebs.core.clock import FakeClock
from ebs.core.errors import ConfigError
from ebs.domains import Domains
from ebs.meta.api import API_PREFIX
from ebs.meta.memory import InMemoryMetadataStore
from ebs.meta.service.auth import BuildOwner, PermissionDeniedError, Policy, Principal
from ebs.meta.service.authz import DomainDenied, DomainPolicy, NssGroups
from ebs.meta.service.deps import current_principal
from ebs.meta.service.routes import ok
from tests.helpers.meta_http import FakeMunge, make_service

DOMAINS = Domains.from_config(
    {
        "domains": {
            "dom-a": {"unix_group": "grp-a"},
            "dom-b": {"unix_group": "grp-b"},
            "dom-c": {"unix_group": "grp-c"},
        }
    }
)


class Groups:
    def __init__(self, table: dict[str, set[str]]) -> None:
        self.table = table

    def groups_of(self, user: str) -> frozenset[str]:
        return frozenset(self.table.get(user, set()))


def _calls(dependant: Any) -> Iterator[Callable[..., object]]:
    yield dependant.call
    for sub in dependant.dependencies:
        yield from _calls(sub)


def _contexts(app: Any) -> dict[tuple[str, str], Any]:
    found = {}
    for route in app.routes:
        for ctx in getattr(route, "effective_route_contexts", lambda: [])():
            for method in ctx.methods:
                found[(method, re.sub(r":[a-z]+\}", "}", ctx.path))] = ctx
    return found


# R1: every route in the OpenAPI document depends on `current_principal`, the dependency that
# authenticates the caller and arms the fail-closed authorization check.
def test_every_route_authorized() -> None:
    svc = make_service(InMemoryMetadataStore())
    paths = svc.client.get(f"{API_PREFIX}/openapi.json").json()["paths"]
    contexts = _contexts(svc.client.app)
    operations = [(m.upper(), p) for p, ops in paths.items() for m in ops]
    assert len(operations) >= 37
    missing = [
        f"{m} {p}"
        for m, p in operations
        if current_principal not in set(_calls(contexts[(m, p)].dependant))
    ]
    assert missing == []


# R1: a route that answers without calling `ServiceDeps.check` gets a 500, never its data.
def test_unauthorized_success_fails_closed() -> None:
    svc = make_service(InMemoryMetadataStore())
    leaky = APIRouter(dependencies=[Depends(current_principal)])

    @leaky.get("/leaky")
    def leak() -> Response:
        return ok(_Secret())  # type: ignore[arg-type]

    app = svc.client.app
    assert isinstance(app, FastAPI)
    app.include_router(leaky, prefix=API_PREFIX)
    r = svc.client.get(f"{API_PREFIX}/leaky", auth=FakeMunge())
    assert r.status_code == 500
    assert "did not authorize" in r.json()["detail"]
    assert "secret" not in r.text
    assert svc.client.get(f"{API_PREFIX}/whoami", auth=FakeMunge()).status_code == 200


class _Secret:
    def model_dump_json(self) -> str:
        return '{"secret": "nda content"}'


def _policy(**kwargs: Any) -> DomainPolicy:
    groups = Groups({"alice": {"grp-a"}, "bob": {"grp-b"}, "carol": {"grp-a", "grp-b"}})
    args: dict[str, Any] = {"served": ["dom-a", "dom-b"], "groups": groups}
    args.update(kwargs)
    return DomainPolicy(Policy(), DOMAINS, **args)


def _who(name: str, via: str = "munge", **kw: Any) -> Principal:
    return Principal(name=name, via=via, **kw)  # type: ignore[arg-type]


# R2: membership of the route's domain and of the domain of the build it names; then the owner
# rules of `Policy` still apply.
def test_domain_policy() -> None:
    policy = _policy(token_domains={"ci-a": ["dom-a"]})
    for name, member in [("alice", {"dom-a"}), ("bob", {"dom-b"}), ("carol", {"dom-a", "dom-b"}),
                         ("dave", set())]:  # fmt: skip
        assert policy.member_of(_who(name)) == member
        for domain in ("dom-a", "dom-b"):
            if domain in member:
                policy(_who(name), "read", domain=domain, owner=None)
            else:
                with pytest.raises(DomainDenied, match="not a member") as info:
                    policy(_who(name), "read", domain=domain, owner=None)
                assert info.value.domain == domain
    ci = _who("ci-a", via="token", token_kind="ci", team="demo")
    assert policy.member_of(ci) == {"dom-a"}
    with pytest.raises(DomainDenied):
        policy(ci, "cache_write", domain="dom-b", owner=None)
    # A build of dom-b, reached through a route of dom-a: both are checked.
    owner = BuildOwner(user_name="carol", project="demo", domain="dom-b", build=7)
    with pytest.raises(DomainDenied, match="dom-b"):
        policy(_who("alice"), "read", domain="dom-a", owner=owner)
    policy(_who("carol"), "build_write", domain=None, owner=owner)
    # Membership is not ownership: bob is in dom-b but may not change carol's build.
    with pytest.raises(PermissionDeniedError, match="only the owner"):
        policy(_who("bob"), "build_write", domain="dom-b", owner=owner)
    # Domain-free operations (toolchains, whoami) need no membership.
    policy(_who("dave"), "read", domain=None, owner=None)


# R5: the service serves only its listed domains; a configured but unserved domain is refused
# even to its members, and the list must be explicit and configured.
def test_served_domains() -> None:
    policy = _policy(groups=Groups({"erin": {"grp-c"}}))
    with pytest.raises(DomainDenied, match="not served"):
        policy(_who("erin"), "read", domain="dom-c", owner=None)
    with pytest.raises(DomainDenied, match="not served"):
        policy(_who("erin"), "read", domain="unknown", owner=None)
    with pytest.raises(ConfigError, match=r"\[meta_service\].domains must list"):
        _policy(served=[])
    with pytest.raises(ConfigError, match="not configured"):
        _policy(served=["dom-a", "nowhere"])
    with pytest.raises(ConfigError, match="token_domains"):
        _policy(token_domains={"ci-x": ["dom-c"]})


# NSS lookups (getgrouplist: LDAP/SSSD groups count) are cached per user for the TTL only.
def test_nss_groups_ttl() -> None:
    clock = FakeClock()
    calls: list[str] = []
    table = {"alice": [100, 200]}

    def getgrouplist(user: str, gid: int) -> list[int]:
        calls.append(user)
        return table[user]

    def getpwnam(user: str) -> pwd.struct_passwd:
        if user not in table:
            raise KeyError(user)
        return pwd.struct_passwd((user, "x", 1000, 100, "", "/", "/bin/sh"))

    def getgrgid(gid: int) -> grp.struct_group:
        if gid == 999:
            raise KeyError(gid)
        return grp.struct_group((f"g{gid}", "x", gid, []))

    groups = NssGroups(
        ttl_s=60, clock=clock, getpwnam=getpwnam, getgrouplist=getgrouplist, getgrgid=getgrgid
    )
    assert groups.groups_of("alice") == {"g100", "g200"}
    table["alice"] = [100, 999]
    clock.advance(30)
    assert groups.groups_of("alice") == {"g100", "g200"}  # cached
    clock.advance(31)
    assert groups.groups_of("alice") == {"g100"}  # removed from g200: effective after the TTL
    assert calls == ["alice", "alice"]
    assert groups.groups_of("nobody") == frozenset()
