"""Runs one FakeSlurm job: the batch script in its own session, with limits and faults.

Started detached by `core.schedule`; outlives the command that started it. Writes the final
state, then runs a scheduling pass so pending jobs start without waiting for the next command.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

from tests.fakes.slurm.core import NODE, cancel_marker, finish, kill_group, locked, schedule

TYPE_CHECKING = False  # see core.py
if TYPE_CHECKING:
    from typing import Any

_PAGE_KB = os.sysconf("SC_PAGE_SIZE") // 1024


def group_rss_kb(pgid: int) -> int:
    """Resident memory of every process in process group `pgid`, like the cgroup counter."""
    total = 0
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
        except OSError:
            continue
        # fields[0] is field 3 (state): pgrp is field 5, rss (pages) field 24
        if int(fields[2]) == pgid:
            total += int(fields[21]) * _PAGE_KB
    return total


def _expand(pattern: str, row: Any) -> str:
    array = row["array_job_id"]
    values = {
        "j": str(row["id"]),
        "A": str(array if array is not None else row["id"]),
        "a": str(row["array_task_id"] if array is not None else 4294967294),
        "x": str(row["name"]),
        "N": NODE,
        "%": "%",
    }
    out: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "%" and i + 1 < len(pattern) and pattern[i + 1] in values:
            out.append(values[pattern[i + 1]])
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _job_env(row: Any) -> dict[str, str]:
    env: dict[str, str] = json.loads(row["env"])
    env.update(
        {
            "SLURM_JOB_ID": str(row["id"]),
            "SLURM_JOBID": str(row["id"]),
            "SLURM_JOB_NAME": str(row["name"]),
            "SLURM_CPUS_PER_TASK": str(row["cpus"]),
            "SLURM_JOB_PARTITION": str(row["partition"]),
            "SLURM_JOB_ACCOUNT": str(row["account"]),
            "SLURM_JOB_QOS": str(row["qos"]),
            "SLURM_SUBMIT_DIR": str(row["workdir"]),
            "SLURM_JOB_NODELIST": NODE,
            "SLURMD_NODENAME": NODE,
        }
    )
    if row["mem_mb"] is not None:
        env["SLURM_MEM_PER_NODE"] = str(row["mem_mb"])
    if row["array_job_id"] is not None:
        env["SLURM_ARRAY_JOB_ID"] = str(row["array_job_id"])
        env["SLURM_ARRAY_TASK_ID"] = str(row["array_task_id"])
    return env


def _stop(proc: subprocess.Popen[bytes], first: int, kill_wait_s: float) -> None:
    kill_group(proc.pid, first)
    if first == signal.SIGKILL:
        return
    deadline = time.monotonic() + kill_wait_s
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    kill_group(proc.pid, signal.SIGKILL)


def main(state_dir: str, job_id: int) -> None:
    root = Path(state_dir)
    with locked(root) as st:
        st.db.execute("UPDATE jobs SET supervisor_pid = ? WHERE id = ?", (os.getpid(), job_id))
        row = st.job(job_id)
        if row is None or row["state"] != "RUNNING":
            return
        config = st.config
        workdir = Path(row["workdir"])
        stdout = workdir / _expand(str(row["stdout"]), row)
        stderr = workdir / _expand(str(row["stderr"]), row) if row["stderr"] else None
        fault: dict[str, Any] | None = json.loads(row["fault"]) if row["fault"] else None
        time_limit = float(row["time_s"]) * float(config["time_scale"]) if row["time_s"] else None
        mem_kb = int(row["mem_mb"]) * 1024 if row["mem_mb"] else None
        script = str(row["script"])
        env = _job_env(row)
        try:
            out = open(stdout, "wb")  # noqa: SIM115 - closed below
            err = open(stderr, "wb") if stderr is not None else None  # noqa: SIM115
        except OSError:  # like slurmstepd: an unopenable log fails the job, not the node
            finish(st, job_id, "FAILED", 1, 0)
            schedule(st)
            return
        try:
            proc = subprocess.Popen(
                [script],
                cwd=workdir,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err if err is not None else subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            out.write(f"slurmstepd: error: execve(): {script}: {exc}\n".encode())
            out.close()
            if err is not None:
                err.close()
            finish(st, job_id, "FAILED", 1, 0)
            schedule(st)
            return
        st.db.execute("UPDATE jobs SET job_pgid = ? WHERE id = ?", (proc.pid, job_id))
    out.close()
    if err is not None:
        err.close()

    kill_wait = float(config["kill_wait_s"])
    poll_s = float(config["poll_s"])
    started = time.monotonic()
    outcome: tuple[str, str] | None = None  # (state, reason)
    while proc.poll() is None:
        time.sleep(poll_s)
        elapsed = time.monotonic() - started
        # scancel leaves a marker: checking it needs no lock, so hundreds of running jobs do
        # not starve the commands of the lock.
        if cancel_marker(root, job_id).exists():
            outcome = ("CANCELLED", "None")
            _stop(proc, signal.SIGTERM, kill_wait)
        elif time_limit is not None and elapsed > time_limit:
            outcome = ("TIMEOUT", "TimeLimit")
            _stop(proc, signal.SIGTERM, kill_wait)
        elif mem_kb is not None and group_rss_kb(proc.pid) > mem_kb:
            outcome = ("OUT_OF_MEMORY", "OutOfMemory")
            _stop(proc, signal.SIGKILL, kill_wait)
        elif fault is not None and elapsed >= float(fault.get("after_s", 0.0)):
            if fault["fault"] == "NODE_FAIL":
                outcome = ("NODE_FAIL", "NodeDown")
                _stop(proc, signal.SIGKILL, kill_wait)
            else:
                outcome = ("PREEMPTED", "None")
                _stop(proc, signal.SIGTERM, kill_wait)
        if outcome is not None:
            break
    rc = proc.wait()
    kill_group(proc.pid, signal.SIGKILL)  # leftovers of the job's session
    code, sig = (rc, 0) if rc >= 0 else (0, -rc)
    state, reason = outcome or (("COMPLETED", "None") if rc == 0 else ("FAILED", "NonZeroExitCode"))
    with locked(root) as st:
        finish(st, job_id, state, code, sig, reason)
        schedule(st)
