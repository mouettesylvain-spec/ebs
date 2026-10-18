"""A GitLab pipeline as it runs ebs (P2-07): `ebs build --ci` writes ebs.env, and a gate job runs
`ebs channel set … --if-passing` with the `EBS_BUILD_ID` it got from that dotenv artifact."""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy.engine import URL

from ebs.core.errors import ExitCode
from tests.helpers.e2e import Site
from tests.helpers.pg import _pg_truncate_engine, pg_migrated, pg_server, pg_url

__all__ = ["_pg_truncate_engine", "pg_migrated", "pg_server", "pg_url"]

FLOW = """\
version: 1
project: demo
domain: e2e
steps:
  gen:
    kind: shell
    script: "cat src/a.txt > out.txt"
    inputs: { src: "src/*.txt" }
    outputs: { out: out.txt }
  sim:
    kind: shell
    matrix: { table: tests.csv }
    params: { test: "${row.test}" }
    script: "cat ${steps.gen.outputs.out} > result.txt;
      echo UVM_ERROR auth tok-e2e-5ecret-42; test ${row.test} != bad"
    outputs: { result: result.txt }
"""


def _ci_build(site: Site, proj: Path, ci_dir: Path, pipeline: str) -> tuple[int, int]:
    """`ebs build --ci` in a pipeline; returns (exit code, EBS_BUILD_ID from ebs.env)."""
    site.extra_env.update(CI="true", GITLAB_CI="true", CI_PROJECT_DIR=str(ci_dir),
                          CI_PROJECT_PATH="cpu/rtl", CI_PIPELINE_ID=pipeline)  # fmt: skip
    proc = site.ebs(proj, "build", "--ci", "-k")
    dotenv = dict(
        line.split("=", 1) for line in (ci_dir / "ebs.env").read_text().splitlines() if line
    )
    assert (ci_dir / "ebs-summary.md").is_file()
    assert (ci_dir / "ebs-junit.xml").is_file()
    return proc.returncode, int(dotenv["EBS_BUILD_ID"])


# R4: the gate job moves the channel only after a passing build, else fails the pipeline.
def test_gate(tmp_path: Path, pg_url: URL) -> None:
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "flow.yaml").write_text(FLOW)
    (proj / "tests.csv").write_text("test\nsmoke\n")
    (proj / "src" / "a.txt").write_text("rtl v1\n")
    site = Site(tmp_path / "site", pg_url, extra_config='[release.gates]\nstable = "demo"\n')
    site.extra_env["EBS_META_TOKEN"] = "tok-e2e-5ecret-42"

    code, passed = _ci_build(site, proj, tmp_path / "ci1", "1")
    assert code == ExitCode.OK
    made = site.ebs(proj, "release", "create", "cpu", "1", "--build", str(passed),
                    "--output", "gen.out")  # fmt: skip
    assert made.returncode == 0, made.stderr
    (proj / "tests.csv").write_text("test\nsmoke\nbad\n")
    code, failed = _ci_build(site, proj, tmp_path / "ci2", "2")
    # R5 end to end: the log the JUnit report quotes has the token masked
    junit = (tmp_path / "ci2" / "ebs-junit.xml").read_text()
    assert "UVM_ERROR auth [MASKED]" in junit
    assert "tok-e2e-5ecret-42" not in junit
    assert code == ExitCode.ACTIONS_FAILED

    def gate(channel: str, build: int | None) -> tuple[int, str]:
        site.extra_env.pop("EBS_BUILD_ID", None)
        if build is not None:
            site.extra_env["EBS_BUILD_ID"] = str(build)
        proc = site.ebs(proj, "channel", "set", "cpu", channel, "1", "--if-passing", "--json")
        return proc.returncode, proc.stdout + proc.stderr

    def shown(channel: str) -> object:
        proc = site.ebs(proj, "channel", "show", "cpu", channel, "--json")
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)["channel"]

    # no channel policy: --if-passing alone guards the move
    code, text = gate("nightly", failed)
    assert code != 0
    assert f"build {failed}" in text
    assert "is failed, not passed" in text
    assert shown("nightly") is None
    code, text = gate("nightly", None)
    assert code == ExitCode.USAGE
    assert "EBS_BUILD_ID" in text
    code, text = gate("nightly", passed)
    assert code == 0, text
    assert json.loads(text.splitlines()[0])["gate"] == {"flow": "demo", "build": passed}
    assert shown("nightly") is not None

    # with a channel gate (`stable` needs a passed `demo` build that consumed the release)
    code, text = gate("stable", failed)
    assert code != 0
    assert shown("stable") is None
    code, text = gate("stable", passed)
    assert code == 0, text
    assert json.loads(text.splitlines()[0])["gate"] == {"flow": "demo", "build": passed}
