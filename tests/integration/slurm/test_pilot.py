"""Pilot worker pool under FakeSlurm with a PostgreSQL queue (P3-06 R1, R2)."""

from __future__ import annotations

import json
import signal
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy.engine import URL

from ebs.config import CONFIG_ENV
from ebs.exec.api import JobHandle
from ebs.exec.pilot.executor import PilotExecutor, PilotSettings
from ebs.exec.slurm.executor import SlurmSettings
from ebs.exec.slurm.models import ArrayTaskId, JobId
from ebs.meta.pg import PgMetadataStore
from tests.fakes.slurm import FakeSlurm
from tests.fakes.slurm.core import kill_group, locked
from tests.helpers.executor import PILOT_ARGV, FakeRunner, batch, wait_states
from tests.helpers.pg import url_string
from tests.helpers.runner import DOMAIN
from tests.helpers.wait import wait_until

pytestmark = pytest.mark.integration


@dataclass
class Pool:
    slurm: FakeSlurm
    fake: FakeRunner
    store: PgMetadataStore
    config: Path
    tmp_path: Path

    def executor(self, **pilot: object) -> PilotExecutor:
        return PilotExecutor(
            cli=self.slurm.cli(),
            store=self.store,
            domain=DOMAIN,
            settings=SlurmSettings(debug_root=self.tmp_path / "debug", poll_min_s=0.2),
            pilot=PilotSettings(**{"idle_s": 1.0, "lease_ttl_s": 3, "poll_s": 0.05, **pilot}),  # type: ignore[arg-type]
            pilot_argv=PILOT_ARGV,
            runner_argv=self.fake.argv,
            env={**self.fake.env(), CONFIG_ENV: str(self.config)},
        )


@pytest.fixture
def pool(tmp_path: Path, pg_url: URL, monkeypatch: pytest.MonkeyPatch) -> Iterator[Pool]:
    monkeypatch.chdir(tmp_path)
    slurm = FakeSlurm.install(tmp_path / "fakeslurm", cpus=2)
    url = url_string(pg_url)
    config = tmp_path / "ebs.toml"
    config.write_text(f"[metadata]\nurl = {json.dumps(url)}\n")
    store = PgMetadataStore.from_config({"url": url})
    yield Pool(slurm, FakeRunner(tmp_path / "fake"), store, config, tmp_path)
    slurm.shutdown()
    store.close()


def _kill(slurm: FakeSlurm, task: ArrayTaskId) -> None:
    with locked(slurm.state_dir) as st:
        (row,) = [
            r
            for r in st.all_jobs()
            if str(r["array_job_id"]) == str(task.array_job_id)
            and r["array_task_id"] == task.task_id
        ]
        pgid = int(row["job_pgid"])
    kill_group(pgid, signal.SIGKILL)


def _pilot_states(ex: PilotExecutor) -> dict[str, str]:
    return {str(ref): info.state for ref, info in ex.query_pilots().items()}


# R1: N pilots (one array) drain a queue larger than N, then exit on their own once idle.
def test_pool_drains_queue(pool: Pool) -> None:
    ids = [f"t[{i}]" for i in range(12)]
    ex = pool.executor(max_pilots=2)
    handles = ex.submit(batch(*ids))
    final = wait_states(ex, handles, timeout=60)
    assert all(s.state == "done" for s in final.values())
    assert all(s.node == "n001" for s in final.values())
    assert sorted(pool.fake.started()) == sorted(ids)
    assert max(int(str(s["load"])) for s in pool.fake.starts()) <= 2
    pilots = ex.pilot_refs()
    assert len(pilots) == 2  # one sbatch: an array of two pilots for twelve actions
    assert len({JobId(str(p).split("_")[0]) for p in pilots}) == 1
    wait_until(
        lambda: set(_pilot_states(ex).values()) == {"COMPLETED"},
        timeout=20,
        what="pilots exit after idle_s",
    )


# R2: a pilot killed mid-action loses its lease; the action is reported as an infrastructure
# failure (the driver's retry re-queues it) and runs again on another pilot.
def test_pilot_death_requeues(pool: Pool) -> None:
    pool.fake.set("a", hold=True)
    ex = pool.executor(max_pilots=1)
    (h,) = ex.submit(batch("a"))
    wait_until(lambda: pool.fake.started() == ["a"], timeout=20, what="a started")
    (pilot,) = ex.pilot_refs()
    assert isinstance(pilot, ArrayTaskId)
    _kill(pool.slurm, pilot)  # the pilot and its runner die without a word
    wait_until(
        lambda: _pilot_states(ex).get(str(pilot)) not in (None, "RUNNING"),
        timeout=20,
        what="the pilot is dead",
    )
    status = wait_states(ex, [h], timeout=20)[h]
    assert (status.state, status.infra_reason, status.node) == ("infra_failed", "node_fail", "n001")
    pool.fake.release("a")  # the retry runs to the end
    (retry,) = ex.submit(batch("a"))  # what the driver's infra retry does
    assert isinstance(retry, JobHandle)
    assert retry != h
    final = wait_states(ex, [retry], timeout=30)[retry]
    assert final.state == "done"
    assert pool.fake.started() == ["a", "a"]
    assert len(ex.pilot_refs()) == 2  # a new pilot was started for the re-queued action
