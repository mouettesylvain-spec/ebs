"""Driver behaviour when executors, runners or outputs misbehave."""

from __future__ import annotations

import os
import signal
import threading
import time
from collections.abc import Sequence

import pytest

from ebs.core.clock import SystemClock
from ebs.core.errors import ConfigError, ExecutorError, MetadataError
from ebs.driver.retry import BackoffRetry
from ebs.driver.scheduler import MAX_MISSED_POLLS, DriverConfig
from ebs.exec.api import JobHandle, JobStatus, SubmitBatch
from ebs.meta.api import BuildId
from tests.helpers.driver import Env, Node, ScriptedExecutor, SleepRecorder

CHAIN = {"a": Node(), "b": Node(deps=(("a", "out"),))}


# R9 / config
@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_batch": 0},
        {"poll_min_s": 0.0},
        {"poll_min_s": 5.0, "poll_max_s": 1.0},
        {"max_retries": -1},
    ],
)
def test_invalid_config_rejected(kwargs: dict[str, float]) -> None:
    with pytest.raises(ConfigError):
        DriverConfig(**kwargs)  # type: ignore[arg-type]


class FlakySubmit(ScriptedExecutor):
    """The first submit fails, as when sbatch cannot reach the controller."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.failed_submits = 0

    def submit(self, batch: SubmitBatch) -> list[JobHandle]:
        if self.failed_submits == 0:
            self.failed_submits += 1
            raise ExecutorError("sbatch: error: Unable to contact slurm controller")
        return super().submit(batch)


# R6: a failed submission is an infrastructure failure of every action in the batch.
def test_submit_error_is_retried(env: Env) -> None:
    executor = FlakySubmit(env.store, env.cas)
    outcome = env.run(env.plan(CHAIN), executor)
    assert executor.failed_submits == 1
    assert outcome.states == {"a": "done", "b": "done"}
    rows = {r.action_id: r for r in env.store.list_actions(outcome.build)}
    assert rows["a"].attempts == 1  # the failed submit never reached `running`


# R6: `done` without a posted result means the runner broke after exiting 0.
def test_done_without_result_is_infra(env: Env) -> None:
    executor = env.executor()
    executor.script("a", "lost", "pass")
    outcome = env.run(env.plan(CHAIN), executor)
    assert executor.submitted == ["a", "a", "b"]
    assert outcome.states == {"a": "done", "b": "done"}


# R6: a job cancelled outside ebs (an admin's scancel) is retried like any infra failure.
def test_external_cancel_is_infra(env: Env) -> None:
    executor = env.executor()
    executor.script("a", "vanished", "vanished", "vanished")
    outcome = env.run(env.plan(CHAIN), executor, max_retries=2)
    assert executor.submitted == ["a", "a", "a"]
    assert outcome.states == {"a": "infra_failed", "b": "skipped"}
    assert outcome.exit_code == 3


# R5: a producer that succeeded without an optional output leaves its consumer unrunnable.
def test_missing_optional_output_fails_consumer(env: Env) -> None:
    plan = env.plan(
        {
            "a": Node(outputs={"out": True, "cov": True}, optional=frozenset({"cov"})),
            "b": Node(deps=(("a", "cov"),)),
            "c": Node(deps=(("b", "out"),)),
        }
    )
    executor = env.executor()
    executor.script("a", "pass", omit={"cov"})
    outcome = env.run(plan, executor, keep_going=True)
    assert outcome.states == {"a": "done", "b": "skipped", "c": "skipped"}
    assert executor.submitted == ["a"]
    assert outcome.status == "failed"


# R8: a job that was already finishing when we cancelled keeps its result.
def test_cancel_keeps_jobs_that_finished(env: Env) -> None:
    def interrupt(poll: int) -> None:
        if poll == 1:
            os.kill(os.getpid(), signal.SIGINT)

    executor = env.executor(on_poll=interrupt)
    executor.script("a", "pass", running_polls=5, ignores_cancel=True)
    executor.script("x", "hang")
    outcome = env.run(env.plan({**CHAIN, "x": Node()}), executor, handle_sigint=True)
    assert outcome.states == {"a": "done", "b": "cancelled", "x": "cancelled"}
    assert env.store.get_result(outcome.build, "a") is not None
    assert outcome.status == "cancelled"


# R8: a retry still waiting when the user cancels ends `cancelled`, not `infra_failed`.
def test_cancel_during_retry_wait(env: Env) -> None:
    def interrupt(poll: int) -> None:
        if poll == 3:
            os.kill(os.getpid(), signal.SIGINT)

    executor = env.executor(on_poll=interrupt)
    executor.script("a", ("infra", "node_fail"), "pass")
    executor.script("x", "hang")
    outcome = env.run(env.plan({"a": Node(), "x": Node()}), executor, handle_sigint=True)
    assert outcome.states == {"a": "cancelled", "x": "cancelled"}
    assert {r.action_id: r.state for r in env.store.list_actions(outcome.build)} == {
        "a": "cancelled",
        "x": "cancelled",
    }


class BrokenStore(Exception):
    pass


# R10: an unexpected error cancels submitted work and still closes the build record.
def test_unexpected_error_cancels_and_finishes_build(env: Env) -> None:
    def explode(poll: int) -> None:
        if poll == 2:
            raise BrokenStore("metadata service went away")

    executor = env.executor(on_poll=explode)
    executor.script("a", "hang")
    with pytest.raises(BrokenStore):
        env.run(env.plan({"a": Node()}), executor)
    assert executor.cancelled == ["a"]
    (build,) = [b for b in range(1, 10) if _exists(env, b)]
    assert env.store.get_build(build).status == "infra_failed"  # type: ignore[arg-type]


def _exists(env: Env, build: int) -> bool:
    try:
        env.store.get_build(build)  # type: ignore[arg-type]
    except MetadataError:
        return False
    return True


class ShortHanded(ScriptedExecutor):
    def submit(self, batch: SubmitBatch) -> list[JobHandle]:
        return super().submit(batch)[:-1]


# Executor contract (interfaces.md § 8): one handle per action, or the build stops loudly.
def test_executor_returning_too_few_handles_is_an_error(env: Env) -> None:
    executor = ShortHanded(env.store, env.cas)
    with pytest.raises(ExecutorError, match="one handle per action"):
        env.run(env.plan({"s[1]": Node(), "s[2]": Node()}), executor)


# R8: outside the main thread no handler can be installed; the build still runs.
def test_sigint_handling_off_main_thread(env: Env) -> None:
    results: list[str] = []

    def build() -> None:
        outcome = env.run(env.plan({"a": Node()}), env.executor(), handle_sigint=True)
        results.append(outcome.status)

    thread = threading.Thread(target=build)
    thread.start()
    thread.join(timeout=30)
    assert results == ["passed"]


# R9: a job that goes from queued or pending straight to done (a short SLURM job).
@pytest.mark.parametrize("pending_polls", [0, 1])
def test_done_without_running(env: Env, pending_polls: int) -> None:
    executor = env.executor()
    executor.script("a", "pass", pending_polls=pending_polls, running_polls=0)
    outcome = env.run(env.plan(CHAIN), executor)
    assert outcome.states == {"a": "done", "b": "done"}


# R7: different license counts are different -L requests, so different batches.
def test_license_counts_split_batches(env: Env) -> None:
    plan = env.plan(
        {
            "sim[1]": Node(licenses={"msimhdlsim": 1}),
            "sim[2]": Node(licenses={"msimhdlsim": 2}),
        }
    )
    executor = env.executor()
    env.run(plan, executor)
    assert len(executor.batches) == 2


# R6, R9: a due retry is resubmitted after its backoff, not after a full poll interval.
def test_retry_not_delayed_by_poll_interval(env: Env) -> None:
    executor = env.executor()
    executor.script("a", ("infra", "node_fail"), "pass")
    executor.script("x", "pass", running_polls=20)
    retry = BackoffRetry(max_retries=1, base_s=1.0)
    env.run(
        env.plan({"a": Node(), "x": Node()}),
        executor,
        retry=retry,
        poll_min_s=10.0,
        poll_max_s=10.0,
    )
    assert env.clock.sleeps[2] == 1.0


# R9
def test_interval_resets_after_change(env: Env) -> None:
    executor = env.executor()
    executor.script("a", "pass", running_polls=5)
    executor.script("b", "pass", running_polls=6)
    env.run(env.plan({"a": Node(), "b": Node()}), executor, poll_min_s=1.0, poll_max_s=4.0)
    assert env.clock.sleeps == [1.0, 1.0, 2.0, 4.0, 4.0, 4.0, 1.0]


class InterruptingClock(SleepRecorder):
    def sleep(self, seconds: float) -> None:
        super().sleep(seconds)
        if len(self.sleeps) == 2:
            os.kill(os.getpid(), signal.SIGINT)


# R8: Ctrl-C during a sleep cancels before polling again.
def test_no_poll_after_cancel_in_sleep(env: Env) -> None:
    executor = env.executor()
    executor.script("a", "hang")
    outcome = env.run(
        env.plan({"a": Node()}), executor, handle_sigint=True, clock=InterruptingClock()
    )
    assert executor.polls == 2  # one regular poll, then the final one after cancelling
    assert outcome.status == "cancelled"


# R8: on the real clock, Ctrl-C ends a long backoff wait at once.
def test_sigint_interrupts_long_backoff(env: Env) -> None:
    executor = env.executor()
    executor.script("a", ("infra", "node_fail"), "pass", running_polls=0)
    timer = threading.Timer(0.3, os.kill, (os.getpid(), signal.SIGINT))
    start = time.monotonic()
    timer.start()
    try:
        outcome = env.run(
            env.plan({"a": Node()}),
            executor,
            handle_sigint=True,
            clock=SystemClock(),
            retry=BackoffRetry(base_s=300.0),
            poll_min_s=0.01,
            poll_max_s=0.05,
        )
    finally:
        timer.cancel()
    assert time.monotonic() - start < 10.0
    assert outcome.status == "cancelled"
    assert outcome.states == {"a": "cancelled"}


# R8: without a handler, Ctrl-C still closes the build record as cancelled.
def test_keyboard_interrupt_without_handler_records_cancelled(env: Env) -> None:
    def interrupt(poll: int) -> None:
        if poll == 1:
            raise KeyboardInterrupt

    executor = env.executor(on_poll=interrupt)
    executor.script("a", "hang")
    with pytest.raises(KeyboardInterrupt):
        env.run(env.plan({"a": Node()}), executor)
    assert executor.cancelled == ["a"]
    assert env.store.get_build(BuildId(1)).status == "cancelled"


# R8: after the first Ctrl-C the previous handler is back, so a second one interrupts at once.
def test_second_sigint_uses_previous_handler(env: Env) -> None:
    seen: list[object] = []

    def interrupt(poll: int) -> None:
        if poll == 1:
            os.kill(os.getpid(), signal.SIGINT)
            seen.append(signal.getsignal(signal.SIGINT))

    before = signal.getsignal(signal.SIGINT)
    executor = env.executor(on_poll=interrupt)
    executor.script("a", "hang")
    env.run(env.plan({"a": Node()}), executor, handle_sigint=True)
    assert seen == [before]


# R9: a job the executor stops reporting is eventually treated as lost (infra) and retried.
class Forgetful(ScriptedExecutor):
    def poll(self, handles: Sequence[JobHandle]) -> dict[JobHandle, JobStatus]:
        statuses = super().poll(handles)
        return {h: s for h, s in statuses.items() if not h.job_id.endswith("#1")}


def test_lost_job_is_infra_and_retried(env: Env) -> None:
    executor = Forgetful(env.store, env.cas)
    outcome = env.run(env.plan({"a": Node()}), executor, retry=BackoffRetry(base_s=0.0))
    assert outcome.states == {"a": "done"}
    assert executor.runs("a") == 2
    assert executor.polls >= MAX_MISSED_POLLS


# Executor contract: handles it did return are cancelled, not leaked.
def test_too_few_handles_cancels_the_returned_ones(env: Env) -> None:
    executor = ShortHanded(env.store, env.cas)
    executor.script("s[1]", "hang")
    with pytest.raises(ExecutorError):
        env.run(env.plan({"s[1]": Node(), "s[2]": Node()}), executor)
    assert executor.cancelled == ["s[1]"]


# R8: the store row carries the pending reason the executor reported.
def test_pending_reason_recorded(env: Env) -> None:
    reasons: list[str | None] = []

    def look(poll: int) -> None:
        if poll == 2:
            (row,) = env.store.list_actions(BuildId(1))
            reasons.append(row.pending_reason)

    executor = env.executor(on_poll=look)
    executor.script("a", "pass", pending_polls=2)
    env.run(env.plan({"a": Node()}), executor)
    assert reasons == ["licenses"]
