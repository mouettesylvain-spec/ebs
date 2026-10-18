"""Provenance queries (P2-04): `memory` as a unit test, `pg` as an integration test.

The scenario is `tests.helpers.provenance.world`.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.engine import URL

from ebs.core.clock import FakeClock
from ebs.core.digest import hash_bytes
from ebs.meta.api import PROVENANCE_DEPTH_MAX, ReleaseCreate, ReleaseOutput, release_output_name
from ebs.meta.memory import InMemoryMetadataStore
from ebs.meta.pg import PgMetadataStore
from ebs.release import provenance as prov
from tests.helpers.pg import url_string
from tests.helpers.provenance import LIB, MODEL, OTHER, RES, SRC_A, SRC_B, TC, THIRD, D, S, world
from tests.helpers.release import Action, action_key, finish, out


@pytest.fixture(params=["memory", pytest.param("pg", marks=pytest.mark.integration)])
def s(request: pytest.FixtureRequest) -> Iterator[S]:
    clock = FakeClock()
    if request.param == "memory":
        yield S(InMemoryMetadataStore(clock=clock), clock)
        return
    url: URL = request.getfixturevalue("pg_url")
    pg = PgMetadataStore.from_config({"url": url_string(url)}, clock=clock)
    try:
        yield S(pg, clock)
    finally:
        pg.close()


def _keys(lineage: prov.Lineage) -> set[tuple[str, str]]:
    return {(g.domain, a.action_id) for g in lineage.graphs for a in g.actions}


# R1: the upstream chain of a sim result: actions (step, rule, params, toolchain), the commits
# of their builds, the releases met on the way, down to the sources.
def test_why_chain(s: S) -> None:
    w = world(s)
    lineage = prov.why(s.store, D, ids=[RES["t1-1"].id])
    assert _keys(lineage) == {
        (D, "sim[seed=1,test=t1]"), (D, "elab"), (D, "compile[lib=core]"), (D, "gen"),
    }  # fmt: skip
    assert not lineage.truncated
    [graph] = lineage.graphs
    compile_ = next(a for a in graph.actions if a.action_id == "compile[lib=core]")
    assert (compile_.step, compile_.rule, compile_.toolchain) == ("compile", "questa.compile", TC)
    assert compile_.params == {"lib": "core", "vlog_opts": "-sv"}
    assert (compile_.build, compile_.project, compile_.flow_commit) == (w.rtl, "rtl", "abc123")
    assert compile_.flow_repo == "git@example.invalid:rtl.git"
    sim = next(a for a in graph.actions if a.action_id == "sim[seed=1,test=t1]")
    assert (sim.build, sim.status, sim.state) == (w.verif, "passed", "done")
    assert {(r.name, r.output) for r in graph.releases} == {
        ("rtl/cpu", "compile[lib=core].worklib"), ("verif/model", "elab.model"),
    }  # fmt: skip
    assert [(x.object_id, x.paths) for x in prov.sources(lineage)] == sorted(
        [(SRC_A, ("src/a.sv", "srcs/a.sv")), (SRC_B, ("srcs/b.sv",))], key=lambda t: str(t[0])
    )
    # Every walked action carries all its edges (in and out).
    edges = {(e.action_key, e.direction, e.logical_path) for e in graph.edges}
    assert (action_key(w.rtl, "compile[lib=core]"), "out", "worklib") in edges
    assert (action_key(w.rtl, "gen"), "in", "src/a.sv") in edges
    # By content digest (an output's bytes) and by action key, the same producer is found.
    by_content = prov.why(s.store, D, ids=[LIB.digest])
    assert _keys(by_content) == {(D, "compile[lib=core]"), (D, "gen")}
    by_key = prov.why(s.store, D, keys=[action_key(w.verif, "elab")])
    assert _keys(by_key) == {(D, "elab"), (D, "compile[lib=core]"), (D, "gen")}
    # Unknown ids have no lineage; nothing from other domains leaks into a domain's walk.
    assert prov.why(s.store, D, ids=[hash_bytes(b"nothing")]).graphs[0].actions == []
    assert prov.why(s.store, THIRD, ids=[out(b"x").id]).graphs[0].releases == []


# R1: --depth bounds the walk and says so.
def test_why_depth_truncates(s: S) -> None:
    world(s)
    one = prov.why(s.store, D, ids=[RES["t1-1"].id], depth=1)
    assert _keys(one) == {(D, "sim[seed=1,test=t1]")}
    assert one.truncated
    three = prov.why(s.store, D, ids=[RES["t1-1"].id], depth=3)
    assert _keys(three) == {(D, "sim[seed=1,test=t1]"), (D, "elab"), (D, "compile[lib=core]")}
    assert three.truncated
    assert not prov.why(s.store, D, ids=[RES["t1-1"].id], depth=4).truncated
    for bad in (0, PROVENANCE_DEPTH_MAX + 1):
        with pytest.raises(Exception, match="depth"):
            prov.why(s.store, D, ids=[RES["t1-1"].id], depth=bad)


# R1, R4: upstream from domain `other` crosses into the source domain only through the export's
# audit row.
def test_why_crosses_export(s: S) -> None:
    w = world(s)
    lineage = prov.why(s.store, OTHER, ids=[RES["o"].id])
    assert [g.domain for g in lineage.graphs] == [OTHER, D]
    assert _keys(lineage) == {
        (OTHER, "sim[seed=7,test=t9]"), (D, "compile[lib=core]"), (D, "gen"),
    }  # fmt: skip
    [link] = lineage.links
    assert (link.domain, link.release, link.source_domain, link.source) == (
        OTHER, w.exported.id, D, w.rtl_release.id,
    )  # fmt: skip
    # `third` holds the same id without an export: its walk stops at the id.
    third = prov.why(s.store, THIRD, ids=[out(b"x").id])
    assert [g.domain for g in third.graphs] == [THIRD]
    assert _keys(third) == {(THIRD, "sim[seed=1,test=x]")}


# R2: downstream builds and releases, transitively through releases and exports.
def test_used_by_transitive(s: S) -> None:
    w = world(s)
    used = prov.used_by(s.store, w.rtl_release)
    assert [(b.domain, b.build) for b in used.builds] == [
        (D, w.verif),
        (D, w.soc),
        (D, w.rerun),
        (OTHER, w.other),
    ]
    assert {(r.domain, r.name, r.version, r.via) for r in used.releases} == {
        (D, "verif/model", "1.0", "outputs"), (OTHER, "rtl/cpu", "1.0", "export"),
    }  # fmt: skip
    assert used.hidden == ()
    soc = prov.used_by(s.store, w.verif_release)
    assert [b.build for b in soc.builds] == [w.soc, w.rerun]  # not the release's own build
    assert soc.releases == ()
    assert used.builds[0].project == "verif"


# R2: a release holding an output and something its own build derived from it (a filelist and
# the library compiled from it) is not a consumer of itself.
def test_used_by_excludes_the_release_itself(s: S) -> None:
    w = world(s)
    outputs = {}
    for action_id, output in (("gen", "filelist"), ("compile[lib=core]", "worklib")):
        result = s.store.get_result(w.rtl, action_id)
        assert result is not None
        o = result.outputs[output]
        outputs[release_output_name(action_id, output)] = ReleaseOutput(
            action_id=action_id, output=output, **o.model_dump()
        )
    both = s.store.create_release(
        ReleaseCreate(domain=D, name="rtl/full", version="1.0", build=w.rtl, outputs=outputs),
        by="alice",
    )
    used = prov.used_by(s.store, both)
    assert (D, "rtl/full") not in {(r.domain, r.name) for r in used.releases}
    assert (D, "verif/model", "1.0", "outputs") in {
        (r.domain, r.name, r.version, r.via) for r in used.releases
    }


# R3: every *.sim action whose inputs derive from the release, with test, seed, status,
# toolchain, build and date; lint and compile are not tests.
def test_tests_for_release(s: S) -> None:
    w = world(s)
    runs = prov.sim_runs(s.store, w.rtl_release).runs
    got = [(r.domain, r.build, r.test, r.seed, r.status, r.toolchain) for r in runs]
    assert got == [
        (D, w.verif, "t1", 1, "passed", TC),
        (D, w.verif, "t1", 2, "failed", TC),
        (D, w.soc, "t1", 1, "failed", TC),
        (D, w.rerun, "t1", 1, "passed", TC),
        (OTHER, w.other, "t9", 7, "passed", TC),
    ]
    dates = [r.finished_at for r in runs]
    assert None not in dates
    first, rerun = dates[0], dates[3]
    assert first is not None
    assert rerun is not None
    assert first < rerun
    assert [r.build for r in prov.sim_runs(s.store, w.verif_release).runs] == [
        w.verif, w.verif, w.soc, w.rerun,
    ]  # fmt: skip


# R3: --latest keeps the newest run per (test, seed).
def test_latest_filter(s: S) -> None:
    w = world(s)
    latest = prov.latest(prov.sim_runs(s.store, w.rtl_release).runs)
    assert [(r.build, r.test, r.seed, r.status) for r in latest] == [
        (w.rerun, "t1", 1, "passed"),
        (w.verif, "t1", 2, "failed"),
        (w.other, "t9", 7, "passed"),
    ]


# R3: a cache hit in a later build is a run of the test too (its recorded result).
def test_tests_include_cache_hits(s: S) -> None:
    w = world(s)
    key = action_key(w.verif, "sim[seed=1,test=t1]")
    hit = finish(s.store, {"sim[seed=1,test=t1]": Action(
        {"result": RES["t1-1"]}, inputs={"model": MODEL.id}, rule="questa.sim",
        params={"test": "t1", "seed": 1}, toolchain=TC, key=key, cached=True)},
        project="verif")  # fmt: skip
    runs = [r for r in prov.sim_runs(s.store, w.verif_release).runs if r.build == hit]
    assert [(r.state, r.cached, r.status) for r in runs] == [("cached", True, "passed")]


# The store primitives agree between backends: one domain, bounded, sorted.
def test_graph_is_domain_scoped_and_sorted(s: S) -> None:
    w = world(s)
    graph = s.store.provenance_downstream(D, ids=[LIB.id], keys=[], depth=PROVENANCE_DEPTH_MAX)
    assert graph.domain == D
    assert {a.build for a in graph.actions} == {w.verif, w.soc, w.rerun}
    assert graph.edges == sorted(
        graph.edges, key=lambda e: (str(e.action_key), e.direction, e.logical_path)
    )
    assert graph.actions == sorted(graph.actions, key=lambda a: (a.build, a.action_id))
    shallow = s.store.provenance_downstream(D, ids=[LIB.id], keys=[], depth=1)
    assert {a.action_id for a in shallow.actions} == {"elab", "lint"}
    assert shallow.truncated


# R4: action keys are not scoped by domain (identical inputs give the same key in two domains),
# so every walk must filter edges and action rows by domain at every step.
def test_walk_scoped_when_key_shared_across_domains(s: S) -> None:
    w = world(s)
    shared = action_key(w.rtl, "compile[lib=core]")
    finish(s.store, {"compile[lib=core]": Action(
        {"worklib": out(b"third worklib")}, inputs={"third/x.sv": hash_bytes(b"third source")},
        rule="questa.compile", key=shared)}, domain=THIRD, project="x")  # fmt: skip
    d_paths = {"srcs/a.sv", "srcs/b.sv", "filelist", "worklib", "src/a.sv"}
    for lineage in (prov.why(s.store, D, ids=[LIB.digest]), prov.why(s.store, D, keys=[shared])):
        [graph] = lineage.graphs
        assert {a.project for a in graph.actions} == {"rtl"}
        assert {e.logical_path for e in graph.edges} == d_paths
    [third] = prov.why(s.store, THIRD, keys=[shared]).graphs
    assert {a.project for a in third.actions} == {"x"}
    assert {e.logical_path for e in third.edges} == {"third/x.sv", "worklib"}
    assert LIB.id not in {e.object_id for e in third.edges}
    down = s.store.provenance_downstream(D, ids=[SRC_A], keys=[], depth=PROVENANCE_DEPTH_MAX)
    assert "x" not in {a.project for a in down.actions}
    assert {e.action_key for e in down.edges} <= {a.key for a in down.actions}
    assert "third/x.sv" not in {e.logical_path for e in down.edges}


# R2, R3, R4: the export of a release found downstream is followed too (into its domain).
def test_used_by_follows_export_of_downstream_release(s: S) -> None:
    w = world(s)
    s.store.record_export(w.verif_release.id, "fourth", by="carol")
    fourth = finish(s.store, {"sim[seed=3,test=t4]": Action(
        {"result": out(b"fourth")}, inputs={"model": MODEL.id}, rule="questa.sim",
        params={"test": "t4", "seed": 3})}, domain="fourth", project="f")  # fmt: skip
    used = prov.used_by(s.store, w.rtl_release)
    assert ("fourth", fourth) in {(b.domain, b.build) for b in used.builds}
    assert ("fourth", "verif/model", "export") in {(r.domain, r.name, r.via) for r in used.releases}
    assert ("fourth", fourth) in {
        (r.domain, r.build) for r in prov.sim_runs(s.store, w.rtl_release).runs
    }
    lineage = prov.downstream(s.store, w.rtl_release)
    assert sorted((x.domain, x.source_domain) for x in lineage.links) == [("fourth", D), (OTHER, D)]


# R1, R4: an export with no consumer yet in its domain still leads back to the source.
def test_why_export_without_consumers(s: S) -> None:
    w = world(s)
    s.store.record_export(w.rtl_release.id, "fourth", by="carol")
    lineage = prov.why(s.store, "fourth", ids=[LIB.id])
    assert [g.domain for g in lineage.graphs] == ["fourth", D]
    assert len(lineage.links) == 1
    assert {a.action_id for a in lineage.graphs[1].actions} == {"compile[lib=core]", "gen"}
    # The exported id is not a source of the copy's domain: the link explains it.
    assert ("fourth", LIB.id) not in {(x.domain, x.object_id) for x in prov.sources(lineage)}


# R1: a content digest names a producer only as a starting point; later levels follow ids.
def test_content_digest_matches_only_the_start(s: S) -> None:
    stamped = out(b"stamped", deterministic=False)  # id != content digest
    result = out(b"q result")
    finish(s.store, {
        "p": Action({"lib": stamped}, rule="shell"),
        # a source that happens to hold the same bytes as p's output, by digest
        "q": Action({"r": result}, inputs={"same.bytes": stamped.digest}, rule="shell"),
    }, domain="c5")  # fmt: skip
    assert {a.action_id for a in prov.why(s.store, "c5", ids=[result.id]).graphs[0].actions} == {
        "q"
    }
    assert {
        a.action_id for a in prov.why(s.store, "c5", ids=[stamped.digest]).graphs[0].actions
    } == {"p"}


# R1, R2: truncation is reported exactly: set when a level past the depth has unwalked keys,
# clear when the walk ended (also when a key is reached again by a longer path).
def test_truncation_flags(s: S) -> None:
    w = world(s)
    assert prov.used_by(s.store, w.rtl_release, depth=1).truncated
    assert not prov.used_by(s.store, w.rtl_release).truncated
    sims = s.store.provenance_downstream(D, ids=[MODEL.id], keys=[], depth=1)
    assert {a.step for a in sims.actions} == {"sim"}
    assert not sims.truncated  # nothing consumes the sim results
    x, y, z = out(b"x"), out(b"y"), out(b"z")
    finish(s.store, {
        "a": Action({"x": x}, rule="shell"),
        "b": Action({"y": y}, inputs={"x": x.id}, rule="shell"),
        "c": Action({"z": z}, inputs={"x": x.id, "y": y.id}, rule="shell"),
    }, domain="diamond")  # fmt: skip
    two = s.store.provenance_upstream("diamond", ids=[z.id], keys=[], depth=2)
    assert {a.action_id for a in two.actions} == {"a", "b", "c"}
    assert not two.truncated
    one = s.store.provenance_upstream("diamond", ids=[z.id], keys=[], depth=1)
    assert one.truncated


# R1: a local producer wins over an export link (the copy's domain rebuilt the output).
def test_why_prefers_local_producer(s: S) -> None:
    w = world(s)
    finish(s.store, {"compile[lib=core]": Action(
        {"worklib": LIB}, inputs={"srcs/a.sv": SRC_A}, rule="questa.compile",
        key=action_key(w.rtl, "compile[lib=core]"))}, domain=OTHER, project="o")  # fmt: skip
    lineage = prov.why(s.store, OTHER, ids=[LIB.id])
    assert lineage.links == ()
    assert [g.domain for g in lineage.graphs] == [OTHER]


# R3: only `*.sim` rule kinds are tests.
def test_only_sim_rules_are_tests(s: S) -> None:
    w = world(s)
    finish(s.store, {"simple": Action({"r": out(b"simple")}, inputs={"model": MODEL.id},
                                      rule="shell.simple")}, project="s")  # fmt: skip
    assert "simple" not in {r.action_id for r in prov.sim_runs(s.store, w.rtl_release).runs}


# The depth is an integer, not a bool.
def test_depth_must_be_an_int(s: S) -> None:
    world(s)
    with pytest.raises(Exception, match="depth"):
        s.store.provenance_upstream(D, ids=[LIB.id], keys=[], depth=True)
