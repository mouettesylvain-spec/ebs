"""The FakeSlurm commands: sbatch, squeue, sacct, scancel, scontrol (the subset ebs uses).

Options and messages follow SLURM 26.05; unknown options are rejected (exit 2) so that a change in
the adapter's argv cannot go unnoticed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tests.fakes.slurm.core import (
    RELEASE,
    TERMINAL,
    State,
    kill_group,
    locked,
    meta,
    sacct_job,
    schedule,
    squeue_job,
    visible_records,
)


@dataclass(frozen=True)
class Output:
    stdout: str
    stderr: str = ""
    code: int = 0


class SlurmError(Exception):
    """Print `message` on stderr and exit with `code`."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:  # argparse exits 2 like the real tools on bad usage
        raise SlurmError(f"{self.prog}: error: {message}", 2)


# --- parsing helpers -------------------------------------------------------------------------

_TIME = re.compile(r"^(?:(\d+)-)?(\d+)(?::(\d+))?(?::(\d+))?$")


def parse_time(text: str) -> int:
    """SLURM --time formats -> seconds."""
    m = _TIME.match(text)
    if m is None:
        raise SlurmError("sbatch: error: Invalid --time specification")
    days, a, b, c = m.groups()
    if days is not None:  # D-H, D-H:M, D-H:M:S
        h, mi, s = int(a), int(b or 0), int(c or 0)
        return int(days) * 86400 + h * 3600 + mi * 60 + s
    if c is not None:  # H:M:S
        return int(a) * 3600 + int(b) * 60 + int(c)
    if b is not None:  # M:S
        return int(a) * 60 + int(b)
    return int(a) * 60  # M


def parse_mem(text: str) -> int:
    m = re.match(r"^(\d+)([KMGT]?)B?$", text.upper())
    if m is None:
        raise SlurmError("sbatch: error: Invalid --mem specification")
    n, unit = int(m.group(1)), m.group(2) or "M"
    factor = {"K": 1 / 1024, "M": 1, "G": 1024, "T": 1024 * 1024}[unit]
    return max(1, int(n * factor))


def parse_array(text: str) -> tuple[list[int], int | None]:
    body, _, throttle = text.partition("%")
    ids: list[int] = []
    for part in body.split(","):
        m = re.match(r"^(\d+)(?:-(\d+)(?::(\d+))?)?$", part)
        if m is None:
            raise SlurmError("sbatch: error: Invalid job array specification")
        lo = int(m.group(1))
        hi = int(m.group(2) or lo)
        step = int(m.group(3) or 1)
        ids.extend(range(lo, hi + 1, step))
    if not ids or len(set(ids)) != len(ids):
        raise SlurmError("sbatch: error: Invalid job array specification")
    return sorted(ids), int(throttle) if throttle else None


def parse_licenses(text: str | None) -> dict[str, int]:
    out: dict[str, int] = {}
    if not text:
        return out
    for part in text.split(","):
        name, _, count = part.partition(":")
        if not name or (count and not count.isdigit()):
            raise SlurmError("sbatch: error: Invalid --licenses specification")
        out[name] = out.get(name, 0) + int(count or 1)
    return out


def read_env_file(path: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for entry in Path(path).read_bytes().split(b"\x00"):
        if entry:
            name, _, value = entry.decode().partition("=")
            env[name] = value
    return env


def _refs(text: str | None) -> list[tuple[int, int | None]]:
    out: list[tuple[int, int | None]] = []
    if not text:
        return out
    for part in text.split(","):
        job, _, task = part.partition("_")
        if not job.isdigit() or (task and not task.isdigit()):
            raise SlurmError(f"Invalid job id: {part}")
        out.append((int(job), int(task) if task else None))
    return out


def _matches(row: Any, refs: list[tuple[int, int | None]]) -> bool:
    for job, task in refs:
        if task is None and (row["id"] == job or row["array_job_id"] == job):
            return True
        if task is not None and row["array_job_id"] == job and row["array_task_id"] == task:
            return True
    return False


# --- sbatch ----------------------------------------------------------------------------------


def sbatch(st: State, argv: Sequence[str]) -> str:
    p = _Parser(prog="sbatch", add_help=False)
    p.add_argument("--parsable", action="store_true")
    p.add_argument("-J", "--job-name")
    p.add_argument("-p", "--partition")
    p.add_argument("-A", "--account")
    p.add_argument("-q", "--qos")
    p.add_argument("-c", "--cpus-per-task", type=int, default=1)
    p.add_argument("--mem")
    p.add_argument("-t", "--time")
    p.add_argument("-L", "--licenses")
    p.add_argument("-C", "--constraint", default="")
    p.add_argument("-a", "--array")
    p.add_argument("-o", "--output")
    p.add_argument("-e", "--error")
    p.add_argument("-D", "--chdir")
    p.add_argument("--export")
    p.add_argument("--export-file")
    p.add_argument("--requeue", dest="requeue", action="store_true", default=False)
    p.add_argument("--no-requeue", dest="requeue", action="store_false")
    p.add_argument("script")
    a = p.parse_args(argv)
    cfg = st.config

    script = Path(a.script)
    try:
        text = script.read_bytes()
    except OSError as exc:
        raise SlurmError(f"sbatch: error: Unable to open file {a.script}") from exc
    if not text.startswith(b"#!"):
        raise SlurmError(
            "sbatch: error: This does not look like a batch script.  The first\n"
            "sbatch: error: line must start with #! followed by the path to an interpreter."
        )
    fail = "sbatch: error: Batch job submission failed: "
    if "accounts" in cfg and a.account is not None and a.account not in cfg["accounts"]:
        raise SlurmError(fail + "Invalid account or account/partition combination specified")
    if "qos" in cfg and a.qos is not None and a.qos not in cfg["qos"]:
        raise SlurmError(fail + "Invalid qos specification")
    if "partitions" in cfg and a.partition is not None and a.partition not in cfg["partitions"]:
        raise SlurmError(fail + "Invalid partition name specified")
    licenses = parse_licenses(a.licenses)
    for name, n in licenses.items():
        if n > int(cfg["licenses"].get(name, 0)):
            raise SlurmError(fail + "Invalid license specification")
    if a.cpus_per_task > int(cfg["cpus"]):
        raise SlurmError(fail + "Requested node configuration is not available")
    time_s = parse_time(a.time) if a.time else None
    mem_mb = parse_mem(a.mem) if a.mem else None
    tasks, throttle = parse_array(a.array) if a.array else ([], None)

    if a.export is None or a.export.upper() == "ALL":
        env = dict(os.environ)
    elif a.export.upper() == "NONE":
        env = {k: v for k, v in os.environ.items() if k.startswith("SLURM_")}
    else:
        raise SlurmError("sbatch: error: the fake supports --export=ALL|NONE only")
    if a.export_file:
        env.update(read_env_file(a.export_file))

    workdir = str(Path(a.chdir).absolute() if a.chdir else Path.cwd())
    default_out = "slurm-%A_%a.out" if a.array else "slurm-%j.out"
    # An array's own id is its meta record's; each task gets a further id, as when SLURM splits
    # a task off to run it, so no two records ever share a job id.
    n = len(tasks) + 1 if a.array else 1
    first = st.allocate_ids(n)
    scripts = st.root / "scripts"
    scripts.mkdir(exist_ok=True)
    stored = scripts / f"{first}.sh"
    shutil.copyfile(script, stored)
    stored.chmod(0o700)
    now = time.time()
    rows: list[tuple[int, int | None, int | None]] = [(first, None, None)]
    if a.array:
        rows = [(first + 1 + i, first, t) for i, t in enumerate(tasks)]
    for job_id, array_job, task in rows:
        st.db.execute(
            "INSERT INTO jobs (id, array_job_id, array_task_id, throttle, name, script, workdir,"
            " stdout, stderr, env, cpus, mem_mb, time_s, licenses, partition, account, qos,"
            " constraints, requeue, state, reason, submit_ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'PENDING','None',?)",
            (
                job_id,
                array_job,
                task,
                throttle,
                a.job_name or script.name,
                str(stored),
                workdir,
                a.output or default_out,
                a.error,
                json.dumps(env),
                a.cpus_per_task,
                mem_mb,
                time_s,
                json.dumps(licenses),
                a.partition or "p1",
                a.account or "a1",
                a.qos or "normal",
                a.constraint,
                int(a.requeue),
                now,
            ),
        )
    schedule(st)
    return f"{first}\n" if a.parsable else f"Submitted batch job {first}\n"


# --- squeue / sacct --------------------------------------------------------------------------


def _query_args(prog: str, argv: Sequence[str], *, squeue: bool) -> argparse.Namespace:
    p = _Parser(prog=prog, add_help=False)
    p.add_argument("--json", action="store_true")
    p.add_argument("-j", "--jobs")
    if squeue:
        p.add_argument("-t", "--states")
    else:
        p.add_argument("-X", "--allocations", action="store_true")
        p.add_argument("-S", "--starttime")
    return p.parse_args(argv)


def squeue(st: State, argv: Sequence[str]) -> str:
    a = _query_args("squeue", argv, squeue=True)
    schedule(st)
    refs = _refs(a.jobs)
    states = {s.strip().upper() for s in (a.states or "").split(",") if s.strip()}
    horizon = time.time() - float(st.config["min_job_age_s"])
    known = [
        r
        for r in st.all_jobs()
        if r["state"] not in TERMINAL or (r["end_ts"] is not None and r["end_ts"] >= horizon)
    ]
    if refs:
        known = [r for r in known if _matches(r, refs)]
        if not known:
            raise SlurmError("slurm_load_jobs error: Invalid job id specified")
    if "ALL" not in states:
        wanted = states or {"PENDING", "RUNNING", "SUSPENDED", "COMPLETING", "CONFIGURING"}
        known = [r for r in known if r["state"] in wanted]
    jobs = [squeue_job(row, ts) for row, ts in visible_records(known)]
    if a.json:
        doc = {"jobs": jobs, "meta": meta(["squeue", *argv]), "errors": [], "warnings": []}
        return json.dumps(doc, indent=2) + "\n"
    lines = ["JOBID STATE REASON NAME"]
    lines += [f"{j['job_id']} {j['job_state'][0]} {j['state_reason']} {j['name']}" for j in jobs]
    return "\n".join(lines) + "\n"


def sacct(st: State, argv: Sequence[str]) -> str:
    a = _query_args("sacct", argv, squeue=False)
    schedule(st)
    refs = _refs(a.jobs)
    rows = [r for r in st.all_jobs() if not refs or _matches(r, refs)]
    jobs = [sacct_job(row, ts) for row, ts in visible_records(rows)]
    if a.json:
        doc = {"jobs": jobs, "meta": meta(["sacct", *argv]), "errors": [], "warnings": []}
        return json.dumps(doc, indent=2) + "\n"
    lines = ["JobID State ExitCode"]
    lines += [
        f"{j['job_id']} {j['state']['current'][0]} {j['exit_code']['return_code']['number']}"
        for j in jobs
    ]
    return "\n".join(lines) + "\n"


# --- scancel / scontrol ----------------------------------------------------------------------


def scancel(st: State, argv: Sequence[str]) -> str | Output:
    p = _Parser(prog="scancel", add_help=False)
    p.add_argument("-s", "--signal")
    p.add_argument("ids", nargs="+")
    a = p.parse_args(argv)
    errors: list[str] = []
    for text in a.ids:
        refs = _refs(text)
        rows = [r for r in st.all_jobs() if _matches(r, refs)]
        if not rows:
            errors.append(
                f"scancel: error: Kill job error on job id {text}: Invalid job id specified"
            )
            continue
        for row in rows:
            if row["state"] in TERMINAL:
                errors.append(
                    f"scancel: error: Kill job error on job id {text}: "
                    "Job/step already completing or completed"
                )
            elif a.signal is not None:
                kill_group(row["job_pgid"], signal.Signals["SIG" + a.signal.removeprefix("SIG")])
            elif row["state"] == "PENDING":
                st.db.execute(
                    "UPDATE jobs SET state = 'CANCELLED', end_ts = ?, exit_code = 0, signal = 0"
                    " WHERE id = ?",
                    (time.time(), row["id"]),
                )
            else:
                st.db.execute("UPDATE jobs SET cancel_requested = 1 WHERE id = ?", (row["id"],))
    schedule(st)
    if errors:  # returned, not raised: the cancellations above must be committed
        return Output("", "\n".join(dict.fromkeys(errors)) + "\n", 1)
    return ""


def scontrol(st: State, argv: Sequence[str]) -> str:
    del st
    if list(argv) == ["--version"]:
        return f"slurm {RELEASE}\n"
    if list(argv) == ["ping"]:
        return "Slurmctld(primary) at fake is UP\n"
    raise SlurmError(f"scontrol: error: the fake does not implement {' '.join(argv)!r}", 2)


COMMANDS: dict[str, Callable[[State, Sequence[str]], str | Output]] = {
    "sbatch": sbatch,
    "squeue": squeue,
    "sacct": sacct,
    "scancel": scancel,
    "scontrol": scontrol,
}


def main(command: str, argv: Sequence[str], state_dir: str) -> int:
    """Entry point of the installed `sbatch`, `squeue`, … scripts."""
    root = Path(os.environ.get("FAKESLURM_STATE", state_dir))
    try:
        with locked(root) as st:
            fault = st.consume_fault("CLI_ERROR", command=command)
            if fault is not None:
                sys.stderr.write(str(fault.get("message", f"{command}: error: injected")) + "\n")
                return int(fault.get("exit_code", 1))
            out = COMMANDS[command](st, argv)
    except SlurmError as exc:
        sys.stderr.write(exc.message + "\n")
        return exc.code
    result = out if isinstance(out, Output) else Output(out)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.code
