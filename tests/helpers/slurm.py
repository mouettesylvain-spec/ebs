"""In-process SLURM stand-in for executor unit tests (no subprocesses, no supervisors).

`ScriptedSlurm` is a `CommandRunner` for `SlurmCli`: `sbatch` registers jobs (array tasks
individually), `squeue --json` reports them in the state the test set, `scancel` marks them
CANCELLED. Every argv is recorded, so tests can count calls per command.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ebs.exec.slurm.cli import CommandResult, SlurmCli


@dataclass
class Job:
    job_id: int
    name: str
    comment: str
    argv: list[str]
    array_job_id: int | None = None
    task_id: int | None = None
    state: str = "PENDING"
    reason: str = "Priority"
    exit_code: int = 0
    node: str = ""  # where it runs/ran; "" while pending


def _opt(argv: Sequence[str], name: str) -> str | None:
    prefix = f"--{name}="
    for arg in argv:
        if arg.startswith(prefix):
            return arg[len(prefix) :]
    return None


def _indices(spec: str) -> list[int]:
    body = spec.split("%", 1)[0]
    out: list[int] = []
    for part in body.split(","):
        lo, _, hi = part.partition("-")
        out.extend(range(int(lo), int(hi or lo) + 1))
    return out


def _num(value: int | None) -> dict[str, Any]:
    return {"set": value is not None, "infinite": False, "number": value or 0}


@dataclass
class ScriptedSlurm:
    next_id: int = 100
    jobs: list[Job] = field(default_factory=list)
    calls: list[list[str]] = field(default_factory=list)
    scripts: list[str] = field(default_factory=list)  # job script text, per sbatch call
    fail_sbatch_after: int | None = None  # sbatch calls that succeed before failures start
    sbatch_error: str | None = None  # every sbatch call fails with this message (exit 1)

    def cli(self) -> SlurmCli:
        return SlurmCli(runner=self)

    def commands(self) -> list[str]:
        return [Path(c[0]).name for c in self.calls]

    def count(self, command: str) -> int:
        return self.commands().count(command)

    def __call__(self, argv: Sequence[str], *, timeout_s: float) -> CommandResult:
        argv = list(argv)
        self.calls.append(argv)
        name = Path(argv[0]).name
        if name == "sbatch":
            return self._sbatch(argv)
        if name == "squeue":
            return self._squeue(argv)
        if name == "sacct":
            return CommandResult(0, json.dumps({"jobs": []}), "")
        if name == "scancel":
            return self._scancel(argv)
        raise AssertionError(f"unexpected command {argv}")

    def _sbatch(self, argv: list[str]) -> CommandResult:
        if self.sbatch_error is not None:
            return CommandResult(1, "", self.sbatch_error)
        if self.fail_sbatch_after is not None and self.count("sbatch") > self.fail_sbatch_after:
            return CommandResult(1, "", "sbatch: error: Batch job submission failed: QOS limit")
        self.scripts.append(Path(argv[-1]).read_text())
        job_id = self.next_id
        self.next_id += 1
        name = _opt(argv, "job-name") or ""
        comment = _opt(argv, "comment") or ""
        array = _opt(argv, "array")
        if array is None:
            self.jobs.append(Job(job_id, name, comment, argv))
        else:
            for task in _indices(array):
                self.jobs.append(Job(job_id, name, comment, argv, job_id, task))
        return CommandResult(0, f"{job_id}\n", "")

    def set_state(
        self, job: str, state: str, *, exit_code: int = 0, reason: str = "None", node: str = ""
    ) -> None:
        """Set the state of "<id>" or "<id>_<task>"."""
        for j in self._select(job):
            j.state, j.exit_code, j.reason, j.node = state, exit_code, reason, node

    def _select(self, ref: str) -> list[Job]:
        base, _, task = ref.partition("_")
        return [
            j for j in self.jobs if j.job_id == int(base) and (not task or j.task_id == int(task))
        ]

    def _squeue(self, argv: list[str]) -> CommandResult:
        wanted = {int(x) for x in (_opt(argv, "jobs") or "").split(",") if x}
        records = []
        for j in self.jobs:
            if j.job_id not in wanted:
                continue
            ended = j.state not in {"PENDING", "RUNNING"}
            records.append(
                {
                    "job_id": j.job_id if j.task_id is None else j.job_id * 1000 + j.task_id,
                    "job_state": [j.state],
                    "state_reason": j.reason,
                    "array_job_id": _num(j.array_job_id),
                    "array_task_id": _num(j.task_id),
                    "array_task_string": "",
                    "name": j.name,
                    "nodes": j.node,
                    "exit_code": {
                        "status": ["SUCCESS" if ended else "PENDING"],
                        "return_code": _num(j.exit_code if ended else None),
                        "signal": {"id": _num(None), "name": ""},
                    },
                }
            )
        return CommandResult(0, json.dumps({"jobs": records}), "")

    def _scancel(self, argv: list[str]) -> CommandResult:
        for ref in argv[1:]:
            for j in self._select(ref):
                if j.state in {"PENDING", "RUNNING"}:
                    j.state = "CANCELLED"
        return CommandResult(0, "", "")
