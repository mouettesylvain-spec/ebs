"""FakeSlurm state, scheduling and JSON rendering (docs/design/testing.md § FakeSlurm, P1-01 R5).

There is no daemon. All state lives in `<state>/state.db` (SQLite). Every command, and every job
supervisor when its job ends, takes the exclusive lock `<state>/lock`, runs one scheduling pass
(`schedule`) and releases it. A started job gets a detached supervisor process
(`supervisor.py`) that runs the batch script in its own session and enforces `--time` and
`--mem`, cancellations and injected faults.

Configuration, `<state>/config.json` (all optional):
    cpus            CPU slots of the whole fake cluster (default 4)
    licenses        {"name": count} license pools; `-L name:n` against an unknown name fails
    accounts, qos, partitions   allowed values; absent = anything goes
    time_scale      real seconds per SLURM second for --time (default 1.0)
    kill_wait_s     SIGTERM -> SIGKILL grace, like KillWait (default 0.5)
    poll_s          supervisor polling period (default 0.05)
    min_job_age_s   how long squeue still shows ended jobs, like MinJobAge (default 300)
    first_job_id    (default 1000)

Faults, `<state>/faults.json`: a list of objects, re-read on every use.
    {"fault": "NODE_FAIL" | "PREEMPTED", "job_name": glob, "after_s": float, "count": n}
        kill a matching job `after_s` after it starts (SIGKILL / SIGTERM then SIGKILL).
    {"fault": "STUCK_LICENSES", "job_name": glob}
        matching jobs stay PENDING (Licenses) while the fault is present.
    {"fault": "CLI_ERROR", "command": name, "message": text, "count": n, "exit_code": 1}
        the next n invocations of `command` print `message` on stderr and fail.
`count` (default: unlimited) is decremented each time the fault fires.
"""

from __future__ import annotations

import contextlib
import fcntl
import fnmatch
import json
import os
import pwd
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RELEASE = "26.05.0"
CLUSTER = "fake"
NODE = "n001"
REPO_ROOT = Path(__file__).resolve().parents[3]

TERMINAL = frozenset(
    {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED"}
)
DEFAULT_CONFIG: dict[str, Any] = {
    "cpus": 4,
    "licenses": {},
    "time_scale": 1.0,
    "kill_wait_s": 0.5,
    "poll_s": 0.05,
    "min_job_age_s": 300.0,
    "first_job_id": 1000,
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    array_job_id INTEGER,
    array_task_id INTEGER,
    throttle INTEGER,
    name TEXT NOT NULL,
    script TEXT NOT NULL,
    workdir TEXT NOT NULL,
    stdout TEXT NOT NULL,
    stderr TEXT,
    env TEXT NOT NULL,
    cpus INTEGER NOT NULL,
    mem_mb INTEGER,
    time_s INTEGER,
    licenses TEXT NOT NULL,
    partition TEXT NOT NULL,
    account TEXT NOT NULL,
    qos TEXT NOT NULL,
    constraints TEXT NOT NULL,
    requeue INTEGER NOT NULL,
    state TEXT NOT NULL,
    reason TEXT NOT NULL,
    exit_code INTEGER,
    signal INTEGER,
    submit_ts REAL NOT NULL,
    start_ts REAL,
    end_ts REAL,
    supervisor_pid INTEGER,
    job_pgid INTEGER,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    fault TEXT
);
CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
"""


@dataclass(frozen=True)
class State:
    """An open, locked view of the fake cluster."""

    root: Path
    db: sqlite3.Connection
    config: dict[str, Any]

    # faults -------------------------------------------------------------------------------

    def faults(self) -> list[dict[str, Any]]:
        path = self.root / "faults.json"
        if not path.exists():
            return []
        data: list[dict[str, Any]] = json.loads(path.read_text())
        return data

    def consume_fault(
        self, kind: str, *, job_name: str = "", command: str = ""
    ) -> dict[str, Any] | None:
        """The first matching fault; its count goes down by one."""
        faults = self.faults()
        for fault in faults:
            if fault.get("fault") != kind:
                continue
            if command and fault.get("command") != command:
                continue
            if job_name and not fnmatch.fnmatchcase(job_name, str(fault.get("job_name", "*"))):
                continue
            count = fault.get("count")
            if count is not None:
                if int(count) <= 0:
                    continue
                fault["count"] = int(count) - 1
                (self.root / "faults.json").write_text(json.dumps(faults))
            return fault
        return None

    def has_fault(self, kind: str, job_name: str) -> bool:
        return any(
            f.get("fault") == kind and fnmatch.fnmatchcase(job_name, str(f.get("job_name", "*")))
            for f in self.faults()
        )

    # ids ----------------------------------------------------------------------------------

    def allocate_ids(self, n: int) -> int:
        row = self.db.execute("SELECT value FROM counters WHERE name = 'next_id'").fetchone()
        first = int(row[0]) if row else int(self.config["first_job_id"])
        self.db.execute(
            "INSERT OR REPLACE INTO counters (name, value) VALUES ('next_id', ?)", (first + n,)
        )
        return first

    def job(self, job_id: int) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.db.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return row

    def all_jobs(self) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM jobs ORDER BY id"))


def load_config(root: Path) -> dict[str, Any]:
    path = root / "config.json"
    config = dict(DEFAULT_CONFIG)
    if path.exists():
        config.update(json.loads(path.read_text()))
    return config


@contextlib.contextmanager
def locked(root: Path) -> Iterator[State]:
    """Exclusive access to the fake cluster; commits on success."""
    root.mkdir(parents=True, exist_ok=True)
    with open(root / "lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        db = sqlite3.connect(root / "state.db", isolation_level=None, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.executescript(_SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            yield State(root, db, load_config(root))
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        finally:
            db.close()
            fcntl.flock(lock, fcntl.LOCK_UN)


# --- scheduling ------------------------------------------------------------------------------


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - not our process
        return True
    # A zombie still answers kill(0); its /proc state says Z.
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


def kill_group(pgid: int | None, sig: int) -> None:
    if pgid:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, sig)


def finish(
    st: State, job_id: int, state: str, exit_code: int, sig: int, reason: str = "None"
) -> None:
    st.db.execute(
        "UPDATE jobs SET state = ?, exit_code = ?, signal = ?, end_ts = ?, reason = ? WHERE id = ?",
        (state, exit_code, sig, time.time(), reason, job_id),
    )


def schedule(st: State) -> None:
    """One scheduling pass: notice lost supervisors, start every pending job that fits."""
    for row in st.db.execute("SELECT * FROM jobs WHERE state = 'RUNNING'").fetchall():
        pid = row["supervisor_pid"]
        if pid and not _alive(int(pid)):
            kill_group(row["job_pgid"], signal.SIGKILL)
            finish(st, int(row["id"]), "NODE_FAIL", 0, 9, "NodeDown")
    running = st.db.execute("SELECT * FROM jobs WHERE state = 'RUNNING'").fetchall()
    free_cpus = int(st.config["cpus"]) - sum(int(r["cpus"]) for r in running)
    pools: dict[str, int] = {k: int(v) for k, v in st.config["licenses"].items()}
    per_array: dict[int, int] = {}
    for r in running:
        for name, n in json.loads(r["licenses"]).items():
            pools[name] = pools.get(name, 0) - int(n)
        if r["array_job_id"] is not None:
            per_array[int(r["array_job_id"])] = per_array.get(int(r["array_job_id"]), 0) + 1
    pending = st.db.execute("SELECT * FROM jobs WHERE state = 'PENDING' ORDER BY id").fetchall()
    for row in pending:
        job_id = int(row["id"])
        wanted: dict[str, int] = json.loads(row["licenses"])
        array = row["array_job_id"]
        reason = None
        if st.has_fault("STUCK_LICENSES", str(row["name"])):
            reason = "Licenses"
        elif (
            array is not None
            and row["throttle"]
            and per_array.get(int(array), 0) >= int(row["throttle"])
        ):
            reason = "JobArrayTaskLimit"
        elif any(pools.get(name, 0) < n for name, n in wanted.items()):
            reason = "Licenses"
        elif int(row["cpus"]) > free_cpus:
            reason = "Resources"
        if reason is not None:
            st.db.execute("UPDATE jobs SET reason = ? WHERE id = ?", (reason, job_id))
            continue
        free_cpus -= int(row["cpus"])
        for name, n in wanted.items():
            pools[name] -= n
        if array is not None:
            per_array[int(array)] = per_array.get(int(array), 0) + 1
        fault = st.consume_fault("NODE_FAIL", job_name=str(row["name"])) or st.consume_fault(
            "PREEMPTED", job_name=str(row["name"])
        )
        pid = _spawn_supervisor(st.root, job_id)
        st.db.execute(
            "UPDATE jobs SET state = 'RUNNING', reason = 'None', start_ts = ?, "
            "supervisor_pid = ?, fault = ? WHERE id = ?",
            (time.time(), pid, json.dumps(fault) if fault else None, job_id),
        )


_BOOT = (
    "import sys; sys.path.insert(0, {root!r}); "
    "from tests.fakes.slurm.supervisor import main; main(sys.argv[1], int(sys.argv[2]))"
)


def _spawn_supervisor(root: Path, job_id: int) -> int:
    with open(root / "supervisor.log", "ab") as log:
        proc = subprocess.Popen(
            [sys.executable, "-I", "-c", _BOOT.format(root=str(REPO_ROOT)), str(root), str(job_id)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log,
            start_new_session=True,
            close_fds=True,
        )
    proc.returncode = 0  # never waited for here: the supervisor outlives this command
    return proc.pid


# --- rendering -------------------------------------------------------------------------------


def _num(value: int | None) -> dict[str, Any]:
    return {"set": value is not None, "infinite": False, "number": 0 if value is None else value}


def _exit(row: sqlite3.Row) -> dict[str, Any]:
    if row["state"] not in TERMINAL:
        return {
            "status": ["PENDING"],
            "return_code": _num(None),
            "signal": {"id": _num(None), "name": ""},
        }
    rc = int(row["exit_code"] or 0)
    sig = int(row["signal"] or 0)
    status = ["SIGNALED"] if sig else (["SUCCESS"] if rc == 0 else ["ERROR"])
    names = {9: "KILL", 15: "TERM"}
    return {
        "status": status,
        "return_code": _num(rc),
        "signal": {"id": _num(sig or None), "name": names.get(sig, "")},
    }


def _user() -> str:
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:  # pragma: no cover
        return "u1"


def meta(command: list[str]) -> dict[str, Any]:
    return {
        "plugin": {
            "type": "",
            "name": "",
            "data_parser": "data_parser/v0.0.44",
            "accounting_storage": "accounting_storage/slurmdbd",
        },
        "client": {"source": "fake", "user": _user(), "group": ""},
        "command": command,
        "slurm": {
            "version": {"major": "26", "minor": "05", "micro": "0"},
            "release": RELEASE,
            "cluster": CLUSTER,
        },
    }


def compact(ids: list[int]) -> str:
    """[0, 1, 2, 5] -> "0-2,5"."""
    parts: list[str] = []
    ordered = sorted(ids)
    start = prev = ordered[0]
    for i in [*ordered[1:], None]:
        if i is not None and i == prev + 1:
            prev = i
            continue
        parts.append(str(start) if start == prev else f"{start}-{prev}")
        if i is not None:
            start = prev = i
    return ",".join(parts)


def visible_records(rows: list[sqlite3.Row]) -> list[tuple[sqlite3.Row, str]]:
    """Rows as SLURM shows them: unstarted array tasks collapse into one record.

    Returns (row, array_task_string); the string is set only for a collapsed record, whose
    row is the lowest pending task and whose job id is the array's.
    """
    out: list[tuple[sqlite3.Row, str]] = []
    collapsed: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        if (
            row["array_job_id"] is not None
            and row["state"] == "PENDING"
            and row["start_ts"] is None
        ):
            collapsed.setdefault(int(row["array_job_id"]), []).append(row)
        else:
            out.append((row, ""))
    for group in collapsed.values():
        tasks = compact([int(r["array_task_id"]) for r in group])
        throttle = group[0]["throttle"]
        out.append((group[0], f"{tasks}%{throttle}" if throttle else tasks))
    out.sort(key=lambda pair: int(pair[0]["id"]))
    return out


def _record_id(row: sqlite3.Row, task_string: str) -> int:
    return int(row["array_job_id"]) if task_string else int(row["id"])


def squeue_job(row: sqlite3.Row, task_string: str) -> dict[str, Any]:
    array = row["array_job_id"]
    task = None if task_string or array is None else int(row["array_task_id"])
    lic = json.loads(row["licenses"])
    return {
        "account": row["account"],
        "array_job_id": _num(int(array) if array is not None else 0),
        "array_task_id": _num(task),
        "array_max_tasks": _num(int(row["throttle"] or 0)),
        "array_task_string": task_string,
        "batch_flag": True,
        "command": row["script"],
        "constraints": row["constraints"],
        "cpus": _num(int(row["cpus"])),
        "cpus_per_task": _num(int(row["cpus"])),
        "current_working_directory": row["workdir"],
        "end_time": _num(int(row["end_ts"]) if row["end_ts"] else 0),
        "exit_code": _exit(row),
        "job_id": _record_id(row, task_string),
        "job_state": [row["state"]],
        "licenses": ",".join(f"{k}:{v}" for k, v in sorted(lic.items())),
        "memory_per_node": _num(row["mem_mb"]),
        "name": row["name"],
        "nodes": NODE if row["start_ts"] else "",
        "partition": row["partition"],
        "qos": row["qos"],
        "requeue": bool(row["requeue"]),
        "standard_error": row["stderr"] or row["stdout"],
        "standard_output": row["stdout"],
        "start_time": _num(int(row["start_ts"]) if row["start_ts"] else 0),
        "state_reason": row["reason"],
        "submit_time": _num(int(row["submit_ts"])),
        "time_limit": _num((int(row["time_s"]) + 59) // 60 if row["time_s"] else None),
        "user_name": _user(),
    }


def sacct_job(row: sqlite3.Row, task_string: str) -> dict[str, Any]:
    array = row["array_job_id"]
    task = None if task_string or array is None else int(row["array_task_id"])
    start = row["start_ts"]
    end = row["end_ts"]
    return {
        "account": row["account"],
        "array": {
            "job_id": int(array) if array is not None else 0,
            "limits": {"max": {"running": {"tasks": int(row["throttle"] or 0)}}},
            "task": task_string,
            "task_id": _num(task),
        },
        "cluster": CLUSTER,
        "constraints": row["constraints"],
        "exit_code": _exit(row),
        "job_id": _record_id(row, task_string),
        "name": row["name"],
        "nodes": NODE if start else "None assigned",
        "partition": row["partition"],
        "qos": row["qos"],
        "required": {"CPUs": int(row["cpus"]), "memory_per_node": _num(row["mem_mb"])},
        "state": {"current": [row["state"]], "reason": row["reason"]},
        "time": {
            "elapsed": int((end or time.time()) - start) if start else 0,
            "end": int(end or 0),
            "limit": _num((int(row["time_s"]) + 59) // 60 if row["time_s"] else None),
            "start": int(start or 0),
            "submission": int(row["submit_ts"]),
        },
        "user": _user(),
        "working_directory": row["workdir"],
    }
