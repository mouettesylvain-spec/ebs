"""Provenance queries respect domain authorization (P2-04 R4).

Through the metadata service, each walk is one domain-scoped route call, so a caller sees the
domains it is a member of; an export is followed only through its audit row, and an export into
(or from) a domain the caller cannot read is reported as hidden, never as data.
"""

from __future__ import annotations

import pytest

from ebs.core.clock import FakeClock
from ebs.core.errors import MetadataError
from ebs.domains import Domains
from ebs.meta.api import PROVENANCE_DEPTH_MAX, ReleaseId, ReleaseView
from ebs.meta.client import HttpMetadataStore, MetadataHttpError
from ebs.meta.memory import InMemoryMetadataStore
from ebs.meta.service.auth import Policy
from ebs.meta.service.authz import DomainPolicy
from ebs.release import provenance as prov
from tests.helpers.meta_http import FakeMunge, make_service
from tests.helpers.provenance import LIB, RES, S, World, world

# alice (uid 1000) in `test` only, bob (1001) in `other` only, carol (1002) in both.
GROUPS = {"alice": {"g-test"}, "bob": {"g-other"}, "carol": {"g-test", "g-other"}}


class Groups:
    def groups_of(self, user: str) -> frozenset[str]:
        return frozenset(GROUPS.get(user, set()))


def _setup() -> tuple[World, dict[str, HttpMetadataStore]]:
    backing = InMemoryMetadataStore(clock=FakeClock())
    w = world(S(backing, FakeClock()))
    domains = Domains.from_config({"domains": {
        "test": {"unix_group": "g-test"}, "other": {"unix_group": "g-other"},
        "third": {"unix_group": "g-third"},
    }})  # fmt: skip
    policy = DomainPolicy(Policy(), domains, ["test", "other", "third"], Groups())
    svc = make_service(backing, authorize=policy)
    clients = {name: svc.store(FakeMunge(uid=uid)) for name, uid in
               (("alice", 1000), ("bob", 1001), ("carol", 1002))}  # fmt: skip
    return w, clients


# R4
def test_queries_respect_domains() -> None:
    w, c = _setup()
    alice, bob, carol = c["alice"], c["bob"], c["carol"]
    full = prov.used_by(carol, w.rtl_release)
    assert {b.domain for b in full.builds} == {"test", "other"}
    assert full.hidden == ()
    # alice cannot read `other`: the export is hidden, its builds and tests are not reported.
    mine = prov.used_by(alice, w.rtl_release)
    assert {b.domain for b in mine.builds} == {"test"}
    assert {r.domain for r in mine.releases} == {"test"}
    assert [h.domain for h in mine.hidden] == ["other"]
    runs = prov.sim_runs(alice, w.rtl_release)
    assert {r.domain for r in runs.runs} == {"test"}
    assert [h.domain for h in runs.hidden] == ["other"]  # reported, not silently dropped
    # bob sees the copy in `other`; its source release (and even its domain) is hidden from him.
    up = prov.why(bob, "other", ids=[RES["o"].id])
    assert [g.domain for g in up.graphs] == ["other"]
    assert [(h.domain, h.release) for h in up.hidden] == [(None, w.rtl_release.id)]
    assert [g.domain for g in prov.why(carol, "other", ids=[RES["o"].id]).graphs] == [
        "other", "test",
    ]  # fmt: skip
    # The route itself refuses a non-member (403), whatever ids are asked.
    for direction in ("provenance_upstream", "provenance_downstream"):
        with pytest.raises(MetadataHttpError) as info:
            getattr(bob, direction)("test", ids=[LIB.id], keys=[], depth=PROVENANCE_DEPTH_MAX)
        assert info.value.status == 403


class _Failing(InMemoryMetadataStore):
    """A store whose release reads fail with a given HTTP status."""

    status = 503

    def get_release_by_id(self, release: ReleaseId) -> ReleaseView | None:
        raise MetadataHttpError("service says no", self.status)


# R4: only "not a member" / "no such object" (403/404) mean hidden; an outage is an error, so a
# result never looks complete when it is not.
def test_service_errors_are_not_hidden() -> None:
    store = _Failing(clock=FakeClock())
    w = world(S(store, FakeClock()))
    with pytest.raises(MetadataError):
        prov.used_by(store, w.rtl_release)
    with pytest.raises(MetadataError):
        prov.why(store, "other", ids=[RES["o"].id])
    store.status = 404
    assert [h.domain for h in prov.used_by(store, w.rtl_release).hidden] == ["other"]
    assert [h.release for h in prov.why(store, "other", ids=[RES["o"].id]).hidden] == [
        w.rtl_release.id
    ]
