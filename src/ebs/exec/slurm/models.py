"""Pydantic models of the `squeue --json` / `sacct --json` fields ebs reads (SLURM 26.05).

Only the fields we use are declared; SLURM adds fields every release, so unknown ones are ignored
(`extra="ignore"`). A missing required field means the output changed shape: the adapter turns
the ValidationError into an ExecutorError naming the SLURM version (P1-01 R2).

Both commands are normalized into `SlurmJobInfo`, which is what the executor consumes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, NewType

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ebs.core.errors import ExecutorError
from ebs.exec.api import NODE_PATTERN

__all__ = [
    "ArrayTaskId",
    "JobId",
    "JobRef",
    "SacctResponse",
    "SlurmJobInfo",
    "SqueueResponse",
    "array_spec_contains",
    "slurm_release",
]

JobId = NewType("JobId", str)  # decimal SLURM job id, e.g. "1234"

_JOB_ID = re.compile(r"^[0-9]+$")


@dataclass(frozen=True, order=True, slots=True)
class ArrayTaskId:
    """One element of a job array, written `<array job id>_<task id>` like SLURM does."""

    array_job_id: JobId
    task_id: int

    def __str__(self) -> str:
        return f"{self.array_job_id}_{self.task_id}"

    @classmethod
    def parse(cls, text: str) -> ArrayTaskId:
        job, sep, task = text.partition("_")
        if not sep or not _JOB_ID.match(job) or not _JOB_ID.match(task):
            raise ExecutorError(
                f"{text!r} is not a SLURM array task id: expected '<job id>_<task id>', "
                "e.g. '1234_5'"
            )
        return cls(JobId(job), int(task))


JobRef = JobId | ArrayTaskId


@dataclass(frozen=True, slots=True)
class SlurmJobInfo:
    """What SLURM says about one job or array element, normalized from squeue or sacct."""

    job_id: JobId  # the element's own id (array elements get one once they start)
    state: str  # base state, e.g. "PENDING", "COMPLETED"
    source: Literal["squeue", "sacct"]
    flags: frozenset[str] = field(default_factory=frozenset)  # e.g. COMPLETING, REQUEUED
    reason: str | None = None  # pending/end reason; None when SLURM says "None"
    exit_code: int | None = None  # the batch script's (= the runner's) return code
    signal: int | None = None  # signal that ended it, if any
    array_job_id: JobId | None = None
    array_task_id: int | None = None
    node: str | None = None  # the one node it ran on; None if pending or several (P1-05 R3)


# --- raw JSON models -------------------------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class OptionalNumber(_Model):
    """SLURM's `{"set": bool, "infinite": bool, "number": int}` wrapper for optional integers."""

    set: bool = False
    infinite: bool = False
    number: int = 0

    @property
    def value(self) -> int | None:
        return self.number if self.set and not self.infinite else None

    @classmethod
    def coerce(cls, raw: object) -> object:
        # Some fields are plain integers in some releases.
        if isinstance(raw, int) and not isinstance(raw, bool):
            return {"set": True, "infinite": False, "number": raw}
        return raw


def _number(raw: object) -> object:
    return OptionalNumber.coerce(raw)


class Signal(_Model):
    id: OptionalNumber = OptionalNumber()
    name: str = ""

    @field_validator("id", mode="before")
    @classmethod
    def _coerce_id(cls, raw: object) -> object:
        return _number(raw)


class ExitCode(_Model):
    status: list[str] = []
    return_code: OptionalNumber = OptionalNumber()
    signal: Signal = Signal()

    @field_validator("return_code", mode="before")
    @classmethod
    def _coerce_rc(cls, raw: object) -> object:
        return _number(raw)

    def code_and_signal(self) -> tuple[int | None, int | None]:
        if "PENDING" in self.status:  # not finished yet
            return None, None
        return self.return_code.value, self.signal.id.value or None


def _state_list(raw: object) -> object:
    # job_state is a list of base state + flags since 23.02; older releases used a string.
    if isinstance(raw, str):
        return [raw]
    return raw


def _reason(raw: str) -> str | None:
    return None if raw in ("", "None") else raw


_ONE_NODE = re.compile(NODE_PATTERN, re.ASCII)


def _node(raw: str) -> str | None:
    """`nodes` names one host for our single-node jobs; "", "None assigned" or a host list
    (`n[1-2]`, `n1,n2`) is no single node to blame."""
    return raw if _ONE_NODE.match(raw) and raw != "None" else None


class SqueueJob(_Model):
    job_id: int
    job_state: list[str] = Field(min_length=1)
    state_reason: str = "None"
    array_job_id: OptionalNumber = OptionalNumber()
    array_task_id: OptionalNumber = OptionalNumber()
    array_task_string: str = ""
    exit_code: ExitCode = ExitCode()
    name: str = ""
    nodes: str = ""
    comment: str = ""

    @field_validator("job_state", mode="before")
    @classmethod
    def _coerce_state(cls, raw: object) -> object:
        return _state_list(raw)

    @field_validator("array_job_id", "array_task_id", mode="before")
    @classmethod
    def _coerce_numbers(cls, raw: object) -> object:
        return _number(raw)

    def info(self, *, task_id: int | None = None) -> SlurmJobInfo:
        code, sig = self.exit_code.code_and_signal()
        array_job = self.array_job_id.value
        task = self.array_task_id.value if task_id is None else task_id
        return SlurmJobInfo(
            job_id=JobId(str(self.job_id)),
            state=self.job_state[0],
            flags=frozenset(self.job_state[1:]),
            reason=_reason(self.state_reason),
            exit_code=code,
            signal=sig,
            array_job_id=JobId(str(array_job)) if array_job else None,
            array_task_id=task if array_job else None,
            node=_node(self.nodes),
            source="squeue",
        )


class SacctState(_Model):
    current: list[str] = Field(min_length=1)
    reason: str = "None"

    @field_validator("current", mode="before")
    @classmethod
    def _coerce_state(cls, raw: object) -> object:
        return _state_list(raw)


class SacctArray(_Model):
    job_id: int = 0
    task_id: OptionalNumber = OptionalNumber()
    task: str = ""

    @field_validator("task_id", mode="before")
    @classmethod
    def _coerce_task(cls, raw: object) -> object:
        return _number(raw)


class SacctJob(_Model):
    job_id: int
    state: SacctState
    array: SacctArray = SacctArray()
    exit_code: ExitCode = ExitCode()
    name: str = ""
    nodes: str = ""

    def info(self, *, task_id: int | None = None) -> SlurmJobInfo:
        code, sig = self.exit_code.code_and_signal()
        array_job = self.array.job_id
        task = self.array.task_id.value if task_id is None else task_id
        return SlurmJobInfo(
            job_id=JobId(str(self.job_id)),
            state=self.state.current[0],
            flags=frozenset(self.state.current[1:]),
            reason=_reason(self.state.reason),
            exit_code=code,
            signal=sig,
            array_job_id=JobId(str(array_job)) if array_job else None,
            array_task_id=task if array_job else None,
            node=_node(self.nodes),
            source="sacct",
        )


class SlurmVersion(_Model):
    major: str | int = ""
    minor: str | int = ""
    micro: str | int = ""


class MetaSlurm(_Model):
    version: SlurmVersion = SlurmVersion()
    release: str = ""


class Meta(_Model):
    slurm: MetaSlurm = MetaSlurm()


class _Response(_Model):
    meta: Meta = Meta()


class SqueueResponse(_Response):
    jobs: list[SqueueJob]


class SacctResponse(_Response):
    jobs: list[SacctJob]


def slurm_release(meta: object) -> str:
    """The SLURM release named in a raw response's `meta`, or "unknown"."""
    try:
        parsed = Meta.model_validate(meta)
    except ValueError:
        return "unknown"
    if parsed.slurm.release:
        return parsed.slurm.release
    v = parsed.slurm.version
    if v.major != "":
        return f"{v.major}.{v.minor}.{v.micro}"
    return "unknown"


# --- array index expressions -----------------------------------------------------------------

_RANGE = re.compile(r"^(\d+)(?:-(\d+)(?::(\d+))?)?$")


def array_spec_contains(spec: str, task: int) -> bool:
    """Whether `task` is in a SLURM array expression like "2-9%2", "1,3,5-7" or "0-15:4%2"."""
    body = spec.split("%", 1)[0]
    for part in body.split(","):
        m = _RANGE.match(part.strip())
        if m is None:
            continue
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) is not None else lo
        step = int(m.group(3)) if m.group(3) is not None else 1
        if lo <= task <= hi and (task - lo) % max(step, 1) == 0:
            return True
    return False
