from __future__ import annotations

from dataclasses import replace

import pytest

from ebs.core.errors import ConfigError, ExecutorError
from ebs.driver.events import read_events
from ebs.driver.retry import (
    Attempt,
    BackoffRetry,
    ReasonRetry,
    RetryDecision,
    RetrySettings,
    attempt_history,
    grown_memory,
)
from ebs.exec.api import JobHandle, SubmitBatch
from ebs.flow.model import Resources
from ebs.meta.api import InfraReason
from ebs.plan.types import ActionSpec
from tests.helpers.driver import Env, Node, ScriptedExecutor, make_plan


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


# --- P1-05: per-reason policy, attempt history, node exclusion ---------------------------------

G = 1 << 30
M = 1 << 20
SETTINGS = RetrySettings(max_retries=2, backoff_s=10.0, backoff_factor=2.0, backoff_max_s=300.0)


def _spec(env: Env, **resources: object) -> ActionSpec:
    node = Node(resources=Resources(**resources))  # type: ignore[arg-type]
    return make_plan(env.cas, {"a": node}).action("a")


def _decide(
    env: Env,
    reason: InfraReason,
    attempt: int = 1,
    settings: RetrySettings = SETTINGS,
    **resources: object,
) -> RetryDecision | None:
    return ReasonRetry(settings).decide(_spec(env, **resources), attempt, reason)


# R1
@pytest.mark.parametrize(
    ("reason", "attempt", "resources", "settings", "expected"),
    [
        # oom: memory x 1.5 (rounded up to a MiB), capped at max_mem; no request, nothing to grow
        ("oom", 1, {"mem": "4G"}, SETTINGS, RetryDecision(10.0, Resources(mem="6G"))),
        ("oom", 2, {"mem": "6G"}, SETTINGS, RetryDecision(20.0, Resources(mem="9G"))),
        ("oom", 1, {"mem": "3M"}, SETTINGS, RetryDecision(10.0, Resources(mem="5M"))),
        (
            "oom",
            1,
            {"mem": "6G", "cpus": 2},
            replace(SETTINGS, max_mem=8 * G),
            RetryDecision(10.0, Resources(mem="8G", cpus=2)),
        ),
        ("oom", 1, {"mem": "8G"}, replace(SETTINGS, max_mem=8 * G), None),
        ("oom", 1, {}, SETTINGS, None),
        ("oom", 3, {"mem": "4G"}, SETTINGS, None),
        (
            "oom",
            1,
            {"mem": "4G"},
            replace(SETTINGS, oom_mem_factor=2.0),
            RetryDecision(10.0, Resources(mem="8G")),
        ),
        # timeout: not retried unless the step (or the site default) asks for it
        ("timeout", 1, {}, SETTINGS, None),
        ("timeout", 1, {"retry_on_timeout": True}, SETTINGS, RetryDecision(10.0)),
        ("timeout", 1, {}, replace(SETTINGS, retry_on_timeout=True), RetryDecision(10.0)),
        ("timeout", 1, {"retry_on_timeout": False}, replace(SETTINGS, retry_on_timeout=True), None),
        ("timeout", 3, {"retry_on_timeout": True}, SETTINGS, None),
        # node_fail / preempted / runner_crash / other: same resources, backoff
        ("node_fail", 1, {"mem": "4G"}, SETTINGS, RetryDecision(10.0)),
        ("node_fail", 2, {}, SETTINGS, RetryDecision(20.0)),
        ("node_fail", 3, {}, SETTINGS, None),
        ("preempted", 1, {}, SETTINGS, RetryDecision(10.0)),
        ("runner_crash", 2, {}, SETTINGS, RetryDecision(20.0)),
        ("other", 1, {}, SETTINGS, RetryDecision(10.0)),
        ("node_fail", 2, {}, replace(SETTINGS, backoff_max_s=15.0), RetryDecision(15.0)),
        # license: 60 s x attempt (not the exponential backoff)
        ("license", 1, {}, SETTINGS, RetryDecision(60.0)),
        ("license", 2, {}, SETTINGS, RetryDecision(120.0)),
        ("license", 3, {}, SETTINGS, None),
        ("license", 2, {}, replace(SETTINGS, license_backoff_s=30.0), RetryDecision(60.0)),
        # input_verification: once, on another node; then it fails (loudly)
        ("input_verification", 1, {}, SETTINGS, RetryDecision(10.0, avoid_node=True)),
        # its own bound counts earlier input_verification failures: see the next test
        # max_retries = 0: nothing is retried
        ("node_fail", 1, {}, replace(SETTINGS, max_retries=0), None),
        ("license", 1, {}, replace(SETTINGS, max_retries=0), None),
        ("input_verification", 1, {}, replace(SETTINGS, max_retries=0), None),
        ("oom", 1, {"mem": "4G"}, replace(SETTINGS, max_retries=0), None),
    ],
)
def test_policy(
    env: Env,
    reason: InfraReason,
    attempt: int,
    resources: dict[str, object],
    settings: RetrySettings,
    expected: RetryDecision | None,
) -> None:
    assert _decide(env, reason, attempt, settings, **resources) == expected


# R1: input_verification is bounded by its own earlier failures, not by the attempt number.
def test_policy_input_verification_counts_its_own_failures(env: Env) -> None:
    policy = ReasonRetry(SETTINGS)
    spec = _spec(env)
    once = RetryDecision(10.0, avoid_node=True)
    assert policy.decide(spec, 1, "input_verification") == once
    # A node failure first: the first input_verification is still retried, elsewhere.
    assert policy.decide(spec, 2, "input_verification", earlier=("node_fail",)) == (
        RetryDecision(20.0, avoid_node=True)
    )
    assert policy.decide(spec, 2, "input_verification", earlier=("input_verification",)) is None
    twice = ReasonRetry(replace(SETTINGS, input_verification_retries=2, max_retries=5))
    assert twice.decide(spec, 2, "input_verification", earlier=("input_verification",)) == (
        RetryDecision(20.0, avoid_node=True)
    )
    assert twice.decide(spec, 2, "input_verification", earlier=("other",)) is not None
    assert twice.decide(spec, 3, "input_verification", earlier=("input_verification",) * 2) is None
    no_retry = ReasonRetry(replace(SETTINGS, input_verification_retries=0))
    assert no_retry.decide(spec, 1, "input_verification") is None


# R1: a retried OOM keeps every other resource of the action.
def test_policy_oom_keeps_other_resources(env: Env) -> None:
    spec = _spec(env, mem="4G", cpus=4, time="1h", constraint="avx512", retry_on_timeout=True)
    decision = ReasonRetry(SETTINGS).decide(spec, 1, "oom")
    assert decision is not None
    assert decision.resources == spec.resources.model_copy(update={"mem": 6 * G})


# R1: `[retry]` in the site config.
def test_settings_from_config() -> None:
    assert RetrySettings.from_config({}) == RetrySettings()
    defaults = RetrySettings()
    assert (defaults.max_retries, defaults.oom_mem_factor, defaults.license_backoff_s) == (
        2,
        1.5,
        60.0,
    )
    assert (defaults.retry_on_timeout, defaults.max_mem, defaults.input_verification_retries) == (
        False,
        None,
        1,
    )
    got = RetrySettings.from_config(
        {
            "retry": {
                "max_retries": 4,
                "backoff_s": 5,
                "backoff_factor": 3,
                "backoff_max_s": 100,
                "oom_mem_factor": 2,
                "max_mem": "64G",
                "license_backoff_s": 30,
                "retry_on_timeout": True,
                "input_verification_retries": 0,
            }
        }
    )
    assert got == RetrySettings(
        max_retries=4,
        backoff_s=5.0,
        backoff_factor=3.0,
        backoff_max_s=100.0,
        oom_mem_factor=2.0,
        max_mem=64 * G,
        license_backoff_s=30.0,
        retry_on_timeout=True,
        input_verification_retries=0,
    )


# R1
@pytest.mark.parametrize(
    ("section", "message"),
    [
        ({"max_retries": -1}, "max_retries"),
        ({"max_retries": 1.5}, "max_retries"),
        ({"max_retries": True}, "max_retries"),
        ({"backoff_s": -1}, "backoff_s"),
        ({"backoff_factor": 0.5}, "backoff_factor"),
        ({"oom_mem_factor": 1}, "oom_mem_factor"),
        ({"oom_mem_factor": "x"}, "oom_mem_factor"),
        ({"max_mem": "lots"}, "max_mem"),
        ({"max_mem": 64}, "max_mem"),
        ({"license_backoff_s": -5}, "license_backoff_s"),
        ({"retry_on_timeout": "yes"}, "retry_on_timeout"),
        ({"input_verification_retries": -1}, "input_verification_retries"),
        ({"retries": 3}, "retries"),
    ],
)
def test_settings_validation(section: dict[str, object], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        RetrySettings.from_config({"retry": section})
    with pytest.raises(ConfigError, match=r"\[retry\]"):
        RetrySettings.from_config({"retry": "often"})


# R1, R5 end to end in the driver: OOM grows memory, timeout is final but never cached.
def test_policy_in_driver(env: Env) -> None:
    executor = env.executor()
    executor.script("a", ("infra", "oom"), ("infra", "oom"), "pass")
    executor.script("t", ("infra", "timeout"))
    plan = env.plan({"a": Node(resources=Resources(mem="2G")), "t": Node()})
    outcome = env.run(plan, executor, cache_mode="write", keep_going=True, retry=ReasonRetry())
    assert [s.resources.mem for s in executor.submitted_specs if s.action_id == "a"] == [
        2 * G,
        3 * G,
        4608 * M,
    ]
    assert outcome.states == {"a": "done", "t": "infra_failed"}
    assert executor.runs("t") == 1
    row = {r.action_id: r for r in env.store.list_actions(outcome.build)}["t"]
    assert (row.state, row.infra_reason) == ("infra_failed", "timeout")
    key = outcome.plan.action("t").key
    assert key is not None
    assert env.store.cache_get("test", key) is None


# R2
def test_attempt_history(env: Env) -> None:
    executor = env.executor()
    executor.script("a", ("infra", "node_fail"), ("infra", "oom"), "pass", nodes=["n1", "n2", "n3"])
    executor.script("b", ("infra", "license"))
    plan = env.plan({"a": Node(resources=Resources(mem="1G")), "b": Node()})
    outcome = env.run(plan, executor, keep_going=True, retry=ReasonRetry(SETTINGS))
    assert outcome.states == {"a": "done", "b": "infra_failed"}
    events = read_events(outcome.events_path)

    # Each attempt is its own event, numbered.
    failed = [
        (e.action_id, e.data["attempt"], e.data["reason"])
        for e in events
        if e.type == "infra_failed"
    ]
    assert sorted(failed) == [
        ("a", 1, "node_fail"),
        ("a", 2, "oom"),
        ("b", 1, "license"),
        ("b", 2, "license"),
        ("b", 3, "license"),
    ]
    submitted = [(e.action_id, e.data["attempt"]) for e in events if e.type == "submitted"]
    assert sorted(submitted) == [("a", 1), ("a", 2), ("a", 3), ("b", 1), ("b", 2), ("b", 3)]

    history = attempt_history(events)
    assert history["a"] == [
        Attempt(1, job_id="a#1", node="n1", reason="node_fail"),
        Attempt(2, job_id="a#2", node="n2", reason="oom"),
        Attempt(3, job_id="a#3", node=None, reason=None),
    ]
    assert [h.reason for h in history["b"]] == ["license"] * 3

    # The final event of each action carries its reason history.
    final = {e.action_id: e.data for e in events if e.type == "finished"}
    assert final["a"]["state"] == "done"
    assert final["a"]["reasons"] == ["node_fail", "oom"]
    assert final["a"]["attempts"] == 3
    assert final["b"]["reasons"] == ["license"] * 3
    assert final["b"]["attempts"] == 3
    # The store keeps the attempt count of the action's row.
    rows = {r.action_id: r for r in env.store.list_actions(outcome.build)}
    assert rows["a"].attempts == 3


# R2: an action that never failed has a one-attempt history and no reasons in its event.
def test_attempt_history_clean_run(env: Env) -> None:
    executor = env.executor()
    outcome = env.run(env.plan({"a": Node()}), executor, retry=ReasonRetry())
    events = read_events(outcome.events_path)
    assert attempt_history(events) == {"a": [Attempt(1, job_id="a#1", node=None, reason=None)]}
    (final,) = [e.data for e in events if e.type == "finished"]
    assert "reasons" not in final


# R3
def test_node_exclusion(env: Env) -> None:
    executor = env.executor()
    # a fails twice on n1 (node_fail): n1 is excluded for the rest of the build.
    executor.script(
        "a", ("infra", "node_fail"), ("infra", "node_fail"), "pass", nodes=["n1", "n1", "n2"]
    )
    # c fails once on n7: one failure does not exclude a node.
    executor.script("c", ("infra", "node_fail"), "pass", nodes=["n7", "n8"])
    plan = env.plan({"a": Node(), "c": Node(), "b": Node(deps=(("a", "out"),))})
    outcome = env.run(plan, executor, retry=ReasonRetry(replace(SETTINGS, max_retries=3)))
    assert outcome.states == {"a": "done", "c": "done", "b": "done"}

    def excluded(action_id: str) -> list[tuple[str, ...]]:
        return [b.exclude_nodes for b in executor.batches if b.actions[0].action_id == action_id]

    # One node_fail on n1 excludes nothing; the second one excludes n1 ...
    assert excluded("a") == [(), (), ("n1",)]
    assert excluded("b") == [("n1",)]  # ... for the whole build, other actions too
    assert all("n7" not in b.exclude_nodes for b in executor.batches)
    events = read_events(outcome.events_path)
    marked = [e.data.get("excluded_node") for e in events if e.type == "infra_failed"]
    assert marked.count("n1") == 1


# R3: two node_fail of different actions on one node exclude it as well.
def test_node_exclusion_across_actions(env: Env) -> None:
    executor = env.executor()
    executor.script("a", ("infra", "node_fail"), "pass", nodes=["n1", "n2"])
    executor.script("c", ("infra", "node_fail"), "pass", nodes=["n1", "n2"])
    plan = env.plan({"a": Node(), "c": Node(), "z": Node(deps=(("a", "out"), ("c", "out")))})
    outcome = env.run(plan, executor, retry=ReasonRetry(SETTINGS))
    assert outcome.states["z"] == "done"
    (z,) = [b for b in executor.batches if b.actions[0].action_id == "z"]
    assert z.exclude_nodes == ("n1",)


# R1, R3: input verification is retried once, away from the node that served bad bytes.
def test_input_verification_retried_on_other_node(env: Env) -> None:
    executor = env.executor()
    executor.script(
        "a",
        ("infra", "input_verification"),
        ("infra", "input_verification"),
        nodes=["n4", "n5"],
    )
    executor.script("x", "pass")
    plan = env.plan({"a": Node(), "x": Node()})
    outcome = env.run(plan, executor, keep_going=True, retry=ReasonRetry(SETTINGS))
    assert outcome.states == {"a": "infra_failed", "x": "done"}
    assert outcome.exit_code == 3
    a_batches = [b for b in executor.batches if b.actions[0].action_id == "a"]
    assert [b.exclude_nodes for b in a_batches] == [(), ("n4",)]
    (x,) = [b for b in executor.batches if b.actions[0].action_id == "x"]
    assert x.exclude_nodes == ()  # only that action avoids n4
    events = read_events(outcome.events_path)
    (final,) = [e.data for e in events if e.type == "finished" and e.action_id == "a"]
    assert final["reasons"] == ["input_verification", "input_verification"]
    assert "CAS" in str(final["hint"])


# R1, R2: the driver hands the earlier reasons to the policy.
def test_input_verification_after_node_fail(env: Env) -> None:
    executor = env.executor()
    executor.script(
        "a",
        ("infra", "node_fail"),
        ("infra", "input_verification"),
        "pass",
        nodes=["n1", "n2", "n3"],
    )
    outcome = env.run(env.plan({"a": Node()}), executor, retry=ReasonRetry(SETTINGS))
    assert outcome.states == {"a": "done"}
    assert [b.exclude_nodes for b in executor.batches] == [(), (), ("n2",)]
    (final,) = [e.data for e in read_events(outcome.events_path) if e.type == "finished"]
    assert final["reasons"] == ["node_fail", "input_verification"]
    assert final["attempts"] == 3


# R2: a failed submit is an attempt too, so `attempts` never undercounts the reasons.
def test_attempts_count_failed_submits(env: Env) -> None:
    class Refusing(ScriptedExecutor):
        def submit(self, batch: SubmitBatch) -> list[JobHandle]:
            raise ExecutorError("sbatch: error: Batch job submission failed")

    executor = Refusing(env.store, env.cas)
    outcome = env.run(
        env.plan({"a": Node()}), executor, retry=ReasonRetry(replace(SETTINGS, backoff_s=0))
    )
    assert outcome.states == {"a": "infra_failed"}
    (final,) = [e.data for e in read_events(outcome.events_path) if e.type == "finished"]
    assert final["reasons"] == ["other"] * 3
    assert final["attempts"] == 3


# R5 / I12: a test FAILED result is final: the policy is never asked.
def test_failed_not_retried(env: Env) -> None:
    class Spy:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int, str]] = []

        def decide(
            self,
            spec: ActionSpec,
            attempt: int,
            reason: InfraReason,
            earlier: tuple[InfraReason, ...] = (),
        ) -> RetryDecision:
            assert earlier == ()
            self.calls.append((spec.action_id, attempt, reason))
            return RetryDecision(delay_s=0.0)

    executor = env.executor()
    executor.script("a", "fail")
    executor.script("b", ("infra", "node_fail"), "fail")
    spy = Spy()
    plan = env.plan({"a": Node(), "b": Node()})
    outcome = env.run(plan, executor, cache_mode="write", keep_going=True, retry=spy)
    assert outcome.states == {"a": "failed", "b": "failed"}
    assert executor.runs("a") == 1
    assert executor.runs("b") == 2  # the infra failure was retried; the test failure was not
    assert spy.calls == [("b", 1, "node_fail")]
    assert outcome.status == "failed"
    for action_id in ("a", "b"):
        key = outcome.plan.action(action_id).key
        assert key is not None
        cached = env.store.cache_get("test", key)
        assert cached is not None
        assert cached.status == "failed"

    # The real policy says the same (another key: the failure above is cached).
    executor = env.executor()
    executor.script("a", "fail")
    plan = env.plan({"a": Node(source="v2")})
    outcome = env.run(plan, executor, retry=ReasonRetry(SETTINGS))
    assert executor.runs("a") == 1
    assert outcome.states == {"a": "failed"}


# --- test-critic follow-ups --------------------------------------------------------------------


# R3: only node_fail counts towards excluding a node; other reasons on one node do not.
def test_only_node_fail_excludes_nodes(env: Env) -> None:
    executor = env.executor()
    executor.script("a", ("infra", "oom"), ("infra", "oom"), "pass", nodes=["n1", "n1", "n2"])
    executor.script(
        "c", ("infra", "preempted"), ("infra", "license"), "pass", nodes=["n1", "n1", "n3"]
    )
    plan = env.plan(
        {
            "a": Node(resources=Resources(mem="1G")),
            "c": Node(),
            "z": Node(deps=(("a", "out"), ("c", "out"))),
        }
    )
    outcome = env.run(plan, executor, retry=ReasonRetry(replace(SETTINGS, max_retries=3)))
    assert outcome.states == {"a": "done", "c": "done", "z": "done"}
    assert all(b.exclude_nodes == () for b in executor.batches)
    events = read_events(outcome.events_path)
    assert not any("excluded_node" in e.data for e in events if e.type == "infra_failed")


# R1, R3: an action avoiding a node does not share a job array (and its exclusion) with others.
def test_avoid_set_splits_batches(env: Env) -> None:
    executor = env.executor()
    executor.script("s[a]", ("infra", "input_verification"), "pass", nodes=["n4", "n5"])
    executor.script("s[b]", ("infra", "node_fail"), "pass", nodes=["n6", "n7"])
    plan = env.plan({"s[a]": Node(), "s[b]": Node()})
    outcome = env.run(plan, executor, retry=ReasonRetry(replace(SETTINGS, backoff_s=0.0)))
    assert outcome.states == {"s[a]": "done", "s[b]": "done"}
    first, *retries = executor.batches
    assert [a.action_id for a in first.actions] == ["s[a]", "s[b]"]
    by_action = {b.actions[0].action_id: b for b in retries}
    assert [len(b.actions) for b in retries] == [1, 1]
    assert by_action["s[a]"].exclude_nodes == ("n4",)
    assert by_action["s[b]"].exclude_nodes == ()


# R1: the CAS hint is for input verification only.
def test_cas_hint_only_for_input_verification(env: Env) -> None:
    executor = env.executor()
    executor.script("t", ("infra", "timeout"))
    executor.script("o", ("infra", "oom"))
    executor.script("l", ("infra", "license"))
    plan = env.plan({"t": Node(), "o": Node(), "l": Node()})
    outcome = env.run(
        plan,
        executor,
        keep_going=True,
        retry=ReasonRetry(replace(SETTINGS, license_backoff_s=0.0)),
    )
    assert set(outcome.states.values()) == {"infra_failed"}
    finals = [e.data for e in read_events(outcome.events_path) if e.type == "finished"]
    assert len(finals) == 3
    assert all("hint" not in data for data in finals)


# R3: a node is excluded once, even when jobs already in flight on it fail afterwards.
def test_node_excluded_once(env: Env) -> None:
    executor = env.executor()
    for action_id in ("s[a]", "s[b]", "s[c]"):
        executor.script(action_id, ("infra", "node_fail"), "pass", nodes=["n1", "n2"])
    plan = env.plan({"s[a]": Node(), "s[b]": Node(), "s[c]": Node()})
    outcome = env.run(plan, executor, retry=ReasonRetry(replace(SETTINGS, backoff_s=0.0)))
    assert set(outcome.states.values()) == {"done"}
    events = read_events(outcome.events_path)
    marked = [e.data.get("excluded_node") for e in events if e.type == "infra_failed"]
    assert marked.count("n1") == 1
    first, *retries = executor.batches
    assert first.exclude_nodes == ()
    assert retries
    assert all(b.exclude_nodes == ("n1",) for b in retries)


# R2: events from another driver version never break the history.
def test_attempt_history_tolerates_odd_events(env: Env) -> None:
    from datetime import UTC, datetime

    from ebs.meta.api import BuildId, Event

    def event(action_id: str, data: dict[str, object]) -> Event:
        return Event(
            ts=datetime(2026, 1, 1, tzinfo=UTC),
            build=BuildId(1),
            type="infra_failed",
            action_id=action_id,
            data=data,
        )

    history = attempt_history(
        [
            event("a", {"attempt": 1, "reason": "martian"}),
            event("b", {"attempt": 1}),
            event("c", {"attempt": True, "reason": "oom"}),  # not an attempt number
            event("d", {"reason": "oom"}),  # the runner's own event: no attempt
        ]
    )
    assert history == {
        "a": [Attempt(1, reason="other")],
        "b": [Attempt(1, reason="other")],
    }


# R1: [retry] bounds.
def test_settings_edges() -> None:
    assert RetrySettings.from_config({"retry": {"backoff_factor": 1}}).backoff_factor == 1.0
    for section, message in (
        ({"backoff_max_s": -1}, "backoff_max_s"),
        ({"max_mem": "512K"}, "max_mem"),
    ):
        with pytest.raises(ConfigError, match=message):
            RetrySettings.from_config({"retry": section})


# R1: the grown request never exceeds the cap and stays MiB-aligned.
def test_grown_memory() -> None:
    assert grown_memory(4 * G, 1.5, None) == 6 * G
    assert grown_memory(4 * G, 1.5, 5 * G + 1) == 5 * G  # cap rounded down to a MiB
    assert grown_memory(4 * G, 1.5, 4 * G + M - 1) is None  # no whole MiB to grow into
    assert grown_memory(3 * M, 1.5, None) == 5 * M
