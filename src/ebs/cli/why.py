"""`ebs why TARGET`: where an artifact came from (P2-04 R1).

TARGET is a digest (an object id, a content digest or an action key), a release
(`NAME@VERSION`, all its outputs, or `NAME@VERSION:OUTPUT`), or an action id of a build
(`--build`, by default the latest build recorded here). The upstream chain is printed as a tree:
each object with the action that produced it (step, rule, result, build, project and commit,
toolchain, params) and that action's inputs, down to the sources; ids that came into the domain
through an export continue in the source domain. `--json` prints the walked graphs, `--dot` a
Graphviz digraph. `--depth N` stops after N action levels.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import typer

from ebs.cli._context import SiteServices
from ebs.cli._output import Output, UsageError, handle_errors, make_output
from ebs.cli._records import find_build
from ebs.cli.logs import match_action
from ebs.cli.plan import JSON_OPTION
from ebs.cli.release import default_domain, find_release, release_store
from ebs.core.digest import Digest
from ebs.core.errors import ConfigError, DigestError, ReleaseError
from ebs.meta.api import (
    PROVENANCE_DEPTH_MAX,
    BuildId,
    MetadataBackend,
    ProvenanceAction,
    ProvenanceGraph,
)
from ebs.release.provenance import DEFAULT_DEPTH, Lineage, lineage_doc, producers, to_dot, why

__all__ = ["DEPTH_OPTION", "why_command"]

DEPTH_OPTION = typer.Option(
    DEFAULT_DEPTH,
    "--depth",
    min=1,
    max=PROVENANCE_DEPTH_MAX,
    metavar="N",
    help=f"Follow at most N action levels (1-{PROVENANCE_DEPTH_MAX}).",
)


@dataclass(frozen=True)
class Target:
    domain: str
    ids: tuple[Digest, ...] = ()
    keys: tuple[Digest, ...] = ()
    label: str = ""


def _digest(text: str) -> Digest | None:
    if ":" not in text or "@" in text:
        return None
    try:
        return Digest.parse(text)
    except DigestError:
        return None


def resolve_target(
    services: SiteServices,
    store: MetadataBackend,
    target: str,
    *,
    domain: str | None,
    build: str | None,
) -> Target:
    """The ids or keys TARGET names (see the module docstring)."""
    digest = _digest(target)
    if digest is not None:
        return Target(default_domain(services, domain), (digest,), (digest,), str(digest))
    if "@" in target:
        name, _, rest = target.partition("@")
        version, _, output = rest.partition(":")
        where = default_domain(services, domain)
        rel = find_release(store, where, name, version)
        if output and output not in rel.outputs:
            raise ReleaseError(
                f"release {name} {version} has no output {output!r}; it has: "
                + ", ".join(sorted(rel.outputs))
            )
        chosen = [o for n, o in sorted(rel.outputs.items()) if not output or n == output]
        return Target(rel.domain, tuple(o.id for o in chosen), (), target)
    build_id = _build(services, build)
    view = store.get_build(build_id)
    rows = {r.action_id: r for r in store.list_actions(build_id)}
    action_id = match_action(target, {a: a for a in rows})
    key = rows[action_id].key
    if key is None:
        raise ConfigError(
            f"action {action_id!r} of build {build_id} has no action key yet (its inputs were "
            "never all known); nothing was recorded about where it came from"
        )
    return Target(view.domain, (), (key,), f"{action_id} (build {build_id})")


def _build(services: SiteServices, ref: str | None) -> BuildId:
    """A build recorded here (UUID prefix or id; the latest without `ref`), or a store id."""
    try:
        return find_build(services.cwd, ref).build
    except ConfigError:
        if ref is not None and ref.isdigit():
            return BuildId(int(ref))
        if ref is None:
            raise ConfigError(
                "give a digest, NAME@VERSION[:OUTPUT], or an action with --build B: there is "
                "no build recorded in this directory to take the action from"
            ) from None
        raise


# --- text rendering --------------------------------------------------------------------------


def short(d: Digest) -> str:
    return f"{d.algo}:{d.hex[:12]}"


def _value(v: object) -> str:
    return v if isinstance(v, str) else json.dumps(v)


@dataclass
class _Tree:
    lineage: Lineage
    out: Output
    shown: set[tuple[int, Digest]] = field(default_factory=set)

    def graph(self, n: int) -> ProvenanceGraph:
        return self.lineage.graphs[n]

    def rows(self, n: int, key: Digest) -> list[ProvenanceAction]:
        found = [a for a in self.graph(n).actions if a.key == key]
        return sorted(found, key=lambda a: (a.cached, a.build, a.action_id))

    def releases(self, n: int, oid: Digest) -> str:
        names = sorted(
            f"{r.name} {r.version} {r.output}" for r in self.graph(n).releases if r.object_id == oid
        )
        return "".join(f"  [release {x}]" for x in names)

    def export_source(self, n: int, oid: Digest) -> tuple[int, str] | None:
        """The walk an id continues in through an export, and the note to print."""
        g = self.graph(n)
        copies = {r.release for r in g.releases if r.object_id == oid}
        for link in self.lineage.links:
            if link.domain != g.domain or link.release not in copies:
                continue
            for m in range(n + 1, len(self.lineage.graphs)):
                other = self.graph(m)
                if other.domain == link.source_domain and producers(other, oid):
                    return m, f"  (export {link.export} of release {link.source} from domain " \
                              f"{link.source_domain!r})"  # fmt: skip
        return None

    def obj(self, n: int, oid: Digest, label: str, prefix: str, last: bool) -> None:
        connector, below = ("└── ", "    ") if last else ("├── ", "│   ")
        text = f"{prefix}{connector}{label}  {short(oid)}{self.releases(n, oid)}"
        made = producers(self.graph(n), oid)
        where = n
        if not made:
            source = self.export_source(n, oid)
            if source is not None:
                where, note = source
                text += note
                made = producers(self.graph(where), oid)
            elif self.graph(n).truncated:
                text += "  (no producer within --depth)"
            else:
                text += "  (source)"
        self.out.line(text)
        made = sorted(made, key=lambda k: self.order(where, k))
        for i, key in enumerate(made):
            self.action(where, key, prefix + below, i == len(made) - 1)

    def order(self, n: int, key: Digest) -> tuple[str, str]:
        """Producers by action id, not key digest: stable when a key schema changes keys."""
        rows = self.rows(n, key)
        return (rows[0].action_id if rows else "", str(key))

    def action(self, n: int, key: Digest, prefix: str, last: bool) -> None:
        connector, below = ("└── ", "    ") if last else ("├── ", "│   ")
        rows = self.rows(n, key)
        if not rows:
            self.out.line(f"{prefix}{connector}action {short(key)}  (no action row)")
            return
        a = rows[0]
        commit = f"@{a.flow_commit[:12]}" if a.flow_commit else ""
        dirty = "+dirty" if a.flow_dirty else ""
        more = f"  (+{len(rows) - 1} more)" if len(rows) > 1 else ""
        result = a.status or a.state
        self.out.line(
            f"{prefix}{connector}{a.action_id}  {a.rule or a.step}  {result}  build {a.build} "
            f"{a.project}{commit}{dirty}{more}"
        )
        if (n, key) in self.shown:
            self.out.line(f"{prefix}{below}(inputs shown above)")
            return
        self.shown.add((n, key))
        details = []
        if a.toolchain is not None:
            details.append(f"toolchain {short(a.toolchain)}")
        if a.params:
            details.append(
                "params " + " ".join(f"{k}={_value(v)}" for k, v in sorted(a.params.items()))
            )
        for d in details:
            self.out.line(f"{prefix}{below}{d}")
        inputs = sorted(
            (e.logical_path, e.object_id)
            for e in self.graph(n).edges
            if e.action_key == key and e.direction == "in"
        )
        for i, (path, oid) in enumerate(inputs):
            self.obj(n, oid, path, prefix + below, i == len(inputs) - 1)


def render(out: Output, lineage: Lineage, target: Target, depth: int) -> None:
    first = lineage.graphs[0]
    out.line(f"why {target.label} (domain {target.domain})")
    tree = _Tree(lineage, out)
    roots: list[tuple[str, Digest]] = [("object", d) for d in target.ids if producers(first, d)]
    roots += [("action", k) for k in target.keys if any(a.key == k for a in first.actions)]
    if not roots:
        ids = target.ids or target.keys
        if any(r.object_id in ids for r in first.releases):
            roots = [("object", d) for d in target.ids]
        else:
            out.line("nothing recorded produced it: no action of this domain has it as an output")
    for i, (kind, d) in enumerate(roots):
        last = i == len(roots) - 1
        if kind == "object":
            tree.obj(0, d, "output", "", last)
        else:
            tree.action(0, d, "", last)
    if lineage.truncated:
        out.line(f"(truncated at depth {depth}: deeper producers are not shown; raise --depth)")
    for h in lineage.hidden:
        out.line(f"(hidden: {h.reason})")


@handle_errors
def why_command(
    ctx: typer.Context,
    target: str = typer.Argument(
        ..., metavar="TARGET", help="A digest, NAME@VERSION[:OUTPUT], or an action id (--build)."
    ),
    domain: str | None = typer.Option(
        None, "--domain", metavar="D", help="Domain; default: that of the latest build here."
    ),
    build: str | None = typer.Option(
        None, "--build", metavar="BUILD", help="Build of an action TARGET (UUID prefix or id)."
    ),
    depth: int = DEPTH_OPTION,
    dot: bool = typer.Option(False, "--dot", help="Print a Graphviz digraph."),
    json_mode: bool = JSON_OPTION,
) -> None:
    """Show where an artifact came from: actions, params, toolchains, commits, sources."""
    services: SiteServices = ctx.obj
    if json_mode and dot:
        raise UsageError("give --json or --dot, not both")
    out = make_output(services.environ, json_mode=json_mode, tty=services.is_tty())
    store = release_store(services)
    found = resolve_target(services, store, target, domain=domain, build=build)
    lineage = why(store, found.domain, ids=found.ids, keys=found.keys, depth=depth)
    if json_mode:
        out.emit({**lineage_doc(lineage), "target": found.label, "domain": found.domain})
    elif dot:
        for line in to_dot(lineage).splitlines():
            out.line(line)
    else:
        render(out, lineage, found, depth)
