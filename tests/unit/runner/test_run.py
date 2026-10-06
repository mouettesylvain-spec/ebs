from __future__ import annotations

import os
from pathlib import Path

import pytest

from ebs.cas.fs import FsCAS
from ebs.core.digest import Digest
from ebs.plan.types import OutputSpec
from ebs.runner.env import DEFAULT_PATH
from ebs.runner.errors import format_infra_line, parse_infra_reason
from ebs.runner.main import EXIT_INFRA, EXIT_OK, EXIT_USAGE, RunRequest
from ebs.runner.run import DROPPED_MARKER, run_tool
from tests.helpers.runner import Harness, shell_spec, store_plan

ENV = {"PATH": DEFAULT_PATH}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie (exited, not yet reaped by its new parent) counts as dead.
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except FileNotFoundError:
        return False


# R5
def test_timeout_kills_group(tmp_path: Path) -> None:
    pids = tmp_path / "pids"
    script = f"sleep 60 & echo $! > {pids}; echo $$ >> {pids}; sleep 60\n"
    run = run_tool(
        ["bash", "-c", script],
        cwd=tmp_path,
        env=ENV,
        log_path=tmp_path / "tool.log",
        timeout_s=0.3,
        max_log=1 << 20,
        grace_s=0.5,
    )
    assert run.timed_out
    assert run.wall_s < 5
    grandchild, child = (int(p) for p in pids.read_text().split())
    assert not _alive(child)
    assert not _alive(grandchild)  # the whole process group, not just the direct child


# R5: a tool ignoring SIGTERM is killed with SIGKILL after the grace period.
def test_timeout_escalates_to_sigkill(tmp_path: Path) -> None:
    run = run_tool(
        ["bash", "-c", "trap '' TERM; while :; do sleep 0.05; done"],
        cwd=tmp_path,
        env=ENV,
        log_path=tmp_path / "tool.log",
        timeout_s=0.2,
        max_log=1 << 20,
        grace_s=0.3,
    )
    assert run.timed_out
    assert run.exit_code == -9
    assert run.wall_s < 5


# R5: stdout and stderr go to one log, in order.
def test_combined_log(tmp_path: Path) -> None:
    log = tmp_path / "tool.log"
    run = run_tool(
        ["bash", "-c", "echo out; echo err >&2; echo out2; exit 3"],
        cwd=tmp_path,
        env=ENV,
        log_path=log,
        timeout_s=None,
        max_log=1 << 20,
        grace_s=1,
    )
    assert run.exit_code == 3
    assert not run.timed_out
    assert log.read_text() == "out\nerr\nout2\n"


# R5
def test_log_cap_keeps_tail(tmp_path: Path) -> None:
    log = tmp_path / "tool.log"
    run = run_tool(
        ["bash", "-c", "for i in $(seq 1 5000); do echo line-$i; done"],
        cwd=tmp_path,
        env=ENV,
        log_path=log,
        timeout_s=None,
        max_log=1000,
        grace_s=1,
    )
    total = sum(len(f"line-{i}\n") for i in range(1, 5001))
    data = log.read_bytes()
    assert data.startswith(DROPPED_MARKER.format(dropped=total - 1000).encode())
    body = data[len(DROPPED_MARKER.format(dropped=total - 1000)) :]
    assert len(body) == 1000
    assert body.endswith(b"line-4999\nline-5000\n")
    assert b"line-1\n" not in data
    assert run.log_dropped == total - 1000


# R5: under the cap nothing is dropped or marked.
def test_log_under_cap_untouched(tmp_path: Path) -> None:
    log = tmp_path / "tool.log"
    run = run_tool(
        ["bash", "-c", "printf 'x%.0s' $(seq 1 1000)"],
        cwd=tmp_path,
        env=ENV,
        log_path=log,
        timeout_s=None,
        max_log=1000,
        grace_s=1,
    )
    assert log.read_bytes() == b"x" * 1000
    assert run.log_dropped == 0


# R5: a program that cannot be started is reported, not raised.
def test_exec_failure(tmp_path: Path) -> None:
    log = tmp_path / "tool.log"
    run = run_tool(
        ["no-such-tool-ebs"],
        cwd=tmp_path,
        env=ENV,
        log_path=log,
        timeout_s=None,
        max_log=1000,
        grace_s=1,
    )
    assert run.exec_error is not None
    assert "no-such-tool-ebs" in log.read_text()


LICENSE_FAIL = "echo 'Error: license checkout failed for feature x'; exit 1\n"


# R6
@pytest.mark.parametrize(
    ("script", "time", "code", "status", "reason"),
    [
        pytest.param("echo ok\n", None, EXIT_OK, "passed", None, id="pass"),
        pytest.param("echo bad; exit 2\n", None, EXIT_OK, "failed", None, id="fail"),
        pytest.param(LICENSE_FAIL, None, EXIT_INFRA, None, "license", id="infra-license"),
        pytest.param(
            "echo crashing; kill -SEGV $$\n", None, EXIT_INFRA, None, "other", id="infra-crash"
        ),
        pytest.param(
            "echo slow; sleep 30\n",
            1,  # resources.time is whole seconds, so this case takes ~1 s
            EXIT_INFRA,
            None,
            "timeout",
            id="timeout",
            marks=pytest.mark.slow,
        ),
    ],
)
def test_pass_fail_infra_paths(
    cas: FsCAS,
    tmp_path: Path,
    script: str,
    time: int | None,
    code: int,
    status: str | None,
    reason: str | None,
) -> None:
    h = Harness(tmp_path, cas)
    spec = shell_spec(script, time=time)
    rc, build = h.run(spec, cache_mode="write")
    assert rc == code
    manifest = h.result(build)
    if status is not None:
        assert manifest is not None
        assert manifest.status == status
        assert manifest.action_key == spec.key
        assert manifest.log is not None
        assert cas.has(manifest.log)
        assert h.store.cache_puts == [("test", spec.key)]
        assert h.store.cache_get("test", spec.key) == manifest
        assert h.store.events == []
        assert parse_infra_reason(h.err.getvalue()) is None
    else:
        assert manifest is None  # infra: no result
        assert h.store.cache_puts == []  # and never cached (I12)
        (event,) = h.store.events
        assert event.type == "infra_failed"
        assert event.action_id == "s"
        assert event.build == build
        assert event.data["reason"] == reason
        # The executor reads the same reason from the runner's stderr (interfaces.md § 9).
        assert parse_infra_reason(h.err.getvalue()) == reason
        log = event.data["log"]
        assert isinstance(log, str)
        with cas.open(Digest.parse(log)) as f:
            assert f.read()  # the log is kept for debugging
    assert h.scratch_entries() == []


# R6: the cache entry is written only when the build's cache mode is `write`.
@pytest.mark.parametrize("mode", ["read", "off"])
def test_no_cache_put_unless_write(cas: FsCAS, tmp_path: Path, mode: str) -> None:
    h = Harness(tmp_path, cas)
    rc, build = h.run(shell_spec("true"), cache_mode=mode)  # type: ignore[arg-type]
    assert rc == EXIT_OK
    assert h.result(build) is not None  # the result is still recorded for the build
    assert h.store.cache_puts == []


# R6
def test_missing_output_fails(cas: FsCAS, tmp_path: Path) -> None:
    h = Harness(tmp_path, cas)
    outputs = [
        OutputSpec("report", "reports/lint.txt", "file"),
        OutputSpec("waivers", "reports/waivers.txt", "file"),
        OutputSpec("cov", "cov.ucdb", "file", optional=True),
    ]
    script = "mkdir -p reports; echo clean > reports/lint.txt\n"
    rc, build = h.run(shell_spec(script, outputs=outputs))
    assert rc == EXIT_OK
    manifest = h.result(build)
    assert manifest is not None
    assert manifest.status == "failed"
    assert manifest.exit_code == 0
    assert manifest.summary["missing_output"] == "waivers"
    assert set(manifest.outputs) == {"report"}  # present outputs are still uploaded


# R6: an optional output may be absent from a passing run.
def test_optional_output_may_be_missing(cas: FsCAS, tmp_path: Path) -> None:
    h = Harness(tmp_path, cas)
    outputs = [
        OutputSpec("report", "report.txt", "file"),
        OutputSpec("cov", "cov.ucdb", "file", optional=True),
    ]
    rc, build = h.run(shell_spec("echo ok > report.txt\n", outputs=outputs))
    assert rc == EXIT_OK
    manifest = h.result(build)
    assert manifest is not None
    assert manifest.status == "passed"
    assert set(manifest.outputs) == {"report"}
    assert "missing_output" not in manifest.summary


# R6: an output of the wrong type (a dir where a file was declared) counts as missing.
def test_wrong_output_type_fails(cas: FsCAS, tmp_path: Path) -> None:
    h = Harness(tmp_path, cas)
    rc, build = h.run(
        shell_spec("mkdir report.txt\n", outputs=[OutputSpec("report", "report.txt", "file")])
    )
    assert rc == EXIT_OK
    manifest = h.result(build)
    assert manifest is not None
    assert manifest.status == "failed"
    assert manifest.summary["missing_output"] == "report"


# R6: outputs are uploaded to the CAS: files as blobs, dirs as trees.
def test_outputs_uploaded(cas: FsCAS, tmp_path: Path) -> None:
    h = Harness(tmp_path, cas)
    outputs = [
        OutputSpec("report", "report.txt", "file"),
        OutputSpec("lib", "work/lib", "dir"),
    ]
    script = "echo -n hello > report.txt; mkdir -p work/lib/sub; echo -n abc > work/lib/sub/x\n"
    rc, build = h.run(shell_spec(script, outputs=outputs))
    assert rc == EXIT_OK
    manifest = h.result(build)
    assert manifest is not None
    report, lib = manifest.outputs["report"], manifest.outputs["lib"]
    assert (report.type, report.size) == ("file", 5)
    with cas.open(report.digest) as f:
        assert f.read() == b"hello"
    assert (lib.type, lib.size) == ("tree", 3)
    assert cas.get_tree(lib.digest).entries[0].name == "sub"


# R6: posting failures are temporary (metadata service down): exit 75, nothing cached.
def test_metadata_failure_is_infra(
    cas: FsCAS, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ebs.core.errors import MetadataError

    h = Harness(tmp_path, cas)

    def down(*args: object) -> None:
        raise MetadataError("connection refused")

    monkeypatch.setattr(h.store, "record_result", down)
    rc, _ = h.run(shell_spec("true"))
    assert rc == EXIT_INFRA
    assert h.store.cache_puts == []
    assert "connection refused" in h.err.getvalue()


# R6 / P0-14: the infra reason reaches stderr even when emitting the event fails.
def test_infra_line_survives_emit_failure(
    cas: FsCAS, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ebs.core.errors import MetadataError

    h = Harness(tmp_path, cas)

    def down(*args: object) -> None:
        raise MetadataError("connection refused")

    monkeypatch.setattr(h.store, "emit", down)
    rc, _ = h.run(shell_spec(LICENSE_FAIL))
    assert rc == EXIT_INFRA
    assert parse_infra_reason(h.err.getvalue()) == "license"  # not main()'s fallback "other"


# R5: the group gets SIGTERM first and the grace period to react (not only the final SIGKILL).
def test_timeout_sigterm_group_then_grace(tmp_path: Path) -> None:
    marker = tmp_path / "grandchild-got-term"
    ready = tmp_path / "ready"
    inner = f'trap "touch {marker}; exit 0" TERM; touch {ready}; while :; do sleep 0.05; done'
    script = f"bash -c '{inner}' & sleep 60\n"
    run = run_tool(
        ["bash", "-c", script],
        cwd=tmp_path,
        env=ENV,
        log_path=tmp_path / "tool.log",
        timeout_s=0.5,
        max_log=1 << 20,
        grace_s=1.0,
    )
    assert ready.exists()
    assert run.timed_out
    assert marker.exists()
    assert 0.5 <= run.wall_s < 0.5 + 1.0 + 0.5  # fires on time, ends within the grace period


# R5: processes the tool leaves behind are killed when it exits normally.
def test_stragglers_killed_after_normal_exit(tmp_path: Path) -> None:
    pids = tmp_path / "pids"
    run = run_tool(
        ["bash", "-c", f"sleep 60 & echo $! > {pids}; exit 0"],
        cwd=tmp_path,
        env=ENV,
        log_path=tmp_path / "tool.log",
        timeout_s=None,
        max_log=1 << 20,
        grace_s=1,
    )
    assert run.exit_code == 0
    assert run.wall_s < 3  # the straggler's copy of the log pipe did not keep us waiting
    assert not _alive(int(pids.read_text()))


# R5: a log that cannot be written (scratch disk full) is reported instead of leaving the tool
# blocked on a full pipe.
def test_log_write_error_does_not_hang(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ebs.runner import run as run_mod

    def full(self: object, data: bytes) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(run_mod._CappedLog, "write", full)
    run = run_tool(
        ["bash", "-c", "head -c 4000000 /dev/zero"],
        cwd=tmp_path,
        env=ENV,
        log_path=tmp_path / "tool.log",
        timeout_s=20,
        max_log=1 << 20,
        grace_s=1,
    )
    assert not run.timed_out
    assert run.exit_code == 0
    assert run.log_error is not None
    assert "No space left" in run.log_error


# R6: ... and through the runner that is an infrastructure failure.
def test_log_write_error_is_infra(
    cas: FsCAS, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ebs.runner import run as run_mod

    def full(self: object, data: bytes) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(run_mod._CappedLog, "write", full)
    h = Harness(tmp_path, cas)
    rc, _ = h.run(shell_spec("echo hi\n"), cache_mode="write")
    assert rc == EXIT_INFRA
    assert h.store.results == {}
    assert h.store.cache_puts == []


# R6: a program that cannot start is an infrastructure failure, never a cached FAILED result.
def test_exec_failure_is_infra(cas: FsCAS, tmp_path: Path) -> None:
    h = Harness(tmp_path, cas)
    rc, _ = h.run(shell_spec("", argv=["no-such-tool-ebs"]), cache_mode="write")
    assert rc == EXIT_INFRA
    assert h.store.results == {}
    assert h.store.cache_puts == []
    assert [e.type for e in h.store.events] == ["infra_failed"]


# R6: without --build there is no store to emit to; the reason still reaches the executor through
# the runner's stderr (P0-14).
def test_infra_line_without_build(cas: FsCAS, tmp_path: Path) -> None:
    h = Harness(tmp_path, cas)
    plan = store_plan(cas, shell_spec("", argv=["no-such-tool-ebs"]))
    rc = h.runner().run(RunRequest(plan, "s", None))
    assert rc == EXIT_INFRA
    assert h.store.events == []
    assert parse_infra_reason(h.err.getvalue()) == "other"


def test_infra_line_round_trip() -> None:
    text = "ebs-runner: s: boom\n" + format_infra_line("license", "no seat\nfree")
    assert text.endswith("\n")
    assert text.count("\n") == 2  # one line, whatever the detail holds
    assert parse_infra_reason(text) == "license"
    later = text + format_infra_line("timeout", "x")
    assert parse_infra_reason(later) == "timeout"  # the last line wins
    assert parse_infra_reason("ebs-runner: s: boom\n") is None
    assert parse_infra_reason('ebs-runner: infra_failed {"reason": "nope"}\n') == "other"
    assert parse_infra_reason("ebs-runner: infra_failed [1]\n") == "other"


# R5/R6: a tool that handles SIGTERM and exits 0 after its timeout still timed out.
@pytest.mark.slow
def test_timeout_graceful_exit_is_infra(cas: FsCAS, tmp_path: Path) -> None:
    h = Harness(tmp_path, cas)
    spec = shell_spec("trap 'exit 0' TERM; sleep 30 & wait\n", time=1)
    rc, _ = h.run(spec, cache_mode="write")
    assert rc == EXIT_INFRA
    assert h.store.results == {}
    assert h.store.cache_puts == []
    (event,) = h.store.events
    assert event.data["reason"] == "timeout"


# R6: an output that is a symlink (possibly to outside the sandbox) is not uploaded.
def test_symlink_output_is_missing(cas: FsCAS, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("not the tool's")
    h = Harness(tmp_path, cas)
    rc, build = h.run(
        shell_spec(f"ln -s {outside} out.txt\n", outputs=[OutputSpec("o", "out.txt", "file")])
    )
    assert rc == EXIT_OK
    manifest = h.result(build)
    assert manifest is not None
    assert manifest.status == "failed"
    assert manifest.summary["missing_output"] == "o"
    assert manifest.outputs == {}


# R5: a resources.time that is still a `${…}` reference means the plan was not expanded; never
# run without the limit.
def test_unexpanded_time_is_usage_error(cas: FsCAS, tmp_path: Path) -> None:
    import dataclasses

    from ebs.flow.model import Resources
    from ebs.plan.keys import with_key

    spec = with_key(
        dataclasses.replace(shell_spec("true"), resources=Resources(time="${row.timeout}"))
    )
    h = Harness(tmp_path, cas)
    rc, _ = h.run(spec)
    assert rc == EXIT_USAGE
    assert "time" in h.err.getvalue()
