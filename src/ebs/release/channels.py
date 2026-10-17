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
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from ebs.core.errors import ConfigError, ReleaseError
from ebs.meta.api import BuildId, ChannelMove, GateCheck, ReleaseStore, ReleaseView

__all__ = ["GatePolicy", "check_gate", "move_channel"]


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


def move_channel(
    store: ReleaseStore,
    gates: GatePolicy,
    release: ReleaseView,
    channel: str,
    *,
    by: str,
    gate_build: BuildId | None = None,
) -> ChannelMove:
    """Point `release.name:channel` at `release`, after its gate (if any) passes."""
    if release.yanked_at is not None:
        raise ReleaseError(
            f"release {release.name} {release.version} is yanked ({release.yank_reason}); a "
            "channel cannot point at it"
        )
    flow = gates.gate_for(release.name, channel)
    gate = None if flow is None else check_gate(store, release, flow, gate_build=gate_build)
    return store.set_channel(release.domain, release.name, channel, release.id, by=by, gate=gate)
