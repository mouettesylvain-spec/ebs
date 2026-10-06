from __future__ import annotations

import os
import signal
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import structlog

from ebs.core.errors import ExecutorError
from ebs.exec.api import JobHandle
from ebs.exec.local import LocalExecutor
from ebs.meta.api import BuildId
from ebs.runner.errors import format_infra_line
from tests.helpers.executor import PLAN, FakeRunner, batch, wait_states
from tests.helpers.runner import DOMAIN
from tests.helpers.wait import wait_until

Make = Callable[..., LocalExecutor]


@pytest.fixture
def fake(tmp_path: Path) -> FakeRunner:
    return FakeRunner(tmp_path / "fake")


@pytest.fixture
def make(fake: FakeRunner, tmp_path: Path) -> Iterator[Make]:
    created: list[LocalExecutor] = []

    def factory(max_parallel: int | None = None) -> LocalExecutor:
        ex = LocalExecutor(
            log_dir=tmp_path / "logs",
            runner_argv=fake.argv,
            max_parallel=max_parallel,
            env=fake.env(),
        )
        created.append(ex)
        return ex

    yield factory
    for ex in created:
        ex.close(grace_s=2)


def _ids(n: int) -> list[str]:
    return [f"t[{i}]" for i in range(n)]


# R1: never more than `max_parallel` runners at once; the rest start as slots free up.
def test_parallelism_bound(fake: FakeRunner, make: Make) -> None:
    ids = _ids(5)
    for a in ids:
        fake.set(a, hold=True)
    ex = make(max_parallel=2)
    handles = ex.submit(batch(*ids))
    assert [h.action_id for h in handles] == ids
    wait_until(lambda: len(fake.started()) == 2, what="two runners started")
    states = ex.poll(handles)
    assert [states[h].state for h in handles] == ["running"] * 2 + ["pending"] * 3
    fake.release(ids[0])  # one finish frees one slot, which goes to the oldest queued action
    wait_states(ex, handles[:1])
    wait_until(lambda: len(fake.started()) == 3, what="third runner started")
    states = ex.poll(handles)
    expected = ["done", "running", "running", "pending", "pending"]
    assert [states[h].state for h in handles] == expected
    for a in ids:
        fake.release(a)
    assert all(s.state == "done" for s in wait_states(ex, handles).values())
    assert sorted(fake.started()) == sorted(ids)
    assert max(int(str(s["load"])) for s in fake.starts()) <= 2


# R1: the default bound is the CPU count.
def test_default_parallelism_is_cpu_count(make: Make, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.cpu_count", lambda: 3)
    assert make().max_parallel == 3
    monkeypatch.setattr("os.cpu_count", lambda: None)
    assert make().max_parallel == 1


# R1: `resources.cpus` counts as slots; a head action that does not fit blocks later ones (FIFO).
def test_cpus_are_slots(fake: FakeRunner, make: Make) -> None:
    ids = ["t[big]", "t[wide]", "t[small]"]
    for a in ids:
        fake.set(a, hold=True, cpus={"t[big]": 3, "t[wide]": 2, "t[small]": 1}[a])
    ex = make(max_parallel=4)
    handles = ex.submit(batch(*ids, cpus={"t[big]": 3, "t[wide]": 2, "t[small]": 1}))
    wait_until(lambda: len(fake.started()) == 1, what="first runner")
    states = ex.poll(handles)
    # big (3) runs; wide (2) does not fit in the 1 slot left; small (1) would, but waits its turn.
    assert [states[h].state for h in handles] == ["running", "pending", "pending"]
    fake.release("t[big]")
    wait_until(lambda: bool(ex.poll(handles)) and len(fake.started()) == 3, what="the rest")
    assert fake.started()[0] == "t[big]"
    assert sorted(fake.started()) == sorted(ids)
    assert max(int(str(s["load"])) for s in fake.starts()) <= 4
    for a in ids:
        fake.release(a)
    assert all(s.state == "done" for s in wait_states(ex, handles).values())


# R1: an action asking for more cpus than the executor has still runs (alone), with a warning.
def test_oversized_action_runs_alone(fake: FakeRunner, make: Make) -> None:
    fake.set("t[huge]", hold=True, cpus=8)
    fake.set("t[next]", hold=True)
    ex = make(max_parallel=2)
    with structlog.testing.capture_logs() as logs:
        handles = ex.submit(batch("t[huge]", "t[next]", cpus={"t[huge]": 8}))
    assert any("cpus" in str(e.get("event")) for e in logs if e["log_level"] == "warning")
    wait_until(lambda: fake.started() == ["t[huge]"], what="huge started")
    assert ex.poll(handles)[handles[1]].state == "pending"
    fake.release("t[huge]")
    fake.release("t[next]")
    assert all(s.state == "done" for s in wait_states(ex, handles).values())
    assert fake.started() == ["t[huge]", "t[next]"]


# R1: the runner gets the plan, action, domain and (when set) build.
@pytest.mark.parametrize("build", [None, BuildId(7)])
def test_runner_argv(fake: FakeRunner, make: Make, build: BuildId | None) -> None:
    ex = make(max_parallel=1)
    (h,) = ex.submit(batch("t[0]", build=build))
    wait_states(ex, [h])
    argv = fake.argv_of("t[0]")
    expected = ["--plan", str(PLAN), "--action", "t[0]", "--domain", DOMAIN]
    if build is not None:
        expected += ["--build", "7"]
    assert argv == expected


# R2: poll answers for every handle at once, without waiting for any runner.
def test_poll_nonblocking_and_pending_reason(fake: FakeRunner, make: Make) -> None:
    ids = _ids(3)
    for a in ids:
        fake.set(a, hold=True)
    ex = make(max_parallel=1)
    handles = ex.submit(batch(*ids))
    wait_until(lambda: fake.started() == ids[:1], what="first runner")
    states = ex.poll(handles)  # a blocking poll would hang here: every runner is held
    assert set(states) == set(handles)
    assert states[handles[0]].state == "running"
    assert states[handles[0]].pending_reason is None
    for h in handles[1:]:
        assert states[h].state == "pending"
        assert states[h].pending_reason == "resources"
        assert states[h].infra_reason is None
        assert states[h].exit_code is None
    assert ex.poll([handles[2]]).keys() == {handles[2]}  # any subset
    for a in ids:
        fake.release(a)
    final = wait_states(ex, handles)
    assert all(s.state == "done" and s.exit_code == 0 for s in final.values())
    assert ex.poll(handles) == final  # terminal states are stable


# R2: a handle this executor did not hand out is an error, not a silent "unknown".
def test_poll_unknown_handle(make: Make) -> None:
    ex = make(max_parallel=1)
    with pytest.raises(ExecutorError, match="unknown"):
        ex.poll([JobHandle(executor="local", job_id="999", action_id="x")])


# R3
@pytest.mark.parametrize(
    ("behaviour", "state", "reason", "exit_code"),
    [
        pytest.param({"exit": 0}, "done", None, 0, id="ok"),
        pytest.param(
            {"exit": 75, "stderr": "ebs-runner: s: boom\n" + format_infra_line("timeout", "slow")},
            "infra_failed",
            "timeout",
            75,
            id="infra-with-reason",
        ),
        pytest.param(
            {"exit": 75, "stderr": format_infra_line("license", "x") + "noise after\n"},
            "infra_failed",
            "license",
            75,
            id="infra-reason-not-last-line",
        ),
        pytest.param({"exit": 75}, "infra_failed", "other", 75, id="infra-no-event"),
        pytest.param(
            {"exit": 75, "stderr": "ebs-runner: infra_failed {not json\n"},
            "infra_failed",
            "other",
            75,
            id="infra-garbled-event",
        ),
        pytest.param(
            {"exit": 75, "stderr": 'ebs-runner: infra_failed {"reason": "bogus"}\n'},
            "infra_failed",
            "other",
            75,
            id="infra-unknown-reason",
        ),
        pytest.param({"exit": 76}, "infra_failed", "input_verification", 76, id="verify"),
        pytest.param({"exit": 64}, "infra_failed", "runner_crash", 64, id="usage"),
        pytest.param({"exit": 70}, "infra_failed", "runner_crash", 70, id="internal"),
        pytest.param({"exit": 1}, "infra_failed", "runner_crash", 1, id="other-code"),
        pytest.param(
            {"signal": int(signal.SIGKILL)},
            "infra_failed",
            "runner_crash",
            -int(signal.SIGKILL),
            id="signal",
        ),
    ],
)
def test_exit_code_mapping(
    fake: FakeRunner,
    make: Make,
    behaviour: dict[str, object],
    state: str,
    reason: str | None,
    exit_code: int,
) -> None:
    fake.set("t[0]", **behaviour)
    ex = make(max_parallel=1)
    (h,) = ex.submit(batch("t[0]"))
    status = wait_states(ex, [h])[h]
    assert status.state == state
    assert status.infra_reason == reason
    assert status.exit_code == exit_code
    assert status.pending_reason is None


# R4: cancel terminates a running runner (which cleans up) and drops queued ones.
def test_cancel(fake: FakeRunner, make: Make) -> None:
    ids = _ids(3)
    for a in ids:
        fake.set(a, hold=True)
    ex = make(max_parallel=1)
    handles = ex.submit(batch(*ids))
    wait_until(lambda: fake.started() == ids[:1], what="first runner")
    ex.cancel(handles[:2])  # one running, one queued
    states = ex.poll(handles)
    assert states[handles[0]].state == "cancelled"
    assert states[handles[1]].state == "cancelled"
    wait_until(lambda: fake.cleaned(ids[0]), what="runner got SIGTERM and cleaned up")
    # The freed slot goes to the remaining queued action; the cancelled one never starts.
    wait_until(lambda: bool(ex.poll(handles)) and len(fake.started()) == 2, what="third starts")
    assert fake.started() == [ids[0], ids[2]]
    assert ex.poll(handles)[handles[2]].state == "running"
    fake.release(ids[2])
    final = wait_states(ex, handles)
    assert [final[h].state for h in handles] == ["cancelled", "cancelled", "done"]
    ex.cancel(handles)  # cancelling finished work is a no-op
    assert ex.poll(handles) == final


# R4: close() cancels everything and waits for the runners to exit.
def test_close_waits_for_runners(fake: FakeRunner, make: Make) -> None:
    ids = _ids(2)
    for a in ids:
        fake.set(a, hold=True)
    ex = make(max_parallel=2)
    handles = ex.submit(batch(*ids))
    wait_until(lambda: len(fake.started()) == 2, what="both started")
    ex.close(grace_s=5)
    assert all(fake.cleaned(a) for a in ids)
    assert not fake.running()
    assert all(s.state == "cancelled" for s in ex.poll(handles).values())
    with pytest.raises(ExecutorError, match="closed"):
        ex.submit(batch("t[late]"))


def _stubborn(tmp_path: Path, max_parallel: int) -> tuple[LocalExecutor, Path]:
    """An executor whose "runner" ignores SIGTERM; it creates a file in `ready` once it does."""
    ready = tmp_path / "ready"
    ready.mkdir()
    script = tmp_path / "stubborn.sh"
    script.write_text('trap "" TERM\ntouch "$READY/$$"\nwhile :; do sleep 0.05; done\n')
    ex = LocalExecutor(
        log_dir=tmp_path / "logs",
        runner_argv=("bash", str(script)),
        max_parallel=max_parallel,
        env={"PATH": "/usr/bin:/bin", "READY": str(ready)},
    )
    return ex, ready


# R4: a runner that ignores SIGTERM is killed after the grace period.
def test_close_kills_after_grace(tmp_path: Path) -> None:
    ex, ready = _stubborn(tmp_path, max_parallel=1)
    (h,) = ex.submit(batch("t[0]"))
    wait_until(lambda: len(list(ready.iterdir())) == 1, what="trap installed")
    started = time.monotonic()
    with structlog.testing.capture_logs() as logs:
        ex.close(grace_s=0.2)
    assert time.monotonic() - started < 5
    assert any("ignored SIGTERM" in str(e["event"]) for e in logs), logs
    assert ex.poll([h])[h].state == "cancelled"


# R4: the grace period bounds the whole close, not each stubborn runner.
def test_close_grace_is_shared(tmp_path: Path) -> None:
    ex, ready = _stubborn(tmp_path, max_parallel=3)
    ex.submit(batch("t[0]", "t[1]", "t[2]"))
    wait_until(lambda: len(list(ready.iterdir())) == 3, what="traps installed")
    started = time.monotonic()
    with structlog.testing.capture_logs() as logs:
        ex.close(grace_s=0.5)
    assert time.monotonic() - started < 1.2  # per-runner grace would take >= 1.5 s
    assert sum("ignored SIGTERM" in str(e["event"]) for e in logs) == 3


# R4: cancelling a runner that already exited (not yet polled) keeps its real outcome.
def test_cancel_after_exit_keeps_state(fake: FakeRunner, make: Make) -> None:
    fake.set("t[0]", exit=0)
    ex = make(max_parallel=1)
    (h,) = ex.submit(batch("t[0]"))
    wait_until(lambda: len(fake.started()) == 1 and not fake.running(), what="fake finished")
    wait_until(lambda: _exited(ex, h), what="runner process exited")
    ex.cancel([h])
    assert ex.poll([h])[h].state == "done"


def _exited(ex: LocalExecutor, h: JobHandle) -> bool:
    """True once the runner exited; WNOWAIT leaves it for the executor to reap."""
    proc = ex._jobs[h].proc
    assert proc is not None
    return os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None


# R3: a runner that cannot start is a runner crash; the queue keeps moving.
def test_runner_cannot_start(tmp_path: Path) -> None:
    ex = LocalExecutor(
        log_dir=tmp_path / "logs",
        runner_argv=(str(tmp_path / "missing-runner"),),
        max_parallel=1,
        env={"PATH": "/usr/bin:/bin"},
    )
    handles = ex.submit(batch("t[0]", "t[1]"))
    states = ex.poll(handles)
    for h in handles:
        assert states[h].state == "infra_failed"
        assert states[h].infra_reason == "runner_crash"
        assert states[h].exit_code is None
    ex.close(grace_s=1)


# R5: memory requests are only logged; nothing enforces them.
def test_memory_is_advisory(fake: FakeRunner, make: Make) -> None:
    ex = make(max_parallel=1)
    with structlog.testing.capture_logs() as logs:
        (h,) = ex.submit(batch("t[0]", mem="64G"))
    assert any("mem" in e and e.get("enforced") is False for e in logs), logs
    assert wait_states(ex, [h])[h].state == "done"
