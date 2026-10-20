"""What pilots and their queue items need of each other (P3-06 R1, R3).

The queue itself lives in the metadata store (`PilotQueueStore`: PostgreSQL with `FOR UPDATE SKIP
LOCKED`, reached through the metadata service from the nodes). This module turns an action into
the `QueueItem` the pilots match on, and a batch into the `Allocation` its pilot jobs request:
the largest cpus and memory of the batch, every license it names at its largest count, and the
node features of every constraint. A pilot asks only for items that fit what is left of it
(`Allocation.slot`, matched by `slot_fits`).

Licenses are requested per pilot job (`sbatch -L`), so a pilot holds them while it is idle too:
the trade-off of not paying a scheduling round per action, and the reason pilots stop after a
short `idle_s` (`[pilot].idle_s`, default 60 s).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from ebs.core.digest import Digest
from ebs.core.errors import ExecutorError
from ebs.exec.slurm.executor import JOB_TIME_GRACE_S
from ebs.meta.api import BuildId, PilotSlot, QueueItem
from ebs.plan.types import ActionSpec

__all__ = ["Allocation", "allocation_for", "features_of", "queue_item"]

_MIB: Final = 1 << 20


def features_of(*constraints: str | None) -> tuple[str, ...]:
    """The node features of constraints (features joined by `&`), sorted and unique."""
    return tuple(sorted({f for c in constraints if c for f in c.split("&") if f}))


def _int(spec: ActionSpec, attr: str) -> int | None:
    value = getattr(spec.resources, attr)
    if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
        raise ExecutorError(
            f"action {spec.action_id}: resources.{attr} is {value!r}, not a number; the planner "
            "must expand it before submission"
        )
    return value


@dataclass(frozen=True, slots=True)
class _Needs:
    cpus: int
    mem_mb: int | None
    time_s: int | None
    licenses: Mapping[str, int]
    features: tuple[str, ...]


def _needs(spec: ActionSpec) -> _Needs:
    """One cpu unless the action asks for more; memory in whole MiB, rounded up."""
    mem = _int(spec, "mem")
    return _Needs(
        cpus=_int(spec, "cpus") or 1,
        mem_mb=None if mem is None else max(1, math.ceil(mem / _MIB)),
        time_s=_int(spec, "time"),
        licenses=dict(spec.licenses),
        features=features_of(spec.resources.constraint),
    )


def queue_item(
    spec: ActionSpec, *, plan: Digest, build: BuildId | None, exclude: Sequence[str] = ()
) -> QueueItem:
    """The queue item of `spec` (`_needs`), for the runner to run from `plan`; pilots on the
    `exclude` nodes (P1-05: nodes that failed it) never lease it."""
    n = _needs(spec)
    return QueueItem(
        action_id=spec.action_id,
        plan=plan,
        build=build,
        cpus=n.cpus,
        mem_mb=n.mem_mb,
        time_s=n.time_s,
        licenses=dict(n.licenses),
        features=n.features,
        exclude=tuple(sorted(set(exclude))),
    )


@dataclass(frozen=True, slots=True)
class Allocation:
    """What one pilot job requests (and so offers its actions). Hashable: the executor keeps
    one set of pilots per allocation."""

    cpus: int
    mem_mb: int | None
    licenses: tuple[tuple[str, int], ...]  # sorted (feature, count)
    features: tuple[str, ...]  # sorted node features, including `[slurm].os_constraint`
    walltime_s: int

    def slot(self, pilot: str, *, node: str | None, elapsed_s: float) -> PilotSlot:
        """What the pilot can still offer after `elapsed_s` of its walltime: the runner of an
        action must finish, upload and report `JOB_TIME_GRACE_S` before SLURM's limit."""
        left = self.walltime_s - JOB_TIME_GRACE_S - math.ceil(elapsed_s)
        return PilotSlot(
            pilot=pilot,
            node=node,
            cpus=self.cpus,
            mem_mb=self.mem_mb,
            time_left_s=max(0, left),
            licenses=dict(self.licenses),
            features=self.features,
        )

    @property
    def constraint(self) -> str | None:
        return "&".join(self.features) or None

    def license_map(self) -> Mapping[str, int]:
        return dict(self.licenses)


def allocation_for(
    actions: Sequence[ActionSpec], *, walltime_s: int, os_constraint: str | None
) -> Allocation:
    """The pilot allocation for a batch: enough for each of its actions, with at least
    `walltime_s` of walltime (more if its longest action needs it)."""
    if not actions:
        raise ExecutorError("cannot size pilots for an empty batch")
    items = [_needs(a) for a in actions]
    mems = [i.mem_mb for i in items if i.mem_mb is not None]
    times = [i.time_s for i in items if i.time_s is not None]
    return Allocation(
        cpus=max(i.cpus for i in items),
        mem_mb=max(mems) if mems else None,
        licenses=_largest(i.licenses for i in items),
        features=features_of(os_constraint, *("&".join(i.features) for i in items)),
        walltime_s=max(walltime_s, max(times, default=0) + 2 * JOB_TIME_GRACE_S),
    )


def _largest(licenses: Iterable[Mapping[str, int]]) -> tuple[tuple[str, int], ...]:
    out: dict[str, int] = {}
    for request in licenses:
        for name, count in request.items():
            out[name] = max(out.get(name, 0), count)
    return tuple(sorted(out.items()))
