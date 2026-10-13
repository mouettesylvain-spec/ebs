"""Job arrays: which actions share an sbatch call, throttles and the array map (P1-02 R1, R2)."""

from __future__ import annotations

import dataclasses
import json

import pytest

from ebs.cas.fs import FsCAS
from ebs.core.canon import canonical_json
from ebs.core.errors import ExecutorError
from ebs.core.types import JsonValue
from ebs.exec.slurm.arrays import (
    ARRAY_MAP_VERSION,
    ArrayMap,
    array_throttle,
    decode_array_map,
    load_array_map,
    plan_jobs,
    store_array_map,
)
from ebs.flow.model import Resources
from ebs.plan.types import ActionSpec
from tests.helpers.executor import PLAN, spec


def _specs(n: int, *, max_parallel: int | None = None) -> list[ActionSpec]:
    out = [spec(f"sim[{i}]") for i in range(n)]
    if max_parallel is not None:
        out = [dataclasses.replace(s, resources=Resources(max_parallel=max_parallel)) for s in out]
    return out


# R1
def test_single_vs_array() -> None:
    (single,) = plan_jobs(_specs(1), max_array_size=1000, throttle=200)
    assert not single.is_array
    assert single.throttle is None
    assert [a.action_id for a in single.actions] == ["sim[0]"]

    (array,) = plan_jobs(_specs(2), max_array_size=1000, throttle=200)
    assert array.is_array
    assert [a.action_id for a in array.actions] == ["sim[0]", "sim[1]"]

    (big,) = plan_jobs(_specs(1000), max_array_size=1000, throttle=200)
    assert big.is_array
    assert len(big.actions) == 1000


# R1
def test_split_large_arrays() -> None:
    specs = _specs(2501)
    jobs = plan_jobs(specs, max_array_size=1000, throttle=200)
    assert [len(j.actions) for j in jobs] == [1000, 1000, 501]
    assert all(j.is_array for j in jobs)
    # Order is kept and every action lands in exactly one job.
    flat = [a.action_id for j in jobs for a in j.actions]
    assert flat == [s.action_id for s in specs]

    # A remainder of one action is a plain job, not a one-element array.
    jobs = plan_jobs(_specs(5), max_array_size=2, throttle=200)
    assert [(len(j.actions), j.is_array) for j in jobs] == [(2, True), (2, True), (1, False)]

    # max_array_size = 1: SLURM arrays are effectively disabled.
    jobs = plan_jobs(_specs(3), max_array_size=1, throttle=200)
    assert [j.is_array for j in jobs] == [False, False, False]


@pytest.mark.parametrize(("size", "throttle"), [(0, 1), (1, 0), (-1, 5)])
def test_invalid_limits(size: int, throttle: int) -> None:
    with pytest.raises(ExecutorError, match="at least 1"):
        plan_jobs(_specs(2), max_array_size=size, throttle=throttle)


def test_empty_batch() -> None:
    assert plan_jobs([], max_array_size=10, throttle=1) == []


# R1
def test_array_map_in_cas(cas: FsCAS) -> None:
    ids = ["sim[test=a,seed=1]", "sim[test=b,seed=2]", "sim[test=ü,seed=3]"]
    digest = store_array_map(cas, PLAN, ids)
    assert cas.has(digest)
    with cas.open(digest) as fh:
        raw = fh.read()
    # Canonical JSON: same plan + actions => same digest, whatever the caller.
    actions: list[JsonValue] = list(ids)
    assert raw == canonical_json({"v": ARRAY_MAP_VERSION, "plan": str(PLAN), "actions": actions})
    assert store_array_map(cas, PLAN, ids) == digest
    loaded = load_array_map(cas, digest)
    assert loaded == ArrayMap(plan=PLAN, actions=tuple(ids))
    # Index i of the array runs actions[i] (the runner reads $SLURM_ARRAY_TASK_ID).
    assert loaded.action_for(1) == "sim[test=b,seed=2]"
    with pytest.raises(ExecutorError, match="task 3"):
        loaded.action_for(3)


@pytest.mark.parametrize(
    "doc",
    [
        b"not json",
        json.dumps({"v": 2, "plan": str(PLAN), "actions": ["a"]}).encode(),
        json.dumps({"v": 1, "plan": "md5:x", "actions": ["a"]}).encode(),
        json.dumps({"v": 1, "plan": str(PLAN), "actions": []}).encode(),
        json.dumps({"v": 1, "plan": str(PLAN), "actions": ["a", "a"]}).encode(),
        json.dumps({"v": 1, "plan": str(PLAN), "actions": [1]}).encode(),
        json.dumps({"v": 1, "plan": str(PLAN)}).encode(),
    ],
)
def test_decode_rejects_bad_maps(doc: bytes) -> None:
    with pytest.raises(ExecutorError, match="array map"):
        decode_array_map(doc)


def test_store_rejects_bad_maps(cas: FsCAS) -> None:
    with pytest.raises(ExecutorError, match="array map"):
        store_array_map(cas, PLAN, [])
    with pytest.raises(ExecutorError, match="array map"):
        store_array_map(cas, PLAN, ["a", "a"])


# R2
def test_throttle_override() -> None:
    # Config default.
    assert array_throttle(_specs(3), default=200) == 200
    # A step's resources.max_parallel wins, also when it is above the default.
    assert array_throttle(_specs(3, max_parallel=4), default=200) == 4
    assert array_throttle(_specs(3, max_parallel=500), default=200) == 500
    # Mixed (the driver batches identical resources, but be safe): the strictest wins.
    mixed = [*_specs(2), *_specs(1, max_parallel=7)]
    assert array_throttle(mixed, default=200) == 7

    (job,) = plan_jobs(_specs(10, max_parallel=3), max_array_size=1000, throttle=200)
    assert job.throttle == 3
    (job,) = plan_jobs(_specs(10), max_array_size=1000, throttle=200)
    assert job.throttle == 200


# R2: in one array, the strictest max_parallel of its actions wins.
def test_throttle_is_strictest_of_batch() -> None:
    specs = [
        *_specs(1, max_parallel=4),
        dataclasses.replace(spec("x[0]")),
        *[dataclasses.replace(s, action_id="y[0]") for s in _specs(1, max_parallel=2)],
    ]
    (job,) = plan_jobs(specs, max_array_size=10, throttle=50)
    assert job.throttle == 2
    (job,) = plan_jobs(_specs(3), max_array_size=10, throttle=50)
    assert job.throttle == 50
