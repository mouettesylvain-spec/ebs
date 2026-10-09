"""`ebs plan/build/status/logs` as a user runs them: console script, PostgreSQL, local executor
and real `ebs-runner` processes (P0-16)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from sqlalchemy.engine import URL

from tests.helpers.pg import _pg_truncate_engine, pg_migrated, pg_server, pg_url, url_string

__all__ = ["_pg_truncate_engine", "pg_migrated", "pg_server", "pg_url"]

FLOW = """\
version: 1
project: demo
domain: e2e
steps:
  gen:
    kind: shell
    script: "echo generating; cat src/a.txt > out.txt"
    inputs: { src: "src/*.txt" }
    outputs: { out: out.txt }
  sim:
    kind: shell
    matrix: { table: tests.csv }
    script: "echo running ${row.test}; cat ${steps.gen.outputs.out} | tee result.txt"
    outputs: { result: result.txt }
"""


def _ebs(cwd: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    exe = Path(sys.executable).parent / "ebs"
    return subprocess.run(
        [str(exe), *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=120,
        check=False,
    )  # fmt: skip


def _json(proc: subprocess.CompletedProcess[str]) -> Any:
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# R1 R2 R3 R4
def test_plan_build_status_logs(tmp_path: Path, pg_url: URL) -> None:
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "flow.yaml").write_text(FLOW)
    (proj / "tests.csv").write_text("test\nsmoke\nrandom\n")
    (proj / "src" / "a.txt").write_text("hello\n")
    config = tmp_path / "config.toml"
    config.write_text(
        f'[cas]\nroot = "{tmp_path / "cas"}"\n'
        f'[scratch]\ndir = "{tmp_path / "scratch"}"\n'
        f'[stat_cache]\npath = "{tmp_path / "statcache.sqlite"}"\n'
        f'[metadata]\nurl = "{url_string(pg_url)}"\n'
    )
    env = {
        "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}",
        "HOME": str(tmp_path / "home"),
        "USER": "alice",
        "EBS_CONFIG": str(config),
        "NO_COLOR": "1",
    }

    # R1: nothing cached yet; sim waits for gen's output
    plan = _json(_ebs(proj, env, "plan", "--json"))
    assert plan["totals"] == {"actions": 3, "hit": 0, "miss": 1, "unknown": 2}

    # R2: UUID first, events, exit 0
    build = _ebs(proj, env, "build", "--cache", "write")
    assert build.returncode == 0, build.stdout + build.stderr
    lines = build.stdout.splitlines()
    first = re.fullmatch(r"build ([0-9a-f-]{36}) \(id \d+\)", lines[0])
    assert first, lines[0]
    uuid = first.group(1)
    assert lines[-1].startswith(f"build {uuid}: build passed")

    # R3
    status = _json(_ebs(proj, env, "status", "--json"))
    assert status["uuid"] == uuid
    assert status["status"] == "passed"
    assert status["totals"] == {"done": 3}

    # R4: the tool log from the CAS, by unique prefix
    logs = _ebs(proj, env, "logs", "sim[test=sm")
    assert logs.returncode == 0, logs.stderr
    assert logs.stdout == "running smoke\nhello\n"

    # R1: second plan predicts 100 % hits (keys of sim refined through gen's cached output)
    again = _json(_ebs(proj, env, "plan", "--json"))
    assert again["totals"] == {"actions": 3, "hit": 3, "miss": 0, "unknown": 0}
    assert again["baseline"]["build"] == uuid

    rebuilt = _ebs(proj, env, "build", "--json")
    assert rebuilt.returncode == 0, rebuilt.stderr
    assert json.loads(rebuilt.stdout.splitlines()[-1])["counts"] == {"cached": 3}

    # R1: a source edit is explained
    (proj / "src" / "a.txt").write_text("changed\n")
    changed = _ebs(proj, env, "plan")
    assert changed.returncode == 0, changed.stderr
    assert 'gen: inputs["src/a.txt"]' in changed.stdout
