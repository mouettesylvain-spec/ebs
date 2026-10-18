"""Channels: movable pointers to releases, with history and gate policies (P2-02 R3).

`ebs channel set NAME CHANNEL VERSION` points `NAME:CHANNEL` (e.g. `rtl-team/cpu:stable`) at a
release of that name and appends the move to the channel's history. A channel may have a gate
(`[release.gates]` in ebs.toml, read by the CLI with direct database access and by the metadata
service for API calls):

    [release.gates]
    stable = "cpu-gate"                 # every `stable` channel: a passing `cpu-gate` build
    "rtl-team/cpu:nightly" = "smoke"    # one release name's channel (wins over the above)

The channel then moves only if a passed build of that flow (its `project`) consumed one of the
release's outputs, as its `in` provenance edges record; the gate build found (or named with
`--gate-build`) is stored with the move.

`ebs channel set … --if-passing` (P2-07 R4, a CI gate job) also requires that one build (the
pipeline's `$EBS_BUILD_ID`) passed; it becomes the gate build, recorded even without a policy.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from ebs.core.errors import ConfigError, ReleaseError
from ebs.meta.api import (
    BuildId,
    BuildView,
    ChannelMove,
    GateCheck,
    MetadataStore,
    ReleaseStore,
    ReleaseView,
)

__all__ = ["GatePolicy", "check_gate", "move_channel", "require_passing"]


@dataclass(frozen=True)
class GatePolicy:
    """`[release.gates]`: channel (or `name:channel`) -> the gate flow's project."""

    gates: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_section(cls, section: Mapping[str, object]) -> GatePolicy:
        """From the `[release]` config section."""
        raw = section.get("gates", {})
        if not isinstance(raw, Mapping):
            raise ConfigError('[release.gates] must be a table of channel = "gate flow"')
        gates: dict[str, str] = {}
        for key, flow in raw.items():
            if not isinstance(key, str) or not isinstance(flow, str) or not flow:
                raise ConfigError(
                    f'[release.gates] {key!r}: expected channel = "<gate flow project>", got '
                    f"{flow!r}"
                )
            gates[key] = flow
        return cls(gates)

    def gate_for(self, name: str, channel: str) -> str | None:
        return self.gates.get(f"{name}:{channel}") or self.gates.get(channel)


def check_gate(
    store: ReleaseStore, release: ReleaseView, flow: str, *, gate_build: BuildId | None = None
) -> GateCheck:
    """A passed build of project `flow` that consumed `release`, else ReleaseError."""
    ids = sorted({out.id for out in release.outputs.values()}, key=str)
    builds = store.gate_builds(release.domain, flow, ids)
    what = f"release {release.name} {release.version}"
    if gate_build is not None:
        if gate_build not in builds:
            raise ReleaseError(
                f"gate: build {gate_build} is not a passed build of flow {flow!r} that consumed "
                f"{what} (its outputs are not among the build's inputs, or it did not pass)"
            )
        return GateCheck(flow=flow, build=gate_build)
    if not builds:
        raise ReleaseError(
            f"gate: the channel needs a passed build of flow {flow!r} that consumed {what}; "
            "none was found. Run the gate flow on this release first"
        )
    return GateCheck(flow=flow, build=builds[0])


def require_passing(store: MetadataStore, build: BuildId, release: ReleaseView) -> BuildView:
    """The build, if it is in the release's domain and passed; else ReleaseError (the gate
    fails)."""
    view = store.get_build(build)
    if view.domain != release.domain:
        raise ReleaseError(
            f"gate: build {build} is in domain {view.domain!r}, not in {release.domain!r} of "
            f"release {release.name} {release.version}; the channel was not moved"
        )
    if view.status != "passed":
        raise ReleaseError(
            f"gate: build {build} ({view.project}) is {view.status}, not passed; the channel was "
            "not moved"
        )
    return view


def move_channel(
    store: ReleaseStore,
    gates: GatePolicy,
    release: ReleaseView,
    channel: str,
    *,
    by: str,
    gate_build: BuildId | None = None,
    passed: BuildView | None = None,
) -> ChannelMove:
    """Point `release.name:channel` at `release`, after its gate (if any) passes.

    `passed` (from `require_passing`) is the gate build: the policy checks it, and without a
    policy it is recorded as the gate of the move.
    """
    if passed is not None:
        gate_build = passed.id
    if release.yanked_at is not None:
        raise ReleaseError(
            f"release {release.name} {release.version} is yanked ({release.yank_reason}); a "
            "channel cannot point at it"
        )
    flow = gates.gate_for(release.name, channel)
    if flow is not None:
        gate: GateCheck | None = check_gate(store, release, flow, gate_build=gate_build)
    elif passed is not None:
        gate = GateCheck(flow=passed.project, build=passed.id)
    else:
        gate = None
    return store.set_channel(release.domain, release.name, channel, release.id, by=by, gate=gate)
