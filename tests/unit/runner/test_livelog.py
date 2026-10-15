"""Live log in the debug area, flushed every 5 s (P1-04 R3, decision D2)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from ebs.cas.fs import FsCAS
from ebs.core.clock import FakeClock
from ebs.plan.types import DebugSpec
from ebs.runner.collect import debug_action_dir
from ebs.runner.livelog import FLUSH_INTERVAL_S, LiveLog
from ebs.runner.main import EXIT_OK
from tests.helpers.runner import DOMAIN, Harness, shell_spec


def test_interval_is_d2() -> None:
    assert FLUSH_INTERVAL_S == 5.0


# R3
def test_flush_interval(tmp_path: Path) -> None:
    clock = FakeClock()
    path = tmp_path / "live.log"
    live = LiveLog(path, clock=clock)
    live.write(b"one\n")
    assert path.read_bytes() == b""  # created at once, so `ebs logs -f` can open it
    clock.advance(4.9)
    live.tick()
    assert path.read_bytes() == b""
    clock.advance(0.1)
    live.tick()
    assert path.read_bytes() == b"one\n"
    live.write(b"two\n")
    clock.advance(1)
    live.tick()
    assert path.read_bytes() == b"one\n"  # 1 s after the last flush
    clock.advance(4)
    live.write(b"three\n")  # a write that finds a flush due flushes too
    assert path.read_bytes() == b"one\ntwo\nthree\n"
    live.write(b"four\n")
    live.close()  # the rest goes out on close
    assert path.read_bytes() == b"one\ntwo\nthree\nfour\n"


# R3: a tool that floods its log cannot fill /debug: past the cap only a note is added.
def test_cap(tmp_path: Path) -> None:
    clock = FakeClock()
    path = tmp_path / "live.log"
    live = LiveLog(path, clock=clock, max_bytes=10)
    live.write(b"0123456789abc")
    live.write(b"more")
    live.close()
    data = path.read_bytes()
    assert data.startswith(b"0123456789")
    assert b"live log stopped" in data
    assert b"more" not in data


def _debug_harness(tmp_path: Path, cas: FsCAS) -> Harness:
    (tmp_path / "debug" / DOMAIN).mkdir(parents=True)
    return Harness(tmp_path, cas, settings_overrides={"debug_root": tmp_path / "debug"})


# R3: removed after success unless debug output is kept on success.
@pytest.mark.parametrize(
    ("script", "on_success", "kept"),
    [
        pytest.param("echo live-out\n", False, False, id="passed"),
        pytest.param("echo live-out\n", True, True, id="passed-kept"),
        pytest.param("echo live-out\nexit 1\n", False, True, id="failed"),
    ],
)
def test_live_log_lifecycle(
    cas: FsCAS, tmp_path: Path, script: str, on_success: bool, kept: bool
) -> None:
    h = _debug_harness(tmp_path, cas)
    spec = dataclasses.replace(shell_spec(script), debug=DebugSpec((), None, on_success))
    code, build = h.run(spec)
    assert code == EXIT_OK, h.err.getvalue()
    live = debug_action_dir(tmp_path / "debug", DOMAIN, str(build), spec.action_id) / "live.log"
    assert live.exists() == kept
    if kept:
        assert live.read_bytes() == b"live-out\n"


# R3: the cap holds across flushes, not just within one write.
def test_cap_across_flushes(tmp_path: Path) -> None:
    clock = FakeClock()
    path = tmp_path / "live.log"
    live = LiveLog(path, clock=clock, max_bytes=10)
    live.write(b"abcdef")
    clock.advance(5)
    live.tick()
    assert path.read_bytes() == b"abcdef"
    live.write(b"ghijkl")
    live.write(b"mnop")
    live.close()
    data = path.read_bytes()
    assert data.startswith(b"abcdefghij")
    assert b"k" not in data.split(b"\n", 1)[0]
    assert b"mnop" not in data
    assert b"live log stopped" in data
