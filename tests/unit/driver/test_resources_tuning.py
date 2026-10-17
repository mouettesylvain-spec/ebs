"""Memory tuning hints from observed usage (P1-05 R4): a hint, never enforcement."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ebs.driver.resources_tuning import (
    MemoryHint,
    UsageLog,
    UsageSample,
    memory_hints,
    p95,
    usage_path,
)
from ebs.flow.model import Resources
from tests.helpers.cli import FLOW, Site, TestServices, write_site
from tests.helpers.driver import Env, Node, ScriptedExecutor

G = 1 << 30
M = 1 << 20
K = 1 << 10


def _samples(step: str, rss_kb: list[int]) -> list[UsageSample]:
    return [UsageSample(step=step, max_rss_kb=kb, cpu_s=1, wall_s=2) for kb in rss_kb]


# R4
@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([7], 7),
        ([1, 2], 2),
        (list(range(1, 101)), 95),  # nearest rank: ceil(0.95 * 100) = 95th value
        (list(range(100, 0, -1)), 95),  # order does not matter
        ([5] * 19 + [1000], 5),  # one outlier in 20 runs is above the p95
        ([5] * 18 + [1000] * 2, 1000),
    ],
)
def test_p95(values: list[int], expected: int) -> None:
    assert p95(values) == expected


def test_p95_empty() -> None:
    with pytest.raises(ValueError, match="no values"):
        p95([])


# R4
def test_hint(env: Env) -> None:
    plan = env.plan(
        {
            "sim[a]": Node(resources=Resources(mem="16G")),
            "sim[b]": Node(resources=Resources(mem="8G")),
            "lint": Node(resources=Resources(mem="4G")),
            "gen": Node(),  # no request: nothing to tune
            "elab": Node(resources=Resources(mem="32G")),  # too few samples
        }
    )
    usage = {
        "sim": [1 * K * K] * 19 + [3 * K * K],  # p95 = 1G; 16G > 4 x 1G
        "lint": [1 * K * K + 1] * 20,  # 4G is not more than 4 x p95 (1G + 1K): no hint
        "gen": [1] * 20,
        "elab": [1] * 4,
        "other": [1] * 50,  # a step that is not in this plan
    }
    hints = memory_hints(plan, usage)
    assert hints == [
        MemoryHint(step="sim", requested=16 * G, p95=1 * G, samples=20, suggested=1536 * M)
    ]
    (hint,) = hints
    text = hint.message()
    assert "'sim'" in text
    assert "16G" in text
    assert "at most 1G" in text
    assert "20" in text
    assert "resources.mem: 1536M" in text

    # Exactly 4x is not a hint; just above is.
    exact = env.plan({"s": Node(resources=Resources(mem="4G"))})
    assert memory_hints(exact, {"s": [1 * K * K] * 10}) == []
    assert memory_hints(exact, {"s": [1 * K * K - 1] * 10}) != []
    # Thresholds are parameters.
    assert memory_hints(exact, {"s": [1] * 2}, min_samples=2) != []
    assert memory_hints(exact, {"s": [512 * K] * 10}, factor=8) == []


# R4: the usage of successful runs is stored; failures and cache hits add nothing.
def test_usage_recorded_by_build(env: Env) -> None:
    executor = ScriptedExecutor(env.store, env.cas)
    executor.script("ok", "pass")
    executor.script("bad", "fail")
    executor.script("flaky", ("infra", "node_fail"))
    plan = env.plan({"ok": Node(), "bad": Node(), "flaky": Node()})
    env.run(plan, executor, keep_going=True, cache_mode="write", max_retries=0)
    log = UsageLog(usage_path(env.workdir))
    assert log.by_step() == {"ok": [0]}  # ScriptedExecutor reports max_rss_kb = 0

    again = ScriptedExecutor(env.store, env.cas)
    again.script("flaky", ("infra", "node_fail"))
    env.run(plan, again, keep_going=True, cache_mode="write", max_retries=0)
    assert again.runs("ok") == 0  # a cache hit: no new sample
    assert log.by_step() == {"ok": [0]}


# R4
def test_usage_log(tmp_path: Path) -> None:
    log = UsageLog(tmp_path / ".ebs" / "usage.jsonl", max_samples=3)
    assert log.by_step() == {}
    log.record(_samples("sim", [10, 20]))
    log.record(_samples("sim", [30, 40]) + _samples("lint", [5]))
    assert log.by_step() == {"sim": [20, 30, 40], "lint": [5]}  # the most recent per step
    # A damaged line (a crash mid-write) is skipped, not fatal.
    with log.path.open("a") as f:
        f.write('{"step": "sim", "max_rss')
    log.record(_samples("sim", [50]))
    assert log.by_step()["sim"] == [30, 40, 50]
    lines = log.path.read_text().splitlines()
    assert json.loads(lines[0]) == {"step": "sim", "max_rss_kb": 10, "cpu_s": 1, "wall_s": 2}


# R4: hints are advisory: an unreadable or damaged log means no hints, never an error.
def test_usage_log_unreadable(tmp_path: Path) -> None:
    damaged = UsageLog(tmp_path / "damaged.jsonl")
    damaged.path.write_bytes(
        b'\xff\xfe{"step"\n' + b'{"step": "sim", "max_rss_kb": 7, "cpu_s": 1, "wall_s": 1}\n'
    )
    assert damaged.by_step() == {"sim": [7]}
    directory = UsageLog(tmp_path / "adir")
    directory.path.mkdir()
    assert directory.by_step() == {}


# R4: failing to write the log never fails a build, and the build is finished first.
def test_usage_not_recorded_build_still_passes(env: Env) -> None:
    usage_path(env.workdir).mkdir(parents=True)  # a directory where the log should be
    executor = ScriptedExecutor(env.store, env.cas)
    outcome = env.run(env.plan({"ok": Node()}), executor)
    assert outcome.status == "passed"
    assert env.store.get_build(outcome.build).status == "passed"


# R4: the log is compacted so that it does not grow without bound.
def test_usage_log_compaction(tmp_path: Path) -> None:
    log = UsageLog(tmp_path / "usage.jsonl", max_samples=2, max_lines=10)
    for i in range(30):
        log.record(_samples("sim", [i]) + _samples("lint", [100 + i]))
    assert len(log.path.read_text().splitlines()) <= 10
    assert log.by_step() == {"sim": [28, 29], "lint": [128, 129]}


# R4: `ebs plan` prints the hint (and lists it with --json).
def test_plan_prints_hint(tmp_path: Path) -> None:
    flow = FLOW.replace(
        '    params: { test: "${row.test}" }\n',
        '    params: { test: "${row.test}" }\n    resources: { mem: 16G }\n',
    )
    assert flow != FLOW
    write_site(tmp_path, flow=flow)
    site = Site(tmp_path, TestServices(tmp_path))
    UsageLog(usage_path(site.proj)).record(_samples("sim", [512 * K] * 10))
    result = site.invoke(["plan"])
    assert result.exit_code == 0, result.output
    assert "hint: step 'sim' requests 16G" in result.stdout
    doc = json.loads(site.invoke(["plan", "--json"]).stdout)
    assert doc["hints"] == [
        {
            "step": "sim",
            "requested_mem": "16G",
            "p95_mem": "512M",
            "samples": 10,
            "suggested_mem": "768M",
            "message": doc["hints"][0]["message"],
        }
    ]

    # No history, no hint.
    usage_path(site.proj).unlink()
    assert "hint:" not in site.invoke(["plan"]).stdout


# R4: unmeasured usage (0) gives no hint; a measured tiny one suggests at least 1M.
def test_hint_ignores_unmeasured_usage(env: Env) -> None:
    plan = env.plan({"sim": Node(resources=Resources(mem="16G"))})
    assert memory_hints(plan, {"sim": [0] * 20}) == []
    assert memory_hints(plan, {"sim": [0] * 16 + [1] * 4}) == []  # 4 measured < 5
    (hint,) = memory_hints(plan, {"sim": [1] * 20})
    assert hint.suggested == M


# R4: compaction happens just past max_lines and keeps exactly the most recent samples.
def test_usage_log_compaction_exact(tmp_path: Path) -> None:
    log = UsageLog(tmp_path / "usage.jsonl", max_samples=2, max_lines=4)
    for kb in (1, 2, 3, 4):
        log.record(_samples("sim", [kb]))
    assert len(log.path.read_text().splitlines()) == 4  # at the limit: untouched
    log.record(_samples("sim", [5]))
    lines = log.path.read_text().splitlines()
    assert [json.loads(line)["max_rss_kb"] for line in lines] == [4, 5]
