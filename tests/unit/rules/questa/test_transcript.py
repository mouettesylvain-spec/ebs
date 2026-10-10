"""Tests for Questa transcript parsing (task P1-08 R4).

The fixtures under `fixtures/` are synthetic: hand-written in the shape of simulator output.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ebs.rules.questa.transcript import (
    TranscriptSummary,
    error_signature,
    parse_transcript,
    sim_passed,
)

FIXTURES = Path(__file__).parent / "fixtures"

SCB_SIGNATURE = (
    "UVM_ERROR <path>(<n>) @ <n>: uvm_test_top.env.sb [SCB] data mismatch at <hex>: "
    "expected <hex> got <hex>"
)

# name -> (uvm_errors, uvm_fatals, errors, fatals, finished, crashed, first error, signature)
CASES: dict[str, tuple[int, int, int, int, bool, bool, str | None, str | None]] = {
    "pass_uvm": (0, 0, 0, 0, True, False, None, None),
    "pass_plain": (0, 0, 0, 0, True, False, None, None),
    "uvm_error": (
        2,
        0,
        0,
        0,
        True,
        False,
        "UVM_ERROR /proj/cpu/tb/scoreboard.sv(142) @ 12500: uvm_test_top.env.sb [SCB] data "
        "mismatch at 0x1f00: expected 'h12 got 'h13",
        SCB_SIGNATURE,
    ),
    "uvm_fatal": (
        0,
        1,
        0,
        0,
        True,
        False,
        "UVM_FATAL tb/env.sv(31) @ 0: uvm_test_top.env [NOVIF] virtual interface must be set "
        "for vif_3",
        "UVM_FATAL <path>(<n>) @ <n>: uvm_test_top.env [NOVIF] virtual interface must be set "
        "for vif_3",
    ),
    "sva_fatal": (
        0,
        0,
        1,
        1,
        True,
        False,
        "** Error: Assertion error.",
        "** Error: Assertion error.",
    ),
    "no_finish": (0, 0, 0, 0, False, False, None, None),
    "license_fail": (
        0,
        0,
        1,
        1,
        False,
        False,
        "** Error: License checkout failed for feature 'simfeature' (status -5)",
        "** Error: License checkout failed for feature 'simfeature' (status -<n>)",
    ),
    "segv": (
        0,
        0,
        0,
        1,
        False,
        True,
        "** Fatal: (SIGSEGV) Bad handle or reference.",
        "** Fatal: (SIGSEGV) Bad handle or reference.",
    ),
    "vlog_error": (
        0,
        0,
        2,
        0,
        False,
        False,
        "** Error: rtl/core/alu.sv(12): (vlog-1001) Undefined variable: 'sum_q'.",
        "** Error: <path>(<n>): (vlog-1001) Undefined variable: 'sum_q'.",
    ),
}


def _parse(name: str) -> TranscriptSummary:
    with open(FIXTURES / f"{name}.txt", encoding="utf-8") as f:
        return parse_transcript(f)


# R4
def test_every_fixture_has_a_case() -> None:
    assert {p.stem for p in FIXTURES.glob("*.txt")} == set(CASES)


# R4
@pytest.mark.parametrize("name", sorted(CASES))
def test_parse_fixtures(name: str) -> None:
    uvm_errors, uvm_fatals, errors, fatals, finished, crashed, first, signature = CASES[name]
    s = _parse(name)
    assert (s.uvm_errors, s.uvm_fatals, s.errors, s.fatals) == (
        uvm_errors,
        uvm_fatals,
        errors,
        fatals,
    )
    assert s.finished is finished
    assert s.crashed is crashed
    assert s.first_error == first
    assert s.signature == signature


# R4: PASSED iff exit 0, no fatals, UVM_ERROR = 0 and a completion marker
@pytest.mark.parametrize(
    ("name", "exit_code", "expected"),
    [
        ("pass_uvm", 0, True),
        ("pass_plain", 0, True),
        ("pass_uvm", 1, False),  # a clean transcript does not hide a failing exit status
        ("uvm_error", 0, False),  # the simulator exits 0 on UVM errors
        ("uvm_fatal", 0, False),
        ("sva_fatal", 0, False),
        ("no_finish", 0, False),  # ran out of events or was stopped: no completion marker
        ("license_fail", 0, False),
        ("segv", 0, False),
    ],
)
def test_sim_passed(name: str, exit_code: int, expected: bool) -> None:
    assert sim_passed(exit_code, _parse(name)) is expected


# R4
def test_summary_counts_win_over_report_lines() -> None:
    # The report summary is authoritative (report lines may be filtered or truncated).
    text = "# UVM_ERROR a.sv(1) @ 0: x [A] boom\n# UVM_ERROR :    5\n# UVM_FATAL :    0\n"
    s = parse_transcript(text.splitlines(keepends=True))
    assert (s.uvm_errors, s.uvm_fatals) == (5, 0)


# R4
def test_report_lines_counted_without_summary() -> None:
    text = (
        "UVM_ERROR a.sv(1) @ 0: x [A] boom\n"
        "UVM_ERROR a.sv(2) @ 5: x [A] boom\n"
        "UVM_FATAL a.sv(3) @ 9: x [B] stop\n"
    )
    s = parse_transcript(text.splitlines(keepends=True))
    assert (s.uvm_errors, s.uvm_fatals) == (2, 1)
    assert s.first_error == "UVM_ERROR a.sv(1) @ 0: x [A] boom"


# R4
def test_last_summary_wins_and_counts_are_not_doubled() -> None:
    text = "UVM_ERROR :    1\nUVM_ERROR :    3\n"
    assert parse_transcript(text.splitlines()).uvm_errors == 3


# R4
def test_report_counts_heading_is_not_an_error() -> None:
    s = parse_transcript(["# ** Report counts by severity\n", "# ** Note: $finish : t.sv(1)\n"])
    assert (s.errors, s.fatals, s.first_error, s.finished) == (0, 0, None, True)


# R4
def test_finish_must_be_a_marker_not_any_mention() -> None:
    s = parse_transcript(["# calling $finish soon\n", "# UVM_INFO @ 0: r [X] $finish later\n"])
    assert s.finished is False


# R4
def test_first_error_is_capped() -> None:
    s = parse_transcript(["** Error: " + "x" * 5000 + "\n"])
    assert s.first_error is not None
    assert len(s.first_error) <= 512
    assert s.signature is not None
    assert len(s.signature) <= 512


# R4
@pytest.mark.parametrize(
    ("line", "signature"),
    [
        ("# ** Error: /a/b/c.sv(12): bad", "** Error: <path>(<n>): bad"),
        (
            "UVM_ERROR x.sv(7) @ 1250: top.env [ID] got 0xDEAD_beef",
            "UVM_ERROR x.sv(<n>) @ <n>: top.env [ID] got <hex>",
        ),
        (
            "** Fatal: value 8'hff != 8'b0000_0001 at 12.5 ns",
            "** Fatal: value <hex> != <hex> at <n> ns",
        ),
        ("** Error: agent_1 and core0 differ by 3", "** Error: agent_1 and core0 differ by <n>"),
        ("** Error:   lots    of   space  ", "** Error: lots of space"),
        ("** Error: Undefined 'data_q' and 'bad'", "** Error: Undefined 'data_q' and 'bad'"),
    ],
)
def test_signature_normalization(line: str, signature: str) -> None:
    assert error_signature(line) == signature


_WORDS = st.sampled_from(["mismatch", "at", "expected", "got", "addr", "lane", "core0", "x_1"])


# R4: numbers, hex values and paths never split a cluster; the signature is a fixed point
@given(
    words=st.lists(_WORDS, min_size=1, max_size=6),
    a=st.lists(st.integers(0, 10**12), min_size=3, max_size=3),
    b=st.lists(st.integers(0, 10**12), min_size=3, max_size=3),
    dirs=st.lists(st.from_regex(r"[a-z][a-z0-9_]{0,6}", fullmatch=True), min_size=1, max_size=4),
)
def test_signature_ignores_numbers_hex_and_paths(
    words: list[str], a: list[int], b: list[int], dirs: list[str]
) -> None:
    def line(n: list[int], path: str) -> str:
        return (
            f"# UVM_ERROR {path}/tb.sv({n[0]}) @ {n[1]}: top [SCB] "
            f"{' '.join(words)} 0x{n[2]:x} {n[0]}"
        )

    first = error_signature(line(a, "/" + "/".join(dirs)))
    assert first == error_signature(line(b, "/".join(reversed(dirs))))
    assert error_signature(first) == first


# R4: as specified, a bare `** Error` (e.g. an SVA `$error`) is counted but does not fail a test
def test_errors_only_transcript_passes() -> None:
    lines = ["# ** Error: Assertion error.\n", "# ** Note: $finish    : tb.sv(9)\n"]
    s = parse_transcript(lines)
    assert (s.errors, s.fatals, s.first_error) == (1, 0, "** Error: Assertion error.")
    assert sim_passed(0, s) is True


# R4: a crash report is remembered even when later fatal lines follow it
def test_crash_flag_sticks_after_later_fatal() -> None:
    s = parse_transcript(["# ** Fatal: (SIGSEGV) bad handle\n", "# ** Fatal: vsim exiting\n"])
    assert (s.crashed, s.fatals) == (True, 2)


# R4
@pytest.mark.parametrize("sig", ["SEGV", "BUS", "ILL", "FPE", "ABRT"])
def test_crash_signals(sig: str) -> None:
    assert parse_transcript([f"# ** Fatal: (SIG{sig}) x\n"]).crashed is True


# R4
@pytest.mark.parametrize(
    "line", ["# ** Fatal: (SIGTERM) x\n", "# ** Fatal: got SIGSEGV from the bench\n"]
)
def test_other_fatals_are_not_crashes(line: str) -> None:
    assert parse_transcript([line]).crashed is False


# R4: only a leading `#` is the transcript prefix; `** Errors: 0` is a tally, not an error
def test_prefix_and_word_boundaries() -> None:
    s = parse_transcript(["** Errors: 0, Warnings: 0\n", "# ** Error: bad #3 here\n"])
    assert (s.errors, s.first_error) == (1, "** Error: bad #3 here")
    assert error_signature("** Error: bad #3 here") == "** Error: bad #<n> here"


# R4: message ids name the diagnostic and are kept; a lone `/` is not a path
def test_signature_keeps_message_ids_and_ratios() -> None:
    assert error_signature("** Error: a.sv(3): (vlog-2110) x") == (
        "** Error: a.sv(<n>): (vlog-2110) x"
    )
    assert error_signature("** Error: 3 / 4 lanes") == "** Error: <n> / <n> lanes"
