from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ebs.cas.fs import FsCAS
from ebs.core.digest import hash_bytes
from ebs.plan.keys import nondeterministic_output_id
from ebs.plan.types import OutputSpec
from ebs.runner.env import DEFAULT_PATH
from ebs.runner.main import EXIT_OK
from ebs.runner.result import output_id, resource_usage
from ebs.runner.run import ToolRun, run_tool
from tests.helpers.runner import HOST, Harness, shell_spec


# R7
def test_output_ids(cas: FsCAS, tmp_path: Path) -> None:
    key, content = hash_bytes(b"key"), hash_bytes(b"content")
    assert output_id(OutputSpec("r", "r.txt", "file"), content, key) == content
    nd = OutputSpec("worklib", "work", "dir", deterministic=False)
    assert output_id(nd, content, key) == nondeterministic_output_id(key, "worklib")

    # Through the runner: the manifest passes those ids downstream, digests stay the content.
    h = Harness(tmp_path, cas)
    outputs = [
        OutputSpec("report", "report.txt", "file"),
        OutputSpec("worklib", "work", "dir", deterministic=False),
    ]
    script = "echo -n r > report.txt; mkdir work; date +%s%N > work/stamp\n"
    spec = shell_spec(script, outputs=outputs)
    assert spec.key is not None
    rc, build = h.run(spec)
    assert rc == EXIT_OK
    manifest = h.result(build)
    assert manifest is not None
    report, worklib = manifest.outputs["report"], manifest.outputs["worklib"]
    assert report.id == report.digest == hash_bytes(b"r")
    assert worklib.id == nondeterministic_output_id(spec.key, "worklib")
    assert worklib.digest != worklib.id
    assert cas.has(worklib.digest)
    # A consumer can find the nondeterministic bytes again (P0-13 resolve_output).
    assert h.store.resolve_output("test", worklib.id) == worklib.digest


def _run(cpu_s: float, wall_s: float, max_rss_kb: int) -> ToolRun:
    return ToolRun(
        exit_code=0,
        timed_out=False,
        wall_s=wall_s,
        cpu_s=cpu_s,
        max_rss_kb=max_rss_kb,
        log_dropped=0,
        exec_error=None,
    )


# R9: whole units (canonical JSON has no floats), rounded to nearest.
@pytest.mark.parametrize(
    ("cpu", "wall", "rss", "expected"),
    [
        (0.0, 0.0, 0, (0, 0, 0)),
        (0.4, 0.6, 10, (0, 1, 10)),
        (12.5, 3.49, 2048, (12, 3, 2048)),  # banker's rounding is fine: whole seconds
        (99.9, 120.2, 1 << 20, (100, 120, 1 << 20)),
    ],
)
def test_resource_usage_units(
    cpu: float, wall: float, rss: int, expected: tuple[int, int, int]
) -> None:
    usage = resource_usage(_run(cpu, wall, rss))
    assert (usage.cpu_s, usage.wall_s, usage.max_rss_kb) == expected


# R9
def test_rusage_recorded(tmp_path: Path) -> None:
    # 64 MiB resident and ~0.3 s of CPU, in a grandchild-free python child.
    code = (
        "import time\n"
        "b = bytearray(64 << 20)\n"
        "for i in range(0, len(b), 4096): b[i] = 1\n"
        "t = time.process_time()\n"
        "while time.process_time() - t < 0.3: pass\n"
    )
    run = run_tool(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env={"PATH": DEFAULT_PATH},
        log_path=tmp_path / "tool.log",
        timeout_s=None,
        max_log=1 << 20,
        grace_s=1,
    )
    assert run.exit_code == 0
    assert run.max_rss_kb >= 64 << 10  # ru_maxrss is in KiB on Linux
    assert run.cpu_s >= 0.3
    assert run.wall_s >= run.cpu_s * 0.9


# R9: the manifest carries the usage and where it ran.
def test_rusage_in_manifest(cas: FsCAS, tmp_path: Path) -> None:
    h = Harness(tmp_path, cas, caller_env={"SLURM_JOB_ID": "777"})
    code = "b = bytearray(32 << 20)\nfor i in range(0, len(b), 4096): b[i] = 1\n"
    rc, build = h.run(shell_spec("", argv=[sys.executable, "-c", code]))
    assert rc == EXIT_OK
    manifest = h.result(build)
    assert manifest is not None
    assert manifest.resources.max_rss_kb >= 32 << 10
    assert manifest.runner.host == HOST
    assert manifest.runner.slurm_job_id == "777"
