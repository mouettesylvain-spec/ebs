"""Tests for the undeclared-read hint (task P3-01 R3, unit part)."""

from __future__ import annotations

from ebs.sandbox.api import undeclared_read_hint

LOG = """\
** Note: loading design
Error: cannot open file '/home/alice/.synopsys_dc.setup': No such file or directory
** Error: errors in design
"""


# R3
def test_hint_names_the_path_and_discover() -> None:
    hint = undeclared_read_hint(LOG, sandbox="bwrap", step="sim")
    assert hint is not None
    assert "/home/alice/.synopsys_dc.setup" in hint
    assert "ebs discover sim" in hint
    assert "bwrap" in hint


# R3: the common shapes of "file not found" messages.
def test_hint_shapes() -> None:
    for line in (
        "cat: /proj/x/defs.v: No such file or directory",
        "ERROR: file /proj/x/defs.v does not exist",
        '** Error: (vlog-7) Failed to open design unit file "/proj/x/defs.v" in read mode.',
        "make: *** No rule to make target '/proj/x/defs.v'.  Stop.",
    ):
        hint = undeclared_read_hint(line + "\n", sandbox="bwrap", step="s")
        assert hint is not None, line
        assert "/proj/x/defs.v" in hint, line


# R3: no hint without a real sandbox, or without a missing-file message.
def test_no_hint() -> None:
    assert undeclared_read_hint(LOG, sandbox="staging", step="sim") is None
    assert undeclared_read_hint("Error: assertion failed\n", sandbox="bwrap", step="s") is None


# R3: a missing *relative* path is a staged-input problem, not the sandbox; still hint without
# a path, since the message still points at a read that failed.
def test_hint_without_absolute_path() -> None:
    hint = undeclared_read_hint("x.v: No such file or directory\n", sandbox="bwrap", step="s")
    assert hint is not None
    assert "ebs discover s" in hint


# R3: the Apptainer fallback hides files too, so it gets the hint as well.
def test_hint_under_apptainer() -> None:
    hint = undeclared_read_hint(LOG, sandbox="apptainer", step="sim")
    assert hint is not None
    assert "apptainer" in hint


# R3: a relative path in the message is not mistaken for an absolute one.
def test_hint_relative_path_not_absolute() -> None:
    hint = undeclared_read_hint("sub/x.v: No such file or directory\n", sandbox="bwrap", step="s")
    assert hint is not None
    assert "/x.v" not in hint
