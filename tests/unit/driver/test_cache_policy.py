from __future__ import annotations

import pytest

from ebs.core.errors import ConfigError
from ebs.driver.cache_policy import CachePolicy
from ebs.meta.api import CacheMode
from tests.helpers.driver import Env, Node, make_plan, result_for


# R2
@pytest.mark.parametrize(
    ("mode", "reads", "writes"),
    [("off", False, False), ("read", True, False), ("write", True, True)],
)
def test_modes(mode: CacheMode, reads: bool, writes: bool) -> None:
    policy = CachePolicy(mode)
    assert (policy.reads, policy.writes) == (reads, writes)


# R2
def test_rerun_failed(env: Env) -> None:
    spec = make_plan(env.cas, {"a": Node()}).action("a")
    passed, failed = result_for(spec), result_for(spec, passed=False)
    assert CachePolicy("read").accept(failed)
    assert CachePolicy("read").accept(passed)
    assert not CachePolicy("read", rerun_failed=True).accept(failed)
    assert CachePolicy("read", rerun_failed=True).accept(passed)


# R2
def test_unknown_mode_rejected() -> None:
    with pytest.raises(ConfigError, match="cache mode"):
        CachePolicy("sometimes")  # type: ignore[arg-type]


# R2: a warm cache (from a `write` build) is read or ignored, and new results written, per mode.
@pytest.mark.parametrize(
    ("mode", "a_cached", "c_written"),
    [("off", False, False), ("read", True, False), ("write", True, True)],
)
def test_cache_modes_end_to_end(env: Env, mode: CacheMode, a_cached: bool, c_written: bool) -> None:
    warm = env.plan({"a": Node()})
    assert env.run(warm, env.executor(), cache_mode="write").status == "passed"

    plan = env.plan({"a": Node(), "c": Node()})
    executor = env.executor()
    outcome = env.run(plan, executor, cache_mode=mode)

    assert outcome.status == "passed"
    assert outcome.states["a"] == ("cached" if a_cached else "done")
    assert executor.submitted == (["c"] if a_cached else ["a", "c"])
    c_key = outcome.plan.action("c").key
    assert c_key is not None
    assert (env.store.cache_get("test", c_key) is not None) == c_written
    assert env.store.get_build(outcome.build).cache_mode == mode
