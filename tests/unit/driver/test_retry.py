from __future__ import annotations

import pytest

from ebs.core.errors import ConfigError
from ebs.driver.retry import BackoffRetry, RetryDecision
from tests.helpers.driver import Env, Node, make_plan


# R6
def test_backoff(env: Env) -> None:
    spec = make_plan(env.cas, {"a": Node()}).action("a")
    policy = BackoffRetry(max_retries=3, base_s=5.0, factor=2.0, max_s=12.0)
    assert policy.decide(spec, 1, "node_fail") == RetryDecision(delay_s=5.0)
    assert policy.decide(spec, 2, "node_fail") == RetryDecision(delay_s=10.0)
    assert policy.decide(spec, 3, "node_fail") == RetryDecision(delay_s=12.0)  # capped
    assert policy.decide(spec, 4, "node_fail") is None  # 3 retries used up


# R6
def test_zero_retries_never_retry(env: Env) -> None:
    spec = make_plan(env.cas, {"a": Node()}).action("a")
    assert BackoffRetry(max_retries=0).decide(spec, 1, "oom") is None


# R6
@pytest.mark.parametrize(
    "kwargs",
    [{"max_retries": -1}, {"base_s": -1.0}, {"factor": 0.5}, {"max_s": -1.0}],
)
def test_invalid_settings_rejected(kwargs: dict[str, float]) -> None:
    with pytest.raises(ConfigError):
        BackoffRetry(**kwargs)  # type: ignore[arg-type]
