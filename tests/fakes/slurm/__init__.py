"""FakeSlurm: SLURM client commands backed by local processes (docs/design/testing.md, P1-01).

`FakeSlurm.install(dir)` writes executable `sbatch`, `squeue`, `sacct`, `scancel` and `scontrol`
scripts into `dir/bin` (put that first on PATH, or pass it as `SlurmCli(bin_dir=…)`) and keeps the
cluster state in `dir/state`. See `core.py` for the configuration and fault-injection files.
"""

from __future__ import annotations

import json
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ebs.core.clock import Clock
from ebs.exec.slurm.cli import SlurmCli
from tests.fakes.slurm.core import REPO_ROOT, TERMINAL, kill_group, load_config, locked

__all__ = ["COMMAND_NAMES", "FakeSlurm"]

COMMAND_NAMES = ("sbatch", "squeue", "sacct", "scancel", "scontrol")

_ENTRY = """#!{python} -I
import sys
sys.path.insert(0, {root!r})
from tests.fakes.slurm.commands import main
sys.exit(main({command!r}, sys.argv[1:], {state!r}))
"""


@dataclass(frozen=True)
class FakeSlurm:
    root: Path

    @property
    def bin_dir(self) -> Path:
        return self.root / "bin"

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @classmethod
    def install(cls, root: Path, **config: Any) -> FakeSlurm:
        fake = cls(root)
        fake.bin_dir.mkdir(parents=True, exist_ok=True)
        fake.state_dir.mkdir(parents=True, exist_ok=True)
        (root / "scripts").mkdir(exist_ok=True)
        for name in COMMAND_NAMES:
            path = fake.bin_dir / name
            path.write_text(
                _ENTRY.format(
                    python=sys.executable,
                    root=str(REPO_ROOT),
                    command=name,
                    state=str(fake.state_dir),
                )
            )
            path.chmod(0o755)
        if config:
            fake.configure(**config)
        return fake

    def configure(self, **config: Any) -> None:
        """Merge `config` into config.json (keys: see core.py)."""
        path = self.state_dir / "config.json"
        current = json.loads(path.read_text()) if path.exists() else {}
        current.update(config)
        path.write_text(json.dumps(current))

    def inject(self, *faults: dict[str, Any]) -> None:
        """Replace the fault list (no arguments: clear it)."""
        (self.state_dir / "faults.json").write_text(json.dumps(list(faults)))

    def cli(self, clock: Clock | None = None) -> SlurmCli:
        return SlurmCli(bin_dir=self.bin_dir, clock=clock)

    def script(self, name: str, text: str) -> Path:
        """An executable job script under the fake's root."""
        path = self.root / "scripts" / name
        path.write_text(text)
        path.chmod(0o755)
        return path

    def supervisor_pid(self, job_id: str) -> int:
        with locked(self.state_dir) as st:
            row = st.job(int(job_id))
            assert row is not None, f"no job {job_id}"
            assert row["supervisor_pid"], f"job {job_id} has no supervisor"
            return int(row["supervisor_pid"])

    def shutdown(self, timeout: float = 5.0) -> None:
        """Kill every job still running and wait for the supervisors to record it."""
        if not (self.state_dir / "state.db").exists():
            return
        with locked(self.state_dir) as st:
            st.db.execute(
                "UPDATE jobs SET state = 'CANCELLED', end_ts = ? WHERE state = 'PENDING'",
                (time.time(),),
            )
            st.db.execute("UPDATE jobs SET cancel_requested = 1 WHERE state = 'RUNNING'")
            groups = [r["job_pgid"] for r in st.all_jobs() if r["state"] not in TERMINAL]
        kill_wait = float(load_config(self.state_dir)["kill_wait_s"])
        deadline = time.monotonic() + timeout + kill_wait
        while time.monotonic() < deadline:
            with locked(self.state_dir) as st:
                if all(r["state"] in TERMINAL for r in st.all_jobs()):
                    return
            time.sleep(0.05)
        for pgid in groups:
            kill_group(pgid, signal.SIGKILL)
