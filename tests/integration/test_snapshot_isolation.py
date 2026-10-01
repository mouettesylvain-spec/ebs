"""I7 / R7: an action executes on the plan-time snapshot bytes, never on live source paths."""

from __future__ import annotations

import random
import subprocess
import sys
from pathlib import Path

from ebs.cas.fs import FsCAS
from ebs.core.clock import SystemClock
from ebs.sources.snapshot import SourceSnapshotter
from ebs.sources.statcache import StatCache

TOOL = """
import pathlib, sys
root = pathlib.Path(sys.argv[1])
for p in sorted(root.rglob("*.sv")):
    print(p.relative_to(root).as_posix(), p.read_text().strip())
"""


# R7 (I7)
def test_edit_during_build_uses_snapshot_bytes(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "rtl").mkdir(parents=True)
    (src / "rtl" / "a.sv").write_text("module a_v1;\n")
    (src / "rtl" / "b.sv").write_text("module b_v1;\n")
    cas = FsCAS(tmp_path / "cas", "test")

    # plan time: snapshot the declared sources
    with StatCache(tmp_path / "statcache.sqlite") as cache:
        snap = SourceSnapshotter(
            cas, cache, clock=SystemClock(), git=None, rng=random.Random(0)
        ).snapshot(src, "rtl/**/*.sv")

    # the user keeps editing while the build runs
    (src / "rtl" / "a.sv").write_text("module a_v2_edited;\n")
    (src / "rtl" / "b.sv").unlink()
    (src / "rtl" / "c.sv").write_text("module c_new;\n")

    # execution: the runner stages the snapshot tree into scratch and runs the tool there
    scratch = tmp_path / "scratch" / "inputs"
    cas.materialize(snap.digest, "tree", scratch)
    out = subprocess.run(
        [sys.executable, "-c", TOOL, str(scratch)], check=True, capture_output=True, text=True
    ).stdout
    assert out.splitlines() == ["rtl/a.sv module a_v1;", "rtl/b.sv module b_v1;"]
