"""A scripted stand-in for `ebs-runner`, started by executor tests as `python fake_runner.py …`.

Stdlib only. Behaviour per action comes from the JSON file named by `$EBS_FAKE_RUNNER_SCRIPT`:
`{action_id: {"exit": int, "hold": bool, "stderr": str, "signal": int, "cpus": int}}` (all
optional; default: exit 0 at once). A held fake waits until the test creates `release/<id>`.
Under `$EBS_FAKE_RUNNER_RECORD` it leaves:

- `argv/<id>.json`: its argv; `running/<id>`: present while it runs, holding its cpus;
- `starts.jsonl`: one `{"action", "load"}` line per start, `load` = cpus of all running fakes;
- `cleaned/<id>`: written when SIGTERM arrives, before it exits 75 (like the real runner).

`<id>` is the SHA-1 of the action id (`record_name`).
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
import time
from pathlib import Path
from types import FrameType

SCRIPT_ENV = "EBS_FAKE_RUNNER_SCRIPT"
RECORD_ENV = "EBS_FAKE_RUNNER_RECORD"
PATH = Path(__file__).resolve()


def record_name(action_id: str) -> str:
    return hashlib.sha1(action_id.encode()).hexdigest()


def _action(argv: list[str]) -> str:
    return argv[argv.index("--action") + 1]


def _cpus(path: Path) -> int:
    """Cpus of a running fake; 0 if it finished meanwhile (or has not written them yet)."""
    try:
        return int(path.read_text() or "0")
    except FileNotFoundError:
        return 0


def main() -> int:
    argv = sys.argv[1:]
    action = _action(argv)
    name = record_name(action)
    script = json.loads(Path(os.environ[SCRIPT_ENV]).read_text())
    behaviour: dict[str, object] = script.get(action, {})
    record = Path(os.environ[RECORD_ENV])
    for sub in ("argv", "running", "cleaned", "release"):
        (record / sub).mkdir(parents=True, exist_ok=True)
    running = record / "running" / name

    def on_term(signum: int, frame: FrameType | None) -> None:
        (record / "cleaned" / name).write_text("cleaned")
        running.unlink(missing_ok=True)
        os._exit(75)

    signal.signal(signal.SIGTERM, on_term)
    (record / "argv" / f"{name}.json").write_text(json.dumps(argv))
    cpus = behaviour.get("cpus", 1)
    running.write_text(str(cpus))
    load = sum(_cpus(p) for p in (record / "running").iterdir())
    with (record / "starts.jsonl").open("a") as f:
        f.write(json.dumps({"action": action, "load": load}) + "\n")
    if behaviour.get("hold"):
        release = record / "release" / name
        while not release.exists():
            time.sleep(0.01)
    stderr = behaviour.get("stderr")
    if isinstance(stderr, str):
        sys.stderr.write(stderr)
        sys.stderr.flush()
    running.unlink(missing_ok=True)
    sig = behaviour.get("signal")
    if isinstance(sig, int):
        if sig != signal.SIGKILL:
            signal.signal(sig, signal.SIG_DFL)
        os.kill(os.getpid(), sig)
    code = behaviour.get("exit", 0)
    assert isinstance(code, int)
    return code


if __name__ == "__main__":
    sys.exit(main())
