"""An OOM-killed job is retried with more memory and then succeeds (P1-05 R1, R2, R5).

The driver runs on FakeSlurm, which kills a job whose memory goes over `--mem`
(`OUT_OF_MEMORY`). The fake runner fills 110 MiB: with `mem: 100M` it is killed, and the retry
asks for 150M (x 1.5) and passes. The fake runner posts nothing, so the store below plays that
part of the runner: it posts a passed result for a job SLURM says completed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ebs.cas.fs import FsCAS
from ebs.driver.build import run_build
from ebs.driver.events import read_events
from ebs.driver.retry import ReasonRetry, RetrySettings, attempt_history
from ebs.driver.scheduler import DriverConfig
from ebs.exec.slurm.cli import SlurmCli
from ebs.exec.slurm.executor import SlurmExecutor, SlurmSettings
from ebs.flow.model import Resources
from ebs.meta.api import BuildId, ResultManifest
from ebs.meta.memory import InMemoryMetadataStore
from ebs.plan.types import Plan
from tests.fakes.slurm import FakeSlurm
from tests.helpers.driver import DOMAIN, Node, make_plan, refiner, result_for
from tests.helpers.executor import FakeRunner

pytestmark = pytest.mark.integration

M = 1 << 20


class PostingStore(InMemoryMetadataStore):
    """Posts the result the real runner would have posted once its job completed."""

    plan: Plan | None = None

    def get_result(self, build: BuildId, action_id: str) -> ResultManifest | None:
        found = super().get_result(build, action_id)
        if found is None and self.plan is not None:
            found = result_for(self.plan.action(action_id))
            self.record_result(build, action_id, found)
            self.cache_put(DOMAIN, found.action_key, found)
        return found


# R1, R2, R5
def test_oom_then_success(tmp_path: Path, fake_slurm: FakeSlurm) -> None:
    fake_slurm.configure(cpus=4)
    runner = FakeRunner(tmp_path / "runner")
    runner.set("sim[a]", alloc_mb=110)
    runner.cas_root = tmp_path / "cas"
    cas = FsCAS(runner.cas_root, DOMAIN)
    store = PostingStore()
    plan = make_plan(cas, {"sim[a]": Node(resources=Resources(mem="100M"))})
    store.plan = plan
    executor = SlurmExecutor(
        cli=SlurmCli(bin_dir=fake_slurm.bin_dir),
        cas=cas,
        settings=SlurmSettings(debug_root=tmp_path / "debug"),
        store=store,
        runner_argv=runner.argv,
        env=runner.env(),
    )
    workdir = tmp_path / "work"
    workdir.mkdir()
    outcome = run_build(
        plan,
        refiner=refiner(cas),
        cas=cas,
        store=store,
        executor=executor,
        workdir=workdir,
        user="alice",
        config=DriverConfig(cache_mode="write", poll_min_s=0.05, poll_max_s=0.2),
        retry=ReasonRetry(RetrySettings(backoff_s=0.0)),
    )

    assert outcome.states == {"sim[a]": "done"}
    assert outcome.exit_code == 0
    assert len(runner.started()) == 2
    assert outcome.plan.action("sim[a]").resources.mem == 150 * M
    history = attempt_history(read_events(outcome.events_path))["sim[a]"]
    assert [(h.attempt, h.reason) for h in history] == [(1, "oom"), (2, None)]
    assert history[0].node == "n001"  # FakeSlurm's node, as SLURM reported it
    # Only the passed result is cached, under the unchanged key (memory is not in the key).
    key = plan.action("sim[a]").key
    assert key is not None
    assert outcome.plan.action("sim[a]").key == key
    cached = store.cache_get(DOMAIN, key)
    assert cached is not None
    assert cached.status == "passed"
