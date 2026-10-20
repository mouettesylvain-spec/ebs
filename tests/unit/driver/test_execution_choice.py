"""The driver picks pilot or direct submission per step (P3-06 R4)."""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from pathlib import Path

import pytest

from ebs.core.errors import ConfigError, ExecutorError
from ebs.driver.execution import (
    RoutingExecutor,
    choose_execution,
    expected_runtime_s,
)
from ebs.driver.resources_tuning import UsageLog, UsageSample
from ebs.exec.api import JobHandle, JobStatus, SubmitBatch
from ebs.exec.pilot.executor import PilotSettings
from ebs.flow.model import Resources
from ebs.meta.api import BuildId
from tests.helpers.executor import PLAN, spec
from tests.helpers.plan import resources
from tests.helpers.runner import DOMAIN


@pytest.mark.parametrize(
    ("mode", "count", "runtime", "expected"),
    [
        ("direct", 10_000, 1, "direct"),  # the step said so
        ("pilot", 1, 3600, "pilot"),
        ("auto", 1, 29, "pilot"),  # short actions
        ("auto", 1, 30, "direct"),
        ("auto", 1, None, "direct"),  # no history
        ("auto", 101, None, "pilot"),  # many actions
        ("auto", 100, 600, "direct"),
        ("auto", 101, 600, "pilot"),
    ],
)
def test_choose_execution(mode: str, count: int, runtime: int | None, expected: str) -> None:
    got = choose_execution(
        mode,  # type: ignore[arg-type]
        count=count,
        expected_runtime_s=runtime,
        threshold=100,
        short_s=30,
    )
    assert got == expected


# R4: the expected runtime is the median wall time of the step's recent runs, given enough.
def test_expected_runtime() -> None:
    assert expected_runtime_s([]) is None
    assert expected_runtime_s([5, 5, 5, 5]) is None  # too few runs say little
    assert expected_runtime_s([1, 2, 3, 100, 200]) == 3
    assert expected_runtime_s([1, 2, 3, 4, 100, 200]) == 4  # upper median


def test_usage_wall_times(tmp_path: Path) -> None:
    log = UsageLog(tmp_path / "usage.jsonl")
    log.record([UsageSample("sim", 10, 1, w) for w in (3, 4, 5)] + [UsageSample("lint", 10, 1, 90)])
    assert log.wall_by_step() == {"sim": [3, 4, 5], "lint": [90]}


def test_pilot_settings() -> None:
    s = PilotSettings.from_config({"pilot": {"threshold": 50, "short_s": 10, "idle_s": 30}})
    assert (s.threshold, s.short_s, s.idle_s) == (50, 10, 30.0)
    assert PilotSettings.from_config({}) == PilotSettings()
    with pytest.raises(ConfigError, match="threshold"):
        PilotSettings.from_config({"pilot": {"threshold": 0}})
    with pytest.raises(ConfigError, match="unknown"):
        PilotSettings.from_config({"pilot": {"bogus": 1}})


def test_resources_execution_field() -> None:
    assert Resources.model_validate({"execution": "pilot"}).execution == "pilot"
    assert Resources().execution is None
    with pytest.raises(ValueError, match="execution"):
        Resources.model_validate({"execution": "sometimes"})


class Recording:
    """An executor that records batches and answers every handle `done`."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.batches: list[SubmitBatch] = []
        self.cancelled: list[JobHandle] = []
        self.adopted: list[JobHandle] = []

    def submit(self, batch: SubmitBatch) -> list[JobHandle]:
        self.batches.append(batch)
        return [JobHandle(self.name, str(i), a.action_id) for i, a in enumerate(batch.actions)]

    def poll(self, handles: Sequence[JobHandle]) -> dict[JobHandle, JobStatus]:
        assert all(h.executor == self.name for h in handles)
        return {h: JobStatus(state="done", exit_code=0) for h in handles}

    def cancel(self, handles: Sequence[JobHandle]) -> None:
        assert all(h.executor == self.name for h in handles)
        self.cancelled.extend(handles)

    def adopt(self, build: BuildId, handles: Sequence[JobHandle]) -> None:
        self.adopted.extend(handles)


def step_batch(step: str, n: int, execution: str | None = None) -> SubmitBatch:
    actions = tuple(
        dataclasses.replace(
            spec(f"{step}[{i}]"),
            step=step,
            resources=resources(None, None, None, execution=execution),
        )
        for i in range(n)
    )
    return SubmitBatch(plan=PLAN, domain=DOMAIN, build=None, actions=actions)


# R4: batches go to the executor the step's mode and history choose; polls and cancels follow.
def test_routing_executor() -> None:
    direct, pilot = Recording("slurm"), Recording("pilot")
    ex = RoutingExecutor(
        direct,
        pilot,
        history={"short": [2, 3, 4, 5, 6], "long": [600] * 5},
        threshold=3,
        short_s=30,
    )
    assert ex.name == "slurm"  # handles adopted on resume name the direct executor
    h_short = ex.submit(step_batch("short", 1))
    h_long = ex.submit(step_batch("long", 2))
    h_many = ex.submit(step_batch("many", 4))
    h_forced = ex.submit(step_batch("forced", 1, execution="pilot"))
    h_kept = ex.submit(step_batch("kept", 10, execution="direct"))
    assert [b.step for b in pilot.batches] == ["short", "many", "forced"]
    assert [b.step for b in direct.batches] == ["long", "kept"]
    # The choice is made once per step: a later, smaller batch (a retry) goes the same way.
    ex.submit(step_batch("many", 1))
    assert [b.step for b in pilot.batches][-1] == "many"
    every = [*h_short, *h_long, *h_many, *h_forced, *h_kept]
    states = ex.poll(every)
    assert set(states) == set(every)
    ex.cancel(every)
    assert set(pilot.cancelled) == {*h_short, *h_many, *h_forced}
    assert set(direct.cancelled) == {*h_long, *h_kept}
    ex.adopt(BuildId(1), h_long)
    assert direct.adopted == h_long
    with pytest.raises(ExecutorError, match="unknown executor"):
        ex.poll([JobHandle("lsf", "1", "x")])
