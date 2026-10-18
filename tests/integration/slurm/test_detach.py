"""`ebs build --detach`, resuming after a driver crash, and cancelling the driver (P2-08).

`test_detach_submits_driver_job` runs the console script on PostgreSQL: the driver job runs on
FakeSlurm and submits the actions to FakeSlurm too, executed by real `ebs-runner` processes. The
other tests drive the SLURM executor in-process with the fake runner of P1-02 (which posts
nothing: `PostingStore` plays that part of the runner, as in test_retry_e2e).
"""

from __future__ import annotations

import json
import os
import re
import signal
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy.engine import URL

from ebs.cas.fs import FsCAS
from ebs.core.clock import Clock, SystemClock
from ebs.driver.build import events_path, run_build, start_build
from ebs.driver.events import EventLog, read_events
from ebs.driver.resume import resume_build
from ebs.driver.retry import BackoffRetry
from ebs.driver.scheduler import DriverConfig, Scheduler
from ebs.exec.api import SubmitBatch
from ebs.exec.slurm.cli import SlurmCli
from ebs.exec.slurm.executor import SlurmExecutor, SlurmSettings
from ebs.meta.api import BuildId, ResultManifest
from ebs.meta.memory import InMemoryMetadataStore
from ebs.plan.planner import refine_plan
from ebs.plan.types import Plan
from tests.fakes.slurm import FakeSlurm
from tests.helpers.driver import DOMAIN, Node, make_plan, refiner, result_for
from tests.helpers.e2e import Site
from tests.helpers.executor import FakeRunner
from tests.helpers.pg import _pg_truncate_engine, pg_migrated, pg_server, pg_url
from tests.helpers.wait import wait_until

__all__ = ["_pg_truncate_engine", "pg_migrated", "pg_server", "pg_url"]

pytestmark = pytest.mark.integration

FAST = DriverConfig(cache_mode="write", poll_min_s=0.05, poll_max_s=0.2)

FLOW = """\
version: 1
project: demo
domain: test
steps:
  gen:
    kind: shell
    script: "cat src/a.txt > out.txt"
    inputs: { src: "src/*.txt" }
    outputs: { out: out.txt }
  sim:
    kind: shell
    matrix: { table: tests.csv }
    script: "cat ${steps.gen.outputs.out} > result.txt && echo ${row.test} >> result.txt"
    outputs: { result: result.txt }
"""


class PostingStore(InMemoryMetadataStore):
    """Posts the result the real runner would have posted once its job completed."""

    plan: Plan | None = None

    def get_result(self, build: BuildId, action_id: str) -> ResultManifest | None:
        found = super().get_result(build, action_id)
        if found is None and self.plan is not None:
            # The runner loads the refined plan: producers' outputs complete consumers' keys.
            produced = {
                (a.action_id, name): out.id
                for a in self.plan.actions
                if (r := super().get_result(build, a.action_id)) is not None
                for name, out in r.outputs.items()
            }
            spec = refine_plan(self.plan, produced).action(action_id)
            if spec.key is None:
                return None
            found = result_for(spec)
            self.record_result(build, action_id, found)
        return found


def slurm_executor(
    tmp_path: Path, fake: FakeSlurm, runner: FakeRunner, store: InMemoryMetadataStore
) -> SlurmExecutor:
    runner.cas_root = tmp_path / "cas"
    return SlurmExecutor(
        cli=SlurmCli(bin_dir=fake.bin_dir),
        cas=FsCAS(runner.cas_root, DOMAIN),
        settings=SlurmSettings(debug_root=tmp_path / "debug", poll_min_s=0.05, poll_max_s=0.2),
        store=store,
        runner_argv=runner.argv,
        env=runner.env(),
    )


def fake_jobs(fake: FakeSlurm) -> list[sqlite3.Row]:
    db = sqlite3.connect(fake.state_dir / "state.db")
    db.row_factory = sqlite3.Row
    try:
        return list(db.execute("SELECT * FROM jobs ORDER BY id"))
    finally:
        db.close()


# R1: --detach submits `ebs build --resume-into <uuid>` as a 1-CPU job with the [driver]
# partition, QOS and walltime, prints the UUID and job id and exits 0; the driver job then runs
# the build, and `ebs status` follows it as usual.
def test_detach_submits_driver_job(tmp_path: Path, fake_slurm: FakeSlurm, pg_url: URL) -> None:
    fake_slurm.configure(cpus=4)
    site = Site(
        tmp_path / "site",
        pg_url,
        extra_config=(
            f'[slurm]\nbin_dir = "{fake_slurm.bin_dir}"\npoll_min_s = 0.1\npoll_max_s = 0.5\n'
            f'[debug]\nroot = "{tmp_path / "debug"}"\n'
            '[driver]\npartition = "service"\nqos = "driver"\ntime = "2h"\n'
        ),
    )
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "flow.yaml").write_text(FLOW)
    # Two rows: `sim` runs as one job array, read by the real ebs-runner's --array-map.
    (proj / "tests.csv").write_text("test\nsmoke\nlong\n")
    (proj / "src" / "a.txt").write_text("hello\n")

    proc = site.ebs(proj, "build", "--detach", "--executor", "slurm", "--cache", "write")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    found = re.search(r"build ([0-9a-f-]{36}) \(id (\d+)\)", proc.stdout)
    job = re.search(r"driver job (\d+)", proc.stdout)
    assert found, proc.stdout
    assert job, proc.stdout
    uuid, job_id = found[1], job[1]
    driver = next(r for r in fake_jobs(fake_slurm) if str(r["id"]) == job_id)
    assert driver["cpus"] == 1
    assert (driver["partition"], driver["qos"], driver["time_s"]) == ("service", "driver", 7200)
    assert driver["comment"] == uuid
    assert driver["name"] == f"ebs:{uuid.replace('-', '')[:8]}:driver"
    assert Path(driver["workdir"]) == proj
    script = Path(driver["script"]).read_text()
    assert f"--resume-into {uuid}" in script
    assert "--executor slurm" in script

    def status() -> dict[str, object]:
        out = site.ebs(proj, "status", uuid, "--json")
        assert out.returncode == 0, out.stdout + out.stderr
        doc: dict[str, object] = json.loads(out.stdout.splitlines()[-1])
        return doc

    wait_until(lambda: status()["status"] != "running", timeout=60, what="the detached build")
    doc = status()
    assert doc["status"] == "passed", doc
    assert doc["totals"] == {"done": 3}
    # Each step ran as one SLURM job (sim as an array), submitted once by the driver job: an array
    # task the runner refused would show up as an extra, retried job.
    prefix = f"ebs:{uuid.replace('-', '')[:8]}"
    jobs = sorted(
        (r["name"], r["array_task_id"] if r["array_job_id"] else -1) for r in fake_jobs(fake_slurm)
    )
    assert jobs == [
        (f"{prefix}:driver", -1),
        (f"{prefix}:gen", -1),
        (f"{prefix}:sim", 0),
        (f"{prefix}:sim", 1),
    ]
    assert all(r["state"] == "COMPLETED" for r in fake_jobs(fake_slurm))
    record = json.loads((proj / ".ebs" / "builds" / uuid / "build.json").read_text())
    assert record["status"] == "passed"
    assert (proj / ".ebs" / "builds" / uuid / "driver.out").exists()


class Crash(BaseException):
    """The driver process dies (kill -9): no cleanup runs."""


class CrashingClock(Clock):
    """The real clock, except that sleeping raises `Crash` once `when()` holds."""

    def __init__(self, when: Callable[[], bool]) -> None:
        self._real = SystemClock()
        self._when = when

    def now(self) -> datetime:
        return self._real.now()

    def monotonic(self) -> float:
        return self._real.monotonic()

    def sleep(self, seconds: float) -> None:
        if self._when():
            raise Crash
        self._real.sleep(seconds)


# R2 (and R3): the driver dies while two jobs run; a new driver (another process: a fresh
# executor) takes over once the dead one's lease expired, re-attaches to the recorded SLURM jobs
# and finishes the build without resubmitting them.
def test_kill_driver_then_resume(tmp_path: Path, fake_slurm: FakeSlurm) -> None:
    runner = FakeRunner(tmp_path / "runner")
    runner.set("a", hold=True)
    runner.set("b", hold=True)
    store = PostingStore()
    executor = slurm_executor(tmp_path, fake_slurm, runner, store)
    plan = make_plan(executor._cas, {"a": Node(), "b": Node(), "c": Node(deps=(("a", "out"),))})
    store.plan = plan
    workdir = tmp_path / "work"
    s = start_build(
        plan, cas=executor._cas, store=store, workdir=workdir, user="alice", cache_mode="write"
    )
    assert store.claim_driver(s.build, "node-a:1:dead", 1).owner == "node-a:1:dead"
    dead = Scheduler(
        plan,
        plan_digest=s.plan_digest,
        refiner=refiner(executor._cas),
        cas=executor._cas,
        store=store,
        executor=executor,
        build=s.build,
        events=EventLog(store, s.build, events_path(workdir, s.uuid), SystemClock()),
        clock=CrashingClock(lambda: sorted(runner.started()) == ["a", "b"]),
        config=FAST,
        retry=BackoffRetry(max_retries=0),
    )
    with pytest.raises(Crash):
        dead.run()
    rows = {r.action_id: r for r in store.list_actions(s.build)}
    assert rows["a"].slurm_job_id
    assert rows["b"].slurm_job_id
    wait_until(lambda: store.claim_driver(s.build, "probe", 1).owner == "probe", timeout=5)
    store.release_driver(s.build, "probe")

    def release_both() -> None:
        runner.release("a")
        runner.release("b")

    releaser = threading.Timer(1.0, release_both)
    releaser.start()
    try:
        outcome = resume_build(
            s.build,
            cas=executor._cas,
            store=store,
            executor=slurm_executor(tmp_path, fake_slurm, runner, store),  # a new process
            workdir=workdir,
            config=FAST,
            owner="node-b:2:new",
        )
    finally:
        releaser.join()
    assert outcome.status == "passed", outcome.states
    assert outcome.states == {"a": "done", "b": "done", "c": "done"}
    assert sorted(runner.started()) == ["a", "b", "c"]  # each ran once
    names = [r["name"] for r in fake_jobs(fake_slurm)]
    assert len(names) == 3  # nothing resubmitted
    types = [e.type for e in read_events(events_path(workdir, s.uuid))]
    assert types.count("build_resumed") == 1
    assert types[-1] == "build_finished"


# R4: SIGTERM to the driver (what `scancel` of the driver job sends) cancels the build's jobs.
def test_cancel_driver_cancels_jobs(tmp_path: Path, fake_slurm: FakeSlurm) -> None:
    runner = FakeRunner(tmp_path / "runner")
    for a in ("a", "b"):
        runner.set(a, hold=True)
    store = PostingStore()
    executor = slurm_executor(tmp_path, fake_slurm, runner, store)
    plan = make_plan(executor._cas, {"a": Node(), "b": Node()})
    store.plan = plan

    def terminate_when_running() -> None:
        wait_until(lambda: sorted(runner.started()) == ["a", "b"], what="both jobs running")
        os.kill(os.getpid(), signal.SIGTERM)

    killer = threading.Thread(target=terminate_when_running)
    previous = signal.getsignal(signal.SIGTERM)
    killer.start()
    try:
        outcome = run_build(
            plan,
            refiner=refiner(executor._cas),
            cas=executor._cas,
            store=store,
            executor=executor,
            workdir=tmp_path / "work",
            user="alice",
            config=FAST,
            handle_sigterm=True,
        )
    finally:
        killer.join()
    assert signal.getsignal(signal.SIGTERM) == previous  # the handler is removed again
    assert outcome.status == "cancelled"
    assert outcome.states == {"a": "cancelled", "b": "cancelled"}
    assert store.get_build(outcome.build).status == "cancelled"
    wait_until(
        lambda: all(r["state"] == "CANCELLED" for r in fake_jobs(fake_slurm)),
        what="SLURM shows every job cancelled",
    )
    wait_until(lambda: all(runner.cleaned(a) for a in ("a", "b")), what="runners cleaned up")


# R4 on the path a driver job takes (`--resume-into` = resume_build): SIGTERM cancels the jobs
# the resumed driver re-attached to as well as those it submitted itself.
def test_cancel_resumed_driver_cancels_jobs(tmp_path: Path, fake_slurm: FakeSlurm) -> None:
    runner = FakeRunner(tmp_path / "runner")
    for a in ("a", "b"):
        runner.set(a, hold=True)
    store = PostingStore()
    executor = slurm_executor(tmp_path, fake_slurm, runner, store)
    plan = make_plan(executor._cas, {"a": Node(), "b": Node()})
    store.plan = plan
    workdir = tmp_path / "work"
    s = start_build(
        plan, cas=executor._cas, store=store, workdir=workdir, user="alice", cache_mode="write"
    )
    executor.submit(  # the dead driver's job for a; b is still queued
        SubmitBatch(plan=s.plan_digest, domain=DOMAIN, build=s.build, actions=(plan.action("a"),))
    )

    def terminate_when_running() -> None:
        wait_until(lambda: sorted(runner.started()) == ["a", "b"], what="both jobs running")
        os.kill(os.getpid(), signal.SIGTERM)

    killer = threading.Thread(target=terminate_when_running)
    previous = signal.getsignal(signal.SIGTERM)
    killer.start()
    try:
        outcome = resume_build(
            s.build,
            cas=executor._cas,
            store=store,
            executor=slurm_executor(tmp_path, fake_slurm, runner, store),  # a new process
            workdir=workdir,
            config=FAST,
            handle_sigterm=True,
        )
    finally:
        killer.join()
    assert signal.getsignal(signal.SIGTERM) == previous
    assert outcome.status == "cancelled"
    assert outcome.states == {"a": "cancelled", "b": "cancelled"}
    assert store.get_build(s.build).status == "cancelled"
    wait_until(
        lambda: all(r["state"] == "CANCELLED" for r in fake_jobs(fake_slurm)),
        what="SLURM shows every job cancelled",
    )
    assert len(fake_jobs(fake_slurm)) == 2  # a was adopted, not resubmitted
