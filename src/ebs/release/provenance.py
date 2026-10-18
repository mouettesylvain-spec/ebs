"""Provenance queries: `ebs why`, `ebs used-by`, `ebs tests` (P2-04).

The stores walk the `provenance_edges` recorded since phase 0 within one domain
(`ProvenanceStore`: a recursive CTE on PostgreSQL). This module composes those walks:

- `why`: upstream from object ids or action keys. An id with no producer in its domain that is
  the output of an exported release is followed into the source domain, and only through the
  export's audit row (`list_exports`), never by looking the id up elsewhere (R4).
- `downstream` / `used_by` / `tests`: from a release's output ids, then through the exports of
  that release and of every release found downstream, into their target domains.

Over the metadata service every walk is a domain-scoped route call, so the caller's domain
membership decides what is seen; an export into or from a domain the caller cannot read is
reported in `hidden`, never as data.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from ebs.core.digest import Digest
from ebs.core.errors import MetadataError
from ebs.meta.api import (
    PROVENANCE_DEPTH_MAX,
    ActionState,
    BuildId,
    ExportRecord,
    ParamJson,
    ProvenanceAction,
    ProvenanceGraph,
    ReleaseId,
    ReleaseView,
)
from ebs.release.releases import ReleaseBackend

__all__ = [
    "DEFAULT_DEPTH",
    "ExportLink",
    "Hidden",
    "Lineage",
    "SimRuns",
    "Source",
    "TestRun",
    "UsedBuild",
    "UsedBy",
    "UsedRelease",
    "downstream",
    "is_sim_rule",
    "junit_xml",
    "latest",
    "lineage_doc",
    "producers",
    "sim_runs",
    "sources",
    "to_dot",
    "used_by",
    "why",
]

DEFAULT_DEPTH: Final = PROVENANCE_DEPTH_MAX

_HIDDEN_STATUSES: Final = frozenset({403, 404})


@dataclass(frozen=True, slots=True)
class ExportLink:
    """An export followed by a query: `release` in `domain` is the copy of `source` in
    `source_domain`, per audit row `export`."""

    domain: str
    release: ReleaseId
    source_domain: str
    source: ReleaseId
    export: int


@dataclass(frozen=True, slots=True)
class Hidden:
    """An export the caller may not follow: `release` is the release on the far side (the
    source for `why`, the copy for downstream queries); `domain` is None when even that is
    unknown to the caller."""

    domain: str | None
    release: ReleaseId
    reason: str


@dataclass(frozen=True, slots=True)
class Lineage:
    """The walks of one query: the starting domain first, then the domains reached through
    exports, in the order they were reached."""

    graphs: tuple[ProvenanceGraph, ...]
    links: tuple[ExportLink, ...]
    hidden: tuple[Hidden, ...]

    @property
    def truncated(self) -> bool:
        return any(g.truncated for g in self.graphs)


@dataclass(frozen=True, slots=True)
class Source:
    """An input id with no producer in its walk: a source file or tree (or, in a truncated
    walk, an id whose producer lies beyond the depth)."""

    domain: str
    object_id: Digest
    paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UsedBuild:
    domain: str
    build: BuildId
    project: str
    flow_repo: str
    flow_commit: str
    user_name: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class UsedRelease:
    """A release downstream of the queried one: built from its outputs (`outputs`) or a copy
    made by an export (`export`)."""

    domain: str
    release: ReleaseId
    name: str
    version: str
    via: Literal["outputs", "export"]


@dataclass(frozen=True, slots=True)
class UsedBy:
    builds: tuple[UsedBuild, ...]
    releases: tuple[UsedRelease, ...]
    hidden: tuple[Hidden, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class TestRun:
    """One simulation action downstream of a release (`ebs tests`)."""

    __test__ = False  # not a pytest class

    domain: str
    build: BuildId
    action_id: str
    step: str
    rule: str
    test: ParamJson | None
    seed: ParamJson | None
    status: Literal["passed", "failed"] | None
    state: ActionState
    cached: bool
    toolchain: Digest | None
    finished_at: datetime | None
    project: str


def _hidden_error(exc: MetadataError) -> bool:
    """True for the service's "not a member" / "no such object" answers (HTTP 403 / 404)."""
    return getattr(exc, "status", None) in _HIDDEN_STATUSES


def producers(graph: ProvenanceGraph, object_id: Digest) -> list[Digest]:
    """Keys of the graph's actions that output `object_id` (as passed-down id or content)."""
    found = {
        e.action_key
        for e in graph.edges
        if e.direction == "out" and object_id in (e.object_id, e.content_digest)
    }
    return sorted(found, key=str)


def _export_record(store: ReleaseBackend, copy: ReleaseId) -> ExportRecord | None:
    for record in store.list_exports(copy):
        if record.exported == copy:
            return record
    return None


# --- why ------------------------------------------------------------------------------------------


def why(
    store: ReleaseBackend,
    domain: str,
    *,
    ids: Iterable[Digest] = (),
    keys: Iterable[Digest] = (),
    depth: int = DEFAULT_DEPTH,
) -> Lineage:
    """Where `ids` (object ids or content digests) and `keys` (action keys) came from."""
    graphs: list[ProvenanceGraph] = []
    links: list[ExportLink] = []
    hidden: list[Hidden] = []
    queue: list[tuple[str, list[Digest], list[Digest]]] = [(domain, list(ids), list(keys))]
    followed: set[tuple[str, int]] = set()
    while queue:
        where, want, want_keys = queue.pop(0)
        graph = store.provenance_upstream(where, ids=want, keys=want_keys, depth=depth)
        graphs.append(graph)
        # Leaves that are outputs of an exported release continue in the source domain.
        by_copy: dict[ReleaseId, list[Digest]] = {}
        for r in graph.releases:
            if r.exported_from is not None and not producers(graph, r.object_id):
                by_copy.setdefault(r.release, []).append(r.object_id)
        for copy, objects in sorted(by_copy.items()):
            if (where, copy) in followed:
                continue
            followed.add((where, copy))
            record = _export_record(store, copy)
            if record is None:
                continue  # an exported_from without its audit row links nothing
            source = _get_release(store, record.release)
            if source is None:
                reason = f"the source of release {copy}, exported into {where!r}, is not readable"
                hidden.append(Hidden(None, record.release, reason))
                continue
            links.append(ExportLink(where, copy, source.domain, source.id, record.id))
            queue.append((source.domain, sorted(set(objects), key=str), []))
    return Lineage(tuple(graphs), tuple(links), tuple(hidden))


def _get_release(store: ReleaseBackend, release: ReleaseId) -> ReleaseView | None:
    try:
        return store.get_release_by_id(release)
    except MetadataError as exc:
        if _hidden_error(exc):
            return None
        raise


def sources(lineage: Lineage) -> list[Source]:
    """The input ids of each walk that no walked action produced, nor an export link explains;
    by domain (walk order), then id."""
    found: list[Source] = []
    linked = {(link.domain, link.release) for link in lineage.links}
    for graph in lineage.graphs:
        produced = {e.object_id for e in graph.edges if e.direction == "out"}
        exported = {r.object_id for r in graph.releases if (graph.domain, r.release) in linked}
        paths: dict[Digest, set[str]] = {}
        for e in graph.edges:
            if e.direction == "in" and e.object_id not in produced | exported:
                paths.setdefault(e.object_id, set()).add(e.logical_path)
        found += [
            Source(graph.domain, oid, tuple(sorted(p)))
            for oid, p in sorted(paths.items(), key=lambda kv: str(kv[0]))
        ]
    return found


# --- downstream: used-by and tests ---------------------------------------------------------------


def downstream(
    store: ReleaseBackend, release: ReleaseView, *, depth: int = DEFAULT_DEPTH
) -> Lineage:
    """What consumed the outputs of `release`, in its domain and through exports."""
    graphs: list[ProvenanceGraph] = []
    links: list[ExportLink] = []
    hidden: list[Hidden] = []
    queue: list[ReleaseView] = [release]
    exported_seen: set[int] = set()
    while queue:
        root = queue.pop(0)
        ids = sorted({o.id for o in root.outputs.values()}, key=str)
        graph = store.provenance_downstream(root.domain, ids=ids, keys=[], depth=depth)
        graphs.append(graph)
        # The exports of the root and of every release found downstream of it.
        made = {e.object_id for e in graph.edges if e.direction == "out"}
        sources_ = [root.id, *sorted({r.release for r in graph.releases if r.object_id in made})]
        for rid in sources_:
            for record in store.list_exports(rid):
                if record.release != rid or record.id in exported_seen:
                    continue
                exported_seen.add(record.id)
                copy = _get_release(store, record.exported)
                if copy is None:
                    hidden.append(Hidden(record.to_domain, record.exported, f"release {rid} "
                                         f"was exported into domain {record.to_domain!r}, "
                                         "which is not readable"))  # fmt: skip
                    continue
                links.append(ExportLink(copy.domain, copy.id, root.domain, rid, record.id))
                queue.append(copy)
    return Lineage(tuple(graphs), tuple(links), tuple(hidden))


def used_by(store: ReleaseBackend, release: ReleaseView, *, depth: int = DEFAULT_DEPTH) -> UsedBy:
    """Builds and releases downstream of `release` (not its own build, not itself), transitively."""
    lineage = downstream(store, release, depth=depth)
    builds: dict[tuple[str, int], UsedBuild] = {}
    releases: dict[tuple[str, int], UsedRelease] = {}
    copies = {(link.domain, link.release) for link in lineage.links}
    for graph in lineage.graphs:
        for a in graph.actions:
            if (graph.domain, a.build) == (release.domain, release.build):
                continue
            builds.setdefault((graph.domain, a.build), UsedBuild(
                graph.domain, a.build, a.project, a.flow_repo, a.flow_commit, a.user_name,
                a.build_created_at,
            ))  # fmt: skip
        made = {e.object_id for e in graph.edges if e.direction == "out"}
        for r in graph.releases:
            if (graph.domain, r.release) == (release.domain, release.id):
                continue  # its own outputs derive from each other inside its build
            if (graph.domain, r.release) in copies:
                via: Literal["outputs", "export"] = "export"
            elif r.object_id in made:
                via = "outputs"
            else:
                continue
            found = UsedRelease(graph.domain, r.release, r.name, r.version, via)
            releases.setdefault((graph.domain, r.release), found)
    return UsedBy(
        builds=tuple(builds.values()),
        releases=tuple(releases.values()),
        hidden=lineage.hidden,
        truncated=lineage.truncated,
    )


def is_sim_rule(rule: str | None) -> bool:
    """True for simulation rule kinds (`questa.sim`, `vcs.sim`, …)."""
    return rule is not None and rule.endswith(".sim")


@dataclass(frozen=True, slots=True)
class SimRuns:
    """The runs `ebs tests` lists, and what the walk could not see: exports the caller may not
    read (`hidden`) and consumers beyond the depth (`truncated`)."""

    runs: tuple[TestRun, ...]
    hidden: tuple[Hidden, ...]
    truncated: bool


def sim_runs(store: ReleaseBackend, release: ReleaseView, *, depth: int = DEFAULT_DEPTH) -> SimRuns:
    """Every `*.sim` action whose inputs derive from the outputs of `release`: by domain (walk
    order), build and action id. Cache hits count: they report the recorded result."""
    lineage = downstream(store, release, depth=depth)
    runs: dict[tuple[str, int, str], TestRun] = {}
    for graph in lineage.graphs:
        for a in graph.actions:
            if is_sim_rule(a.rule):
                runs.setdefault((graph.domain, a.build, a.action_id), _run(graph.domain, a))
    return SimRuns(tuple(runs.values()), lineage.hidden, lineage.truncated)


def _run(domain: str, a: ProvenanceAction) -> TestRun:
    params = a.params or {}
    assert a.rule is not None
    return TestRun(
        domain=domain, build=a.build, action_id=a.action_id, step=a.step, rule=a.rule,
        test=params.get("test"), seed=params.get("seed"), status=a.status, state=a.state,
        cached=a.cached, toolchain=a.toolchain, finished_at=a.finished_at, project=a.project,
    )  # fmt: skip


def latest(runs: Sequence[TestRun]) -> list[TestRun]:
    """The newest run per (test, seed) (by finish time, then build id), in order of first
    appearance."""
    best: dict[tuple[str, str], TestRun] = {}
    for run in runs:
        key = (repr(run.test), repr(run.seed))
        held = best.get(key)
        if held is None or _newer(run, held):
            best[key] = run
    return list(best.values())


def _newer(a: TestRun, b: TestRun) -> bool:
    if a.finished_at is not None and b.finished_at is not None and a.finished_at != b.finished_at:
        return a.finished_at > b.finished_at
    if (a.finished_at is None) != (b.finished_at is None):
        return a.finished_at is not None
    return (a.build, a.domain) > (b.build, b.domain)


# --- renderings -----------------------------------------------------------------------------------


def lineage_doc(lineage: Lineage) -> dict[str, object]:
    """The JSON document of `ebs why --json` (v1)."""
    return {
        "v": 1,
        "truncated": lineage.truncated,
        "graphs": [g.model_dump(mode="json") for g in lineage.graphs],
        "links": [
            {"domain": x.domain, "release": x.release, "source_domain": x.source_domain,
             "source": x.source, "export": x.export}
            for x in lineage.links
        ],
        "hidden": [
            {"domain": h.domain, "release": h.release, "reason": h.reason} for h in lineage.hidden
        ],
        "sources": [
            {"domain": s.domain, "object_id": str(s.object_id), "paths": list(s.paths)}
            for s in sources(lineage)
        ],
    }  # fmt: skip


def _short(d: Digest) -> str:
    return f"{d.algo}:{d.hex[:12]}"


def _node(d: Digest) -> str:
    """A Graphviz node id for a digest: the algorithm and the full value, so ids never merge."""
    return f"{d.algo}_{d.hex}"


def _dot_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def to_dot(lineage: Lineage) -> str:
    """A Graphviz digraph: one cluster per walk, boxes for actions, ellipses for objects; an
    edge runs from an input to its action and from an action to its output."""
    lines = ["digraph provenance {", "  rankdir=BT;", "  node [fontsize=10];"]
    for n, graph in enumerate(lineage.graphs):
        lines += [
            f"  subgraph cluster_{n} {{",
            f"    label={_dot_quote('domain ' + graph.domain)};",
        ]
        rows: dict[Digest, list[ProvenanceAction]] = {}
        for a in graph.actions:
            rows.setdefault(a.key, []).append(a)
        objects: dict[Digest, str] = {}
        for e in graph.edges:
            objects.setdefault(e.object_id, e.logical_path)
        for key in sorted({e.action_key for e in graph.edges}, key=str):
            found = rows.get(key, [])
            label = found[0].action_id if found else _short(key)
            if found:
                first = found[0]
                label += f"\n{first.rule or first.step}\nbuild {first.build}"
                if len(found) > 1:
                    label += f" (+{len(found) - 1})"
            lines.append(f"    a{n}_{_node(key)} [shape=box,label={_dot_quote(label)}];")
        for oid, path in sorted(objects.items(), key=lambda kv: str(kv[0])):
            lines.append(
                f"    o{n}_{_node(oid)} [label={_dot_quote(path + chr(10) + _short(oid))}];"
            )
        for e in graph.edges:
            act, obj = f"a{n}_{_node(e.action_key)}", f"o{n}_{_node(e.object_id)}"
            lines.append(f"    {obj} -> {act};" if e.direction == "in" else f"    {act} -> {obj};")
        lines.append("  }")
    # An export joins the same id in two walks: source cluster -> copy cluster, dashed.
    held = [{e.object_id for e in g.edges} for g in lineage.graphs]
    for link in lineage.links:
        for d, graph in enumerate(lineage.graphs):
            if graph.domain != link.domain:
                continue
            for r in graph.releases:
                if r.release != link.release or r.object_id not in held[d]:
                    continue
                for src, other in enumerate(lineage.graphs):
                    if other.domain == link.source_domain and r.object_id in held[src]:
                        x = _node(r.object_id)
                        lines.append(
                            f"  o{src}_{x} -> o{d}_{x} "
                            f"[style=dashed,label={_dot_quote(f'export {link.export}')}];"
                        )
    lines.append("}")
    return "\n".join(lines) + "\n"


def junit_xml(runs: Sequence[TestRun], *, suite: str) -> str:
    """A JUnit XML report of `runs` (one testsuite; a failed result is a failure, a run without
    a result is skipped)."""
    root = ET.Element("testsuite", name=suite, tests=str(len(runs)),
                      failures=str(sum(r.status == "failed" for r in runs)),
                      skipped=str(sum(r.status is None for r in runs)))  # fmt: skip
    for r in runs:
        name = f"{r.test if r.test is not None else r.action_id}"
        if r.seed is not None:
            name += f" seed={r.seed}"
        case = ET.SubElement(root, "testcase", name=name, classname=f"{r.domain}.{r.project}",
                             file=f"build {r.build} {r.action_id}")  # fmt: skip
        if r.status == "failed":
            ET.SubElement(case, "failure", message=f"{r.action_id} failed in build {r.build}")
        elif r.status is None:
            ET.SubElement(case, "skipped", message=f"{r.action_id} has no result ({r.state})")
    ET.indent(root)
    return ET.tostring(root, encoding="unicode", xml_declaration=True) + "\n"
