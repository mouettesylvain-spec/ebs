"""Job arrays: grouping a batch into sbatch calls, throttles and the array map (P1-02 R1, R2).

A batch of one action is one plain job. A larger batch becomes one job array, split into parts
of at most `max_array_size` tasks (SLURM's MaxArraySize); a part of a single action is a plain
job again. Each array has an **array map**, stored in the domain's CAS, that names the action of
every task index; the runner reads it with `--array-map` and `$SLURM_ARRAY_TASK_ID` (P1-04).

Array map (canonical JSON, so equal maps share a digest):

    {"v": 1, "plan": "sha256:<plan digest>", "actions": [<action id of task 0>, …]}

The throttle (`--array=…%N`) is the step's `resources.max_parallel` if set, else the configured
`[slurm].array_throttle`. It applies per array: a batch split into k parts may run k·N tasks.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from ebs.cas.api import CAS
from ebs.core.canon import canonical_json
from ebs.core.digest import Digest
from ebs.core.errors import DigestError, ExecutorError
from ebs.plan.types import ActionSpec

__all__ = [
    "ARRAY_MAP_VERSION",
    "ArrayMap",
    "JobPlan",
    "array_throttle",
    "decode_array_map",
    "encode_array_map",
    "load_array_map",
    "plan_jobs",
    "store_array_map",
]

ARRAY_MAP_VERSION: Final = 1


@dataclass(frozen=True, slots=True)
class JobPlan:
    """One sbatch call: a plain job (one action) or a job array (task i runs actions[i])."""

    actions: tuple[ActionSpec, ...]
    is_array: bool
    throttle: int | None  # arrays only


@dataclass(frozen=True, slots=True)
class ArrayMap:
    """Which action each task of a job array runs."""

    plan: Digest
    actions: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.actions:
            raise ExecutorError("an array map needs at least one action")
        if len(set(self.actions)) != len(self.actions):
            raise ExecutorError("an array map names each action once; got duplicates")

    def action_for(self, task: int) -> str:
        if not 0 <= task < len(self.actions):
            raise ExecutorError(
                f"array task {task} is not in the array map (tasks 0-{len(self.actions) - 1}); "
                "was the job submitted with another map?"
            )
        return self.actions[task]


def _positive(name: str, value: int) -> None:
    if value < 1:
        raise ExecutorError(f"{name} must be at least 1, got {value}")


def array_throttle(actions: Sequence[ActionSpec], default: int) -> int:
    """`%N` for an array of `actions`: the strictest `resources.max_parallel`, else `default`."""
    limits = [a.resources.max_parallel for a in actions if a.resources.max_parallel is not None]
    return min(limits) if limits else default


def plan_jobs(
    actions: Sequence[ActionSpec], *, max_array_size: int, throttle: int
) -> list[JobPlan]:
    """The sbatch calls for `actions`, in order (R1); `throttle` is the configured default."""
    _positive("max_array_size", max_array_size)
    _positive("array_throttle", throttle)
    jobs: list[JobPlan] = []
    for start in range(0, len(actions), max_array_size):
        part = tuple(actions[start : start + max_array_size])
        if len(part) == 1:
            jobs.append(JobPlan(part, is_array=False, throttle=None))
        else:
            jobs.append(JobPlan(part, is_array=True, throttle=array_throttle(part, throttle)))
    return jobs


def encode_array_map(array_map: ArrayMap) -> bytes:
    return canonical_json(
        {
            "v": ARRAY_MAP_VERSION,
            "plan": str(array_map.plan),
            "actions": list(array_map.actions),
        }
    )


def decode_array_map(data: bytes) -> ArrayMap:
    """Parse an array map; ExecutorError says what is wrong with it."""
    try:
        doc = json.loads(data)
    except ValueError as exc:
        raise ExecutorError(f"the array map is not JSON: {exc}") from None
    if not isinstance(doc, dict) or doc.get("v") != ARRAY_MAP_VERSION:
        raise ExecutorError(
            f"unsupported array map (expected an object with v = {ARRAY_MAP_VERSION}); "
            "was it written by another ebs version?"
        )
    actions = doc.get("actions")
    if not isinstance(actions, list) or not all(isinstance(a, str) for a in actions):
        raise ExecutorError("invalid array map: 'actions' must be a list of action ids")
    try:
        plan = Digest.parse(str(doc.get("plan")))
    except DigestError as exc:
        raise ExecutorError(f"invalid array map: 'plan' is not a digest ({exc})") from None
    try:
        return ArrayMap(plan=plan, actions=tuple(actions))
    except ExecutorError as exc:
        raise ExecutorError(f"invalid array map: {exc}") from None


def store_array_map(cas: CAS, plan: Digest, actions: Sequence[str]) -> Digest:
    """Put the array map of `actions` (task i runs actions[i]) into `cas`; returns its digest."""
    try:
        array_map = ArrayMap(plan=plan, actions=tuple(actions))
    except ExecutorError as exc:
        raise ExecutorError(f"invalid array map: {exc}") from None
    return cas.put_bytes(encode_array_map(array_map))


def load_array_map(cas: CAS, digest: Digest) -> ArrayMap:
    with cas.open(digest) as fh:
        return decode_array_map(fh.read())
