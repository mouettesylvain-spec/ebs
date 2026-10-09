"""Run `ebs` as a user does on a copy of `examples/` (P0-17): console script, PostgreSQL, local
executor and real `ebs-runner` processes.

`Site` writes an ebs.toml pointing at a private CAS, scratch dir and stat cache under `tmp_path`
and the test database; `Site.ebs` runs the console script in a directory with that config.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.engine import URL

from tests.helpers.pg import url_string

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = REPO_ROOT / "examples"
BIN_DIR = Path(sys.executable).parent


@dataclass(frozen=True, slots=True)
class BuildRun:
    """One `ebs build --json`: its events and the final result document."""

    proc: subprocess.CompletedProcess[str]
    events: list[dict[str, Any]]
    result: dict[str, Any]

    def actions(self, event_type: str) -> set[str]:
        return {e["action_id"] for e in self.events if e["type"] == event_type}

    @property
    def submitted(self) -> set[str]:
        return self.actions("submitted")

    @property
    def cached(self) -> set[str]:
        return self.actions("cache_hit")


@dataclass
class Site:
    """A private EBS site: config file, CAS, scratch, stat cache, all under `root`."""

    root: Path
    pg_url: URL
    extra_config: str = ""
    extra_env: dict[str, str] = field(default_factory=dict)

    @property
    def config(self) -> Path:
        return self.root / "config.toml"

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.config.write_text(
            f'[cas]\nroot = "{self.root / "cas"}"\n'
            f'[scratch]\ndir = "{self.root / "scratch"}"\n'
            f'[stat_cache]\npath = "{self.root / "statcache.sqlite"}"\n'
            f'[metadata]\nurl = "{url_string(self.pg_url)}"\n' + self.extra_config
        )

    def env(self) -> dict[str, str]:
        return {
            "PATH": f"{BIN_DIR}:{os.environ['PATH']}",
            "HOME": str(self.root / "home"),
            "USER": "alice",
            "EBS_CONFIG": str(self.config),
            "NO_COLOR": "1",
            **self.extra_env,
        }

    def ebs(self, cwd: Path, *args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(BIN_DIR / "ebs"), *args], cwd=cwd, env=self.env(), capture_output=True,
            text=True, timeout=timeout, check=False,
        )  # fmt: skip

    def build(self, cwd: Path, *args: str) -> BuildRun:
        """`ebs build --cache write --json`; asserts the build passed."""
        proc = self.ebs(cwd, "build", "--cache", "write", "--json", *args)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return parse_build(proc)


def parse_build(proc: subprocess.CompletedProcess[str]) -> BuildRun:
    docs = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    assert docs, proc.stderr
    return BuildRun(proc=proc, events=docs[:-1], result=docs[-1])


def copy_example(name: str, dest: Path) -> Path:
    """A fresh copy of `examples/<name>` (without any `.ebs/` left by a local run)."""
    target = dest / name
    shutil.copytree(EXAMPLES / name, target, ignore=shutil.ignore_patterns(".ebs"))
    return target
