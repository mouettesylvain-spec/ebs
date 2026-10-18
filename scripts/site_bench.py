"""Measure the examples for the documentation site's landing page (task D-01 R8).

Usage: python scripts/site_bench.py --pg-server URL [--out site/src/data/bench.json]

URL is a PostgreSQL server the script may create a database on (e.g.
`postgresql+psycopg://ebs:ebs@localhost:5432/postgres`); the database is dropped at the end.
Each scenario runs `ebs build --cache write --json` on a fresh copy of an example, against a
fresh CAS, and records how many actions executed and how many came from the cache:

    lint-cold      examples/lint-make, first build
    lint-warm      a fresh copy (a new clone, a CI job): everything is a cache hit
    lint-comment   a comment edit in rtl/alu/alu.sv: one lint reruns, early cutoff on summary
    sim-cold       examples/verilator-sim, first build
    sim-seed       one seed changed in tests.csv: one simulation and the report rerun

For comparison, `make -q` (question mode) on a fresh copy of the lint Makefile says which
targets Make would rebuild: the "make" entry. Output document (v1):

    {"v": 1, "commit": "abc1234", "measured_at": "…Z",
     "scenarios": [{"id", "example", "label", "executed", "cached", "total", "wall_s"}, …],
     "make": {"example": "lint-make", "executed": N, "total": N}}   # lint targets only
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "examples"
BIN_DIR = Path(sys.executable).parent


def _replace(path: str, old: str, new: str) -> Callable[[Path], None]:
    def edit(root: Path) -> None:
        file = root / path
        text = file.read_text(encoding="utf-8")
        if old not in text:
            raise ValueError(f"{path} no longer contains {old!r}: update scripts/site_bench.py")
        file.write_text(text.replace(old, new, 1), encoding="utf-8")

    return edit


@dataclass(frozen=True)
class Scenario:
    id: str
    example: str
    label: str
    edit: Callable[[Path], None] | None = None
    fresh: bool = False  # start from a fresh copy of the example


SCENARIOS: tuple[Scenario, ...] = (
    Scenario("lint-cold", "lint-make", "First build", fresh=True),
    Scenario("lint-warm", "lint-make", "Fresh checkout, cache filled before", fresh=True),
    Scenario(
        "lint-comment",
        "lint-make",
        "Comment edit in one RTL file",
        _replace(
            "rtl/alu/alu.sv",
            "// Combinational ALU: add, subtract, and, or.",
            "// Combinational ALU (add/sub/and/or).",
        ),
    ),
    Scenario("sim-cold", "verilator-sim", "First regression", fresh=True),
    Scenario(
        "sim-seed",
        "verilator-sim",
        "One seed changed",
        _replace("tests.csv", "random,42", "random,43"),
    ),
)


@dataclass(frozen=True)
class BuildCounts:
    executed: int
    cached: int
    total: int


@dataclass(frozen=True)
class ScenarioResult:
    scenario: Scenario
    counts: BuildCounts
    wall_s: float


def parse_build(stdout: str) -> BuildCounts:
    """Counts from `ebs build --json`: events, one per line, then the result document."""
    docs = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    if not docs:
        raise ValueError("ebs build printed nothing")
    result, events = docs[-1], docs[:-1]
    if result.get("status") != "passed":
        raise ValueError(f"the build failed: {result}")
    executed = {e["action_id"] for e in events if e.get("type") == "submitted"}
    cached = {e["action_id"] for e in events if e.get("type") == "cache_hit"}
    return BuildCounts(len(executed), len(cached), len(executed | cached))


def bench_document(
    commit: str,
    measured_at: str,
    results: Sequence[ScenarioResult],
    *,
    make_executed: int,
    make_total: int,
) -> dict[str, Any]:
    return {
        "v": 1,
        "commit": commit,
        "measured_at": measured_at,
        "scenarios": [
            {
                "id": r.scenario.id,
                "example": r.scenario.example,
                "label": r.scenario.label,
                "executed": r.counts.executed,
                "cached": r.counts.cached,
                "total": r.counts.total,
                "wall_s": round(r.wall_s, 2),
            }
            for r in results
        ],
        "make": {"example": "lint-make", "executed": make_executed, "total": make_total},
    }


# --- running ------------------------------------------------------------------------------------


def lint_blocks() -> list[str]:
    return (EXAMPLES / "lint-make" / "blocks.csv").read_text(encoding="utf-8").split()[1:]


def make_rebuilds(work: Path) -> int:
    """Lint targets Make would rebuild on a fresh copy of examples/lint-make (`make -q`), out
    of one per block: Make has no shared cache, so a new checkout starts from nothing."""
    example = work / "make-lint"
    shutil.copytree(EXAMPLES / "lint-make", example, ignore=shutil.ignore_patterns(".ebs"))
    stale = 0
    for block in lint_blocks():
        argv = ["make", "-q", "-C", "lint", "lint", f"BLOCK={block}", "LINTER=fake"]
        if subprocess.run(argv, cwd=example, capture_output=True, check=False).returncode != 0:
            stale += 1
    return stale


def run_scenarios(work: Path, pg_url: str) -> list[ScenarioResult]:
    config = work / "config.toml"
    config.write_text(
        f'[cas]\nroot = "{work / "cas"}"\n[scratch]\ndir = "{work / "scratch"}"\n'
        f'[stat_cache]\npath = "{work / "statcache.sqlite"}"\n[metadata]\nurl = "{pg_url}"\n',
        encoding="utf-8",
    )
    env = {
        "PATH": f"{BIN_DIR}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(work / "home"),
        "EBS_CONFIG": str(config),
        "NO_COLOR": "1",
    }
    results = []
    checkouts: dict[str, Path] = {}
    for scenario in SCENARIOS:
        if scenario.fresh:  # a new checkout of its own; later scenarios continue in it
            checkouts[scenario.example] = work / "checkouts" / scenario.id
            shutil.copytree(
                EXAMPLES / scenario.example,
                checkouts[scenario.example],
                ignore=shutil.ignore_patterns(".ebs"),
            )
        example = checkouts[scenario.example]
        if scenario.edit is not None:
            scenario.edit(example)
        start = time.monotonic()
        proc = subprocess.run(
            [str(BIN_DIR / "ebs"), "build", "--cache", "write", "--json"],
            cwd=example, env=env, capture_output=True, text=True, check=False, timeout=600,
        )  # fmt: skip
        wall = time.monotonic() - start
        if proc.returncode != 0:
            raise RuntimeError(f"{scenario.id}: ebs build failed:\n{proc.stdout}{proc.stderr}")
        results.append(ScenarioResult(scenario, parse_build(proc.stdout), wall))
    return results


def _commit() -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True, text=True,
        check=False,
    )  # fmt: skip
    return proc.stdout.strip() or "unknown"


def main(argv: Sequence[str] | None = None) -> int:
    import sqlalchemy as sa

    from ebs.meta import migrations

    parser = argparse.ArgumentParser(description="Measure the examples for the site.")
    parser.add_argument("--pg-server", required=True, help="PostgreSQL server URL (SQLAlchemy)")
    parser.add_argument("--out", type=Path, default=REPO / "site" / "src" / "data" / "bench.json")
    args = parser.parse_args(argv)

    server = sa.make_url(args.pg_server)
    name = f"ebs_bench_{secrets.token_hex(4)}"
    admin = sa.create_engine(server, isolation_level="AUTOCOMMIT", poolclass=sa.pool.NullPool)
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    try:
        url = server.set(database=name).render_as_string(hide_password=False)
        migrations.upgrade(url)
        with tempfile.TemporaryDirectory(prefix="ebs-bench-") as tmp:
            work = Path(tmp)
            results = run_scenarios(work, url)
            make_executed = make_rebuilds(work)
            make_total = len(lint_blocks())
    finally:
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()

    measured_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    doc = bench_document(
        _commit(), measured_at, results, make_executed=make_executed, make_total=make_total
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    for r in results:
        c = r.counts
        counts = f"executed {c.executed}/{c.total}  cached {c.cached}"
        print(f"{r.scenario.id:14} {counts}  {r.wall_s:.1f}s")
    print(f"make (fresh checkout): {make_executed}/{make_total} lint targets rebuilt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
